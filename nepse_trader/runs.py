"""Suggestion runs: validation, persistence and optional notification.

This is the service layer behind ``POST /api/suggestions``. Runs, suggestions
and the watchlist live in Neon — the endpoint returns 503 when
``NEON_CONN_URL`` is unset, because there is nowhere durable to put them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import db
from . import store
from . import suggestions

MAX_SYMBOLS = 50
MAX_IDEMPOTENCY_KEY = 200
CHANNEL = "discord"


class RunError(ValueError):
    pass


def normalize(symbols):
    if not isinstance(symbols, (list, tuple)):
        raise RunError("symbols must be a list of tickers")
    names = []
    for raw in symbols:
        if not isinstance(raw, str):
            raise RunError("every symbol must be a string")
        name = raw.strip().upper()
        if not name:
            continue
        if not suggestions.SYMBOL_RE.match(name):
            raise RunError(
                "invalid symbol %r (letters, numbers, &, _, ., - only)" % raw
            )
        if name not in names:
            names.append(name)
    if len(names) > MAX_SYMBOLS:
        raise RunError("too many symbols (max %d per run)" % MAX_SYMBOLS)
    return names


def watchlist():
    require_db()
    return store.watchlist_symbols()


def add_symbols(symbols):
    require_db()
    names = normalize(symbols)
    if not names:
        raise RunError("no symbols to add")
    return store.add_watchlist_symbols(names)


def remove_symbol(symbol):
    require_db()
    name = normalize([symbol])
    if not name:
        raise RunError("symbol is required")
    return store.remove_watchlist_symbol(name[0])


def require_db():
    if not db.enabled():
        raise db.DbError(
            "NEON_CONN_URL is not set; suggestion runs and the watchlist need "
            "the Neon store"
        )


def _run_id():
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )


def _notify(cfg, run_id, suggestions_list):
    """Best-effort Discord delivery; never raises into the API response.

    Phase 4 adds ``nepse_trader/discord.py``. Until it is present (or when
    ``DISCORD_WEBHOOK_URL`` is unset) the run is marked as skipped, so a cron
    job still gets its suggestions back.
    """
    try:
        from . import discord
    except ImportError:
        result = {
            "status": "skipped",
            "reason": "Discord notifications are not configured",
            "sent": False,
        }
    else:
        try:
            if store.notification_sent(run_id, CHANNEL):
                return {"status": "skipped", "reason": "already sent for this run"}
            result = discord.send_run(cfg, run_id, suggestions_list)
        except Exception as exc:
            result = {"status": "error", "reason": str(exc)[:300], "sent": False}

    try:
        store.log_notification(
            run_id,
            CHANNEL,
            result.get("status") or "error",
            result.get("reason"),
        )
    except Exception:
        pass  # the log is a nice-to-have; never fail the run over it
    return result


def execute(cfg, symbols=None, notify=False, idempotency_key=None):
    require_db()
    if idempotency_key and len(idempotency_key) > MAX_IDEMPOTENCY_KEY:
        raise RunError("Idempotency-Key is too long (max %d chars)" % MAX_IDEMPOTENCY_KEY)
    if idempotency_key:
        existing = store.get_run_by_key(idempotency_key)
        if existing:
            return dict(existing, idempotent=True)

    names = normalize(symbols) if symbols else store.watchlist_symbols()
    if not names:
        raise RunError(
            "no symbols: pass symbols in the body or add them to the watchlist"
        )

    run_id = _run_id()
    params = suggestions.params(cfg)
    try:
        store.create_run(run_id, names, params, idempotency_key, notify)
    except db.DbError:
        # Another request won the race on the same Idempotency-Key.
        existing = idempotency_key and store.get_run_by_key(idempotency_key)
        if existing:
            return dict(existing, idempotent=True)
        raise

    try:
        result = suggestions.generate(cfg, names)
    except Exception as exc:
        store.finish_run(run_id, "error", exc)
        raise

    store.save_run_suggestions(run_id, result["suggestions"])
    if notify:
        _notify(cfg, run_id, result["suggestions"])
    store.finish_run(
        run_id,
        "ok",
        result={
            "market": result["market"],
            "portfolio": result["portfolio"],
            "disclaimer": result["disclaimer"],
        },
    )
    return dict(store.get_run(run_id), idempotent=False)


def latest():
    require_db()
    return store.latest_run()
