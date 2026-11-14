"""File-backed price/stop/target alerts for the NEPSE session."""

from __future__ import annotations

import json
from pathlib import Path

KINDS = ("above", "below", "stop", "target", "max_hold")


def path_for(cfg):
    return Path(cfg["state_dir"]) / "alerts.json"


def load(path):
    path = Path(path)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, alerts):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alerts, indent=2) + "\n", encoding="utf-8")


def add(alerts, symbol, kind, level=None, note="", since="", sessions=0):
    if kind not in KINDS:
        raise ValueError("unknown alert kind %r" % kind)
    alerts.append(
        {
            "symbol": symbol.upper(),
            "kind": kind,
            "level": float(level) if level is not None else None,
            "note": note,
            "since": since,
            "sessions": int(sessions),
            "triggered": False,
            "triggered_on": "",
        }
    )
    return alerts


def from_plan(alerts, plan, cfg):
    symbol = plan["symbol"]
    add(alerts, symbol, "stop", plan["stop"], "stop from plan")
    add(alerts, symbol, "target", plan["target"], "target from plan")
    add(
        alerts,
        symbol,
        "max_hold",
        None,
        "flat: %d sessions held" % plan["max_hold_days"],
        since=plan.get("signal_date", ""),
        sessions=plan["max_hold_days"],
    )
    return alerts


def check(alerts, source, bars_for=None):
    """Mark alerts that have hit. Returns the alerts that fired just now."""
    fired = []
    for alert in alerts:
        if alert["triggered"]:
            continue
        symbol = alert["symbol"]
        if alert["kind"] == "max_hold":
            if bars_for is None:
                continue
            bars = bars_for(symbol)
            held = sum(1 for bar in bars if alert["since"] and bar["date"] > alert["since"])
            if held >= alert["sessions"]:
                alert["triggered"] = True
                alert["triggered_on"] = bars[-1]["date"]
                fired.append(
                    dict(alert, message="%s: %s sessions since %s - flatten"
                         % (symbol, held, alert["since"]))
                )
            continue

        price, as_of = source.last(symbol)
        level = alert["level"]
        hit = False
        if alert["kind"] == "above":
            hit = price >= level
        elif alert["kind"] == "below":
            hit = price <= level
        elif alert["kind"] == "stop":
            hit = price <= level
        elif alert["kind"] == "target":
            hit = price >= level
        if hit:
            alert["triggered"] = True
            alert["triggered_on"] = as_of
            fired.append(
                dict(
                    alert,
                    price=price,
                    message="%s %s hit: price Rs %s vs level Rs %s (%s)"
                    % (symbol, alert["kind"], price, level, as_of or "latest"),
                )
            )
    return fired
