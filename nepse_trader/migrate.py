"""Apply the Neon schema and copy the file-backed state into Postgres.

    python -m nepse_trader.migrate --config config.json

Reads ``NEON_CONN_URL`` from the environment, falling back to ``.env`` /
``.env.local`` in the project root. Existing ``trader/*.json`` state is copied
once: a snapshot is only written when its row does not exist yet, and alerts /
simulations are only imported when their tables are empty, so re-running the
command never duplicates or overwrites newer data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from . import db
from . import store

SNAPSHOTS = ("scan", "plans", "orders", "timemachine")


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def import_state(state_dir):
    """Copy whatever the file store has into Neon. Returns counts."""
    state_dir = Path(state_dir)
    counts = {"migrations": 0, "snapshots": 0, "alerts": 0, "simulations": 0}

    counts["migrations"] = len(db.migrate())

    for kind in SNAPSHOTS:
        if store.read_state(kind) is not None:
            continue
        payload = _read_json(state_dir / ("%s.json" % kind))
        if payload is not None:
            store.write_state(kind, payload)
            counts["snapshots"] += 1

    alerts = _read_json(state_dir / "alerts.json")
    if alerts and not store.load_alerts():
        store.save_alerts(alerts)
        counts["alerts"] = len(alerts)

    for path in sorted(state_dir.glob("simulations/*/result.json")):
        payload = _read_json(path)
        if not payload or not payload.get("id"):
            continue
        if store.get_simulation(payload["id"]) is not None:
            continue
        store.save_simulation(payload)
        counts["simulations"] += 1

    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nepse-trader-migrate", description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)

    if args.env_file:
        db.load_env_files(args.env_file)
    else:
        db.load_env_files(db.ROOT / ".env", db.ROOT / ".env.local")

    if not db.enabled():
        print("%s is not set" % db.ENV_VAR, file=sys.stderr)
        return 2

    cfg = config_mod.load(args.config)
    try:
        counts = import_state(cfg["state_dir"])
    except db.DbError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("migrations applied: %d" % counts["migrations"])
    print("snapshots imported: %d" % counts["snapshots"])
    print("alerts imported: %d" % counts["alerts"])
    print("simulations imported: %d" % counts["simulations"])
    print("tables:", ", ".join(db.inventory()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
