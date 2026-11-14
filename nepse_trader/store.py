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


# -- watchlist --------------------------------------------------------

DEFAULT_WATCHLIST = "default"


def _watchlist_id(name, create=False):
    rows = db.fetch_all("select id from watchlists where name = %s", (name,))
    if rows:
        return rows[0][0]
    if not create:
        return None
    rows = db.fetch_all(
        "insert into watchlists (name) values (%s)"
        " on conflict (name) do update set updated_at = now() returning id",
        (name,),
    )
    return rows[0][0]


def watchlist_symbols(name=DEFAULT_WATCHLIST):
    rows = db.fetch_all(
        "select s.symbol from watchlist_symbols s"
        " join watchlists w on w.id = s.watchlist_id"
        " where w.name = %s order by s.symbol",
        (name,),
    )
    return [row[0] for row in rows]


def add_watchlist_symbols(symbols, name=DEFAULT_WATCHLIST):
    watchlist_id = _watchlist_id(name, create=True)
    with db.transaction() as cur:
        for symbol in symbols:
            cur.execute(
                "insert into watchlist_symbols (watchlist_id, symbol) values (%s, %s)"
                " on conflict do nothing",
                (watchlist_id, symbol.upper()),
            )
    return watchlist_symbols(name)


def remove_watchlist_symbol(symbol, name=DEFAULT_WATCHLIST):
    watchlist_id = _watchlist_id(name)
    if watchlist_id is None:
        return False
    return (
        db.execute(
            "delete from watchlist_symbols where watchlist_id = %s and symbol = %s",
            (watchlist_id, symbol.upper()),
        )
        > 0
    )


# -- suggestion runs --------------------------------------------------


def _timestamp(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def create_run(run_id, symbols, params, idempotency_key=None, notify=False):
    db.execute(
        "insert into suggestion_runs (id, symbols, params, status, idempotency_key,"
        " notify_requested) values (%s, %s, %s, 'running', %s, %s)",
        (run_id, list(symbols), db.jsonb(params), idempotency_key, bool(notify)),
    )


def finish_run(run_id, status, error=None, result=None):
    db.execute(
        "update suggestion_runs set status = %s, error = %s, finished_at = now(),"
        " result = coalesce(%s, result) where id = %s",
        (
            status,
            (error or None) and str(error)[:1000],
            db.jsonb(result) if result is not None else None,
            run_id,
        ),
    )


def save_run_suggestions(run_id, suggestions):
    with db.transaction() as cur:
        for item in suggestions or []:
            cur.execute(
                "insert into suggestions (run_id, symbol, action, quantity,"
                " confidence, price, reasoning, signals) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    item.get("symbol"),
                    item.get("action") or "HOLD",
                    int(item.get("quantity") or 0),
                    float(item.get("confidence") or 0.0),
                    item.get("currentPrice"),
                    db.jsonb(item.get("reasoning") or []),
                    db.jsonb(item.get("signals") or {}),
                ),
            )


def get_run(run_id):
    rows = db.fetch_all(
        "select id, created_at, finished_at, symbols, params, status, error,"
        " notify_requested, result from suggestion_runs where id = %s",
        (run_id,),
    )
    if not rows:
        return None
    return _run_payload(rows[0])


def get_run_by_key(idempotency_key):
    rows = db.fetch_all(
        "select id, created_at, finished_at, symbols, params, status, error,"
        " notify_requested, result from suggestion_runs where idempotency_key = %s",
        (idempotency_key,),
    )
    if not rows:
        return None
    return _run_payload(rows[0])


def latest_run():
    rows = db.fetch_all(
        "select id, created_at, finished_at, symbols, params, status, error,"
        " notify_requested, result from suggestion_runs order by created_at desc limit 1"
    )
    if not rows:
        return None
    return _run_payload(rows[0])


def _run_payload(row):
    (
        run_id,
        created_at,
        finished_at,
        symbols,
        params,
        status,
        error,
        notify,
        result,
    ) = row
    result = result or {}
    suggestion_rows = db.fetch_all(
        "select symbol, action, quantity, confidence, price, reasoning, signals,"
        " created_at from suggestions where run_id = %s order by id",
        (run_id,),
    )
    notice = latest_notification(run_id)
    return {
        "run_id": run_id,
        "created_at": _timestamp(created_at),
        "finished_at": _timestamp(finished_at),
        "symbols": list(symbols or []),
        "params": params or {},
        "status": status,
        "error": error,
        "notify_requested": bool(notify),
        "market": result.get("market"),
        "portfolio": result.get("portfolio"),
        "disclaimer": result.get("disclaimer"),
        "notified": bool(notice and notice.get("status") == "sent"),
        "notification": notice,
        "suggestions": [
            {
                "symbol": item[0],
                "action": item[1],
                "quantity": item[2],
                "confidence": item[3],
                "currentPrice": item[4],
                "reasoning": item[5] or [],
                "signals": item[6] or {},
                "created_at": _timestamp(item[7]),
            }
            for item in suggestion_rows
        ],
    }


# -- notification log -------------------------------------------------


def log_notification(run_id, channel, status, error=None):
    db.execute(
        "insert into notification_log (run_id, channel, status, error)"
        " values (%s, %s, %s, %s)",
        (run_id, channel, status, (error or None) and str(error)[:1000]),
    )


def notification_sent(run_id, channel):
    rows = db.fetch_all(
        "select 1 from notification_log where run_id = %s and channel = %s"
        " and status = 'sent' limit 1",
        (run_id, channel),
    )
    return bool(rows)


def latest_notification(run_id):
    rows = db.fetch_all(
        "select channel, status, error, sent_at from notification_log"
        " where run_id = %s order by id desc limit 1",
        (run_id,),
    )
    if not rows:
        return None
    channel, status, error, sent_at = rows[0]
    return {
        "channel": channel,
        "status": status,
        "error": error,
        "sent_at": _timestamp(sent_at),
    }
