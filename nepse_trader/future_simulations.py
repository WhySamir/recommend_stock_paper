"""Future-simulation date and outcome primitives.

A future simulation records a paper trade today and settles it against the
close on a later due date. This module holds the date arithmetic and the
charge-aware P&L calculation; the API owns validation, persistence and data
fetching.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import simulation as simulation_mod
from .simulation import _fees

CAVEAT = (
    "Forecast only until settled; the settled result uses the close on the "
    "stored due date and the charges in the saved record. It is not a broker "
    "order or a prediction."
)
FORECAST_KEYS = (
    "expected_net_pnl",
    "best_net_pnl",
    "worst_net_pnl",
    "profit_probability_pct",
)

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


def _stamp(now):
    """UTC timestamp in the project's Z-suffixed ISO format."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_record(symbol, action, shares, price, days, cfg, bars, iterations=1000, seed=None, now=None):
    """Build an unsaved ledger record with one forecast per horizon."""
    start = today_npt(now)
    holidays = cfg.get("holidays") or ()
    seed = cfg["timemachine"]["seed"] if seed is None else seed
    horizons = []
    for count in normalise_days(days):
        due = due_date(start, count, holidays)
        try:
            result = simulation_mod.run(
                symbol, bars, action, shares, count, price, iterations, seed, cfg
            )
            forecast = {key: result["forecast"][key] for key in FORECAST_KEYS}
        except ValueError:
            # A brand-new symbol may not have enough cached bars to bootstrap
            # a forecast; the paper trade can still be logged and settled.
            forecast = {key: None for key in FORECAST_KEYS}
        horizons.append(
            {
                "days": count,
                "due_date": due.isoformat(),
                "status": "pending",
                "forecast": forecast,
            }
        )
    return {
        "symbol": symbol.upper(),
        "action": action,
        "shares": shares,
        "price": round(float(price), 2),
        "created_date": start.isoformat(),
        "charges": dict(cfg["charges"]),
        "horizons": horizons,
        "status": "pending",
        "generated_at": _stamp(now),
        "caveat": CAVEAT,
    }


def settle(record, bars, now=None):
    """Settle each due horizon once, against the close on its own due date."""
    today = today_npt(now)
    settled_at = _stamp(now)
    by_date = {bar["date"]: bar for bar in bars}
    last_bar_date = bars[-1]["date"] if bars else ""
    settled = 0
    for horizon in record["horizons"]:
        if horizon.get("status") == "settled":
            settled += 1
            continue
        due = horizon["due_date"]
        horizon.pop("reason", None)
        horizon.pop("last_bar_date", None)
        if date.fromisoformat(due) > today:
            horizon["status"] = "pending"
            horizon["reason"] = "scheduled"
            continue
        bar = by_date.get(due)
        if bar is None:
            horizon["status"] = "pending"
            horizon["reason"] = "due-date bar not available"
            horizon["last_bar_date"] = last_bar_date
            continue
        exit_price = float(bar["close"])
        values = outcome(
            record["action"],
            record["shares"],
            record["price"],
            exit_price,
            {
                "charges": record.get("charges")
                or {"broker_pct": 0.36, "sebon_pct": 0.015, "dp_charge": 25.0}
            },
        )
        horizon.update(values)
        horizon["exit_price"] = round(exit_price, 2)
        horizon["exit_date"] = due
        horizon["settled_at"] = settled_at
        horizon["status"] = "settled"
        settled += 1
    if settled == len(record["horizons"]):
        record["status"] = "settled"
    elif settled:
        record["status"] = "partial"
    else:
        record["status"] = "pending"
    return record
