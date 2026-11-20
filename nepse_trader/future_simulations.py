"""Future-simulation date and outcome primitives.

A future simulation records a paper trade today and settles it against the
close on a later due date. This module holds the date arithmetic and the
charge-aware P&L calculation; the API owns validation, persistence and data
fetching.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .simulation import _fees

NPT = timezone(timedelta(hours=5, minutes=45))


def today_npt(now=None):
    """Return today's date in Nepal time (UTC+5:45, no DST)."""
    if now is None:
        return datetime.now(NPT).date()
    return now.astimezone(NPT).date()


def normalise_days(raw):
    """Accept an int or list of ints; return sorted, deduplicated horizons."""
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    days = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("days must be integers")
        if not 1 <= value <= 252:
            raise ValueError("days must be between 1 and 252")
        days.append(value)
    days = sorted(set(days))
    if not days or len(days) > 10:
        raise ValueError("days must contain 1 to 10 horizons")
    return days


def due_date(start, days, holidays):
    """Add calendar days, then roll forward past weekends and holidays."""
    result = start + timedelta(days=days)
    closed = set(holidays or ())
    while result.weekday() >= 5 or result.isoformat() in closed:
        result += timedelta(days=1)
    return result


def outcome(action, shares, price, exit_price, cfg):
    """Net P&L for a long buy or an already-owned sell, including charges."""
    gross = shares * (exit_price - price)
    if action == "sell":
        gross = -gross
    fees = _fees(cfg, shares * price) + _fees(cfg, shares * exit_price)
    net = gross - fees
    if net > 0:
        result = "profit"
    elif net < 0:
        result = "loss"
    else:
        result = "break_even"
    return {
        "gross_pnl": round(gross, 2),
        "fees": round(fees, 2),
        "net_pnl": round(net, 2),
        "result": result,
    }
