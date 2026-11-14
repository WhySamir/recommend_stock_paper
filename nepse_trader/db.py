"""Neon Postgres access: connection, migrations and a smoke check.

The connection string comes only from the ``NEON_CONN_URL`` environment
variable. It is never printed, logged or written anywhere by this module.

    python -m nepse_trader.db --check     # connectivity and table inventory
    python -m nepse_trader.db --migrate   # apply migrations/*.sql
    python -m nepse_trader.db --smoke     # CRUD through the real store layer

When ``NEON_CONN_URL`` is unset the engine keeps using the file-backed state in
``state_dir`` (the offline/test mode); nothing here is required for CSV runs.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ENV_VAR = "NEON_CONN_URL"
ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"


class DbError(RuntimeError):
    pass


def load_env_files(*paths):
    """Populate ``os.environ`` from ``KEY=value`` files without overriding it."""
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def connection_string():
    return (os.environ.get(ENV_VAR) or "").strip() or None


def enabled():
    return connection_string() is not None


def connect():
    """One short-lived connection. Neon's pooler handles the reuse."""
    url = connection_string()
    if not url:
        raise DbError("%s is not set" % ENV_VAR)
    try:
        import psycopg
    except ImportError:
        raise DbError(
            "psycopg is not installed; run: pip install -r requirements.txt"
        )
    try:
        return psycopg.connect(url, connect_timeout=15, prepare_threshold=None)
    except Exception as exc:  # never echo the URL or the password
        raise DbError("could not connect to Neon: %s" % exc.__class__.__name__)


@contextmanager
def transaction():
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    finally:
        conn.close()


def execute(sql, params=None):
    with transaction() as cur:
        cur.execute(sql, params or ())
        return cur.rowcount


def fetch_all(sql, params=None):
    with transaction() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def scalar(sql, params=None):
    rows = fetch_all(sql, params)
    return rows[0][0] if rows else None


def jsonb(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def migrate(migrations_dir=MIGRATIONS_DIR):
    """Apply every ``*.sql`` file once, in filename order."""
    files = sorted(Path(migrations_dir).glob("*.sql"))
    applied = []
    with transaction() as cur:
        cur.execute(
            "create table if not exists schema_migrations ("
            " version text primary key,"
            " applied_at timestamptz not null default now())"
        )
        cur.execute("select pg_advisory_xact_lock(%s)", (84219931,))
        cur.execute("select version from schema_migrations")
        done = {row[0] for row in cur.fetchall()}
        for path in files:
            if path.name in done:
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "insert into schema_migrations (version) values (%s)", (path.name,)
            )
            applied.append(path.name)
    return applied


def inventory():
    rows = fetch_all(
        "select table_name from information_schema.tables"
        " where table_schema = current_schema()"
        " order by table_name"
    )
    return [row[0] for row in rows]


def _smoke():
    """Write, read, update and delete through ``nepse_trader.store``."""
    from . import store

    db_info = fetch_all("select current_database(), version()")[0]
    print("connected: %s on %s" % (db_info[0], db_info[1].split(",")[0]))

    store.write_state("smoke_test", {"hello": "world", "n": 1})
    first = store.read_state("smoke_test")
    assert first == {"hello": "world", "n": 1}, first
    print("snapshot write/read ok:", first)

    store.write_state("smoke_test", {"hello": "world", "n": 2})
    updated = store.read_state("smoke_test")
    assert updated["n"] == 2, updated
    print("snapshot update ok:", updated)

    original_alerts = store.load_alerts()
    probe = [
        {
            "symbol": "SMOKE",
            "kind": "above",
            "level": 123.45,
            "note": "smoke test",
            "since": "",
            "sessions": 0,
            "triggered": False,
            "triggered_on": "",
        }
    ]
    store.save_alerts(probe)
    written = store.load_alerts()
    assert len(written) == 1 and written[0]["symbol"] == "SMOKE", written
    written[0]["triggered"] = True
    written[0]["triggered_on"] = "2026-11-14"
    store.save_alerts(written)
    assert store.load_alerts()[0]["triggered"] is True
    print("alert write/read/update ok")
    store.save_alerts(original_alerts)
    assert len(store.load_alerts()) == len(original_alerts)
    print("alert rows restored")

    payload = {
        "id": "smoke-0001",
        "symbol": "SMOKE",
        "action": "buy",
        "shares": 10,
        "days": 2,
        "forecast": {"expected_net_pnl": 0.0},
    }
    store.save_simulation(payload)
    assert store.get_simulation("smoke-0001")["symbol"] == "SMOKE"
    assert any(row["id"] == "smoke-0001" for row in store.list_simulations())
    print("simulation write/read/list ok")
    store.delete_simulation("smoke-0001")
    assert store.get_simulation("smoke-0001") is None
    print("simulation delete ok")

    execute("delete from state_snapshots where kind = %s", ("smoke_test",))
    assert store.read_state("smoke_test") is None
    print("snapshot delete ok")
    print("SMOKE PASS")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nepse-trader-db", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="connect and list tables")
    group.add_argument("--migrate", action="store_true", help="apply migrations/*.sql")
    group.add_argument("--smoke", action="store_true", help="CRUD through the store")
    parser.add_argument(
        "--env-file", default=None, help="load env vars from this file first"
    )
    args = parser.parse_args(argv)

    if args.env_file:
        load_env_files(args.env_file)
    elif not enabled() and (ROOT / ".env").is_file():
        load_env_files(ROOT / ".env")

    if not enabled():
        print("%s is not set" % ENV_VAR, file=sys.stderr)
        return 2

    try:
        if args.migrate:
            applied = migrate()
            print("applied: %s" % (", ".join(applied) if applied else "nothing new"))
            args.check = True
        if args.smoke:
            _smoke()
            return 0
        print("database:", fetch_all("select current_database()")[0][0])
        print("tables:", ", ".join(inventory()) or "(none)")
        return 0
    except DbError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
