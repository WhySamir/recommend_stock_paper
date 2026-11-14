"""Persistence for the engine's state.

With ``NEON_CONN_URL`` set every write goes to Neon Postgres. Otherwise the
original JSON / folder state under ``state_dir`` is used, which keeps offline
CSV runs and the test suite working without a database.
"""

from __future__ import annotations

from . import db

SNAPSHOT_KINDS = ("scan", "plans", "orders", "timemachine")


def backend():
    return "postgres" if db.enabled() else "files"


# -- snapshots: scan / plans / orders / timemachine -------------------


def write_state(name, payload):
    db.execute(
        "insert into state_snapshots (kind, payload, updated_at)"
        " values (%s, %s, now())"
        " on conflict (kind) do update"
        " set payload = excluded.payload, updated_at = now()",
        (name, db.jsonb(payload)),
    )


def read_state(name):
    rows = db.fetch_all(
        "select payload from state_snapshots where kind = %s", (name,)
    )
    return rows[0][0] if rows else None


# -- alerts -----------------------------------------------------------


def load_alerts():
    rows = db.fetch_all(
        "select symbol, kind, level, note, since, sessions, triggered,"
        " triggered_on from alerts order by id"
    )
    return [
        {
            "symbol": row[0],
            "kind": row[1],
            "level": row[2],
            "note": row[3],
            "since": row[4],
            "sessions": row[5],
            "triggered": row[6],
            "triggered_on": row[7],
        }
        for row in rows
    ]


def save_alerts(alerts):
    """Replace the whole list, matching the file store's semantics."""
    with db.transaction() as cur:
        cur.execute("delete from alerts")
        for alert in alerts or []:
            cur.execute(
                "insert into alerts (symbol, kind, level, note, since, sessions,"
                " triggered, triggered_on) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(alert.get("symbol", "")).upper(),
                    str(alert.get("kind", "")),
                    alert.get("level"),
                    str(alert.get("note") or ""),
                    str(alert.get("since") or ""),
                    int(alert.get("sessions") or 0),
                    bool(alert.get("triggered")),
                    str(alert.get("triggered_on") or ""),
                ),
            )


# -- custom paper-trade simulations -----------------------------------


def save_simulation(payload):
    db.execute(
        "insert into simulations (id, symbol, action, shares, days, payload)"
        " values (%s, %s, %s, %s, %s, %s)"
        " on conflict (id) do update set payload = excluded.payload",
        (
            payload["id"],
            payload.get("symbol") or "",
            payload.get("action") or "",
            int(payload.get("shares") or 0),
            int(payload.get("days") or 0),
            db.jsonb(payload),
        ),
    )
    return payload


def get_simulation(simulation_id):
    rows = db.fetch_all(
        "select payload from simulations where id = %s", (simulation_id,)
    )
    return rows[0][0] if rows else None


def list_simulations():
    rows = db.fetch_all(
        "select payload->>'id', payload->>'symbol', payload->>'action',"
        " (payload->>'shares')::int, (payload->>'days')::int,"
        " payload->>'generated_at'"
        " from simulations order by created_at desc"
    )
    return [
        {
            "id": row[0],
            "symbol": row[1],
            "action": row[2],
            "shares": row[3],
            "days": row[4],
            "generated_at": row[5],
        }
        for row in rows
    ]


def delete_simulation(simulation_id):
    return db.execute("delete from simulations where id = %s", (simulation_id,))


# -- paper positions --------------------------------------------------


def load_positions():
    rows = db.fetch_all(
        "select symbol, quantity, avg_price, realized_pnl from positions"
        " order by symbol"
    )
    return [
        {
            "symbol": row[0],
            "quantity": row[1],
            "avg_price": row[2],
            "realized_pnl": row[3],
        }
        for row in rows
    ]


def save_position(symbol, quantity, avg_price, realized_pnl=0.0):
    db.execute(
        "insert into positions (symbol, quantity, avg_price, realized_pnl, updated_at)"
        " values (%s, %s, %s, %s, now())"
        " on conflict (symbol) do update set quantity = excluded.quantity,"
        " avg_price = excluded.avg_price, realized_pnl = excluded.realized_pnl,"
        " updated_at = now()",
        (symbol.upper(), int(quantity), float(avg_price), float(realized_pnl)),
    )


def delete_position(symbol):
    return db.execute("delete from positions where symbol = %s", (symbol.upper(),))
