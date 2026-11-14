"""Time Machine, daily-bar edition.

1. History: replay the setup rule on every past bar and measure what the
   next ``max_hold_days`` sessions actually did.
2. Simulation: bootstrap the symbol's own historical sessions to replay the
   current trade thousands of times.

Daily bars cannot say whether the stop or the target printed first inside a
day, so a bar that touches both is counted as a stop. That keeps the odds
honest rather than flattering.
"""

from __future__ import annotations

import random

from . import scan
from .plan import build_plan


def _simulate_forward(bars, start, stop, target, max_hold):
    """Walk sessions after ``start``. Returns (outcome, r_multiple, sessions)."""
    entry = bars[start]["open"] or bars[start]["close"]
    risk = entry - stop
    if risk <= 0:
        return "invalid", 0.0, 0
    for offset in range(1, max_hold + 1):
        index = start + offset
        if index >= len(bars):
            break
        bar = bars[index]
        if bar["low"] <= stop:
            return "stop", -1.0, offset
        if bar["high"] >= target:
            return "target", (target - entry) / risk, offset
    last = min(start + max_hold, len(bars) - 1)
    return "timeout", (bars[last]["close"] - entry) / risk, last - start


def history(symbol, bars, cfg):
    """Every past firing of the setup, with the outcome that followed."""
    params = cfg["plan"]
    start = scan.min_bars(cfg)
    trades = []
    for index in range(start - 1, len(bars) - 1):
        candidate = scan.evaluate(symbol, bars[: index + 1], cfg)
        if not candidate:
            continue
        entry_bar = bars[index + 1]
        signal = dict(candidate[0])
        signal["close"] = entry_bar["open"] or entry_bar["close"]
        plan = build_plan(signal, cfg)
        if not plan:
            continue
        outcome, r_multiple, sessions = _simulate_forward(
            bars, index + 1, plan["stop"], plan["target"], params["max_hold_days"]
        )
        trades.append(
            {
                "signal_date": signal["date"],
                "entry_date": entry_bar["date"],
                "setup": signal["setup"],
                "entry": plan["entry"],
                "stop": plan["stop"],
                "target": plan["target"],
                "outcome": outcome,
                "r": round(r_multiple, 2),
                "sessions": sessions,
            }
        )

    total = len(trades)
    wins = sum(1 for t in trades if t["outcome"] == "target")
    stops = sum(1 for t in trades if t["outcome"] == "stop")
    timeouts = sum(1 for t in trades if t["outcome"] == "timeout")
    return {
        "firings": total,
        "target_first": wins,
        "stop_first": stops,
        "timeout": timeouts,
        "hit_rate_pct": round(100.0 * wins / total, 1) if total else 0.0,
        "expectancy_r": round(sum(t["r"] for t in trades) / total, 2) if total else 0.0,
        "trades": trades,
    }


def monte_carlo(bars, plan, cfg):
    """Bootstrap the symbol's own sessions into ``iterations`` replays."""
    tm = cfg["timemachine"]
    iterations = int(tm["iterations"])
    max_hold = int(plan["max_hold_days"])
    lookback = int(tm["lookback"])
    rng = random.Random(tm["seed"])

    window = bars[-lookback:]
    sessions = []
    for index in range(1, len(window)):
        prev = window[index - 1]["close"]
        if prev <= 0:
            continue
        sessions.append(
            (
                window[index]["high"] / prev - 1.0,
                window[index]["low"] / prev - 1.0,
                window[index]["close"] / prev - 1.0,
            )
        )
    if not sessions:
        return {"iterations": 0, "error": "not enough history to bootstrap"}

    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = entry - stop
    tally = {"target": 0, "stop": 0, "timeout": 0}
    r_total = 0.0
    for _ in range(iterations):
        price = entry
        outcome = "timeout"
        for _day in range(max_hold):
            up, down, close_move = rng.choice(sessions)
            if price * (1 + down) <= stop:
                outcome = "stop"
                r_total += -1.0
                break
            if price * (1 + up) >= target:
                outcome = "target"
                r_total += (target - entry) / risk
                break
            price = price * (1 + close_move)
        else:
            r_total += (price - entry) / risk
        tally[outcome] += 1

    total = float(iterations)
    return {
        "iterations": iterations,
        "seed": tm["seed"],
        "sessions_bootstrapped": len(sessions),
        "target_first_pct": round(100.0 * tally["target"] / total, 1),
        "stop_first_pct": round(100.0 * tally["stop"] / total, 1),
        "timeout_pct": round(100.0 * tally["timeout"] / total, 1),
        "expectancy_r": round(r_total / total, 2),
    }


def run(symbol, bars, plan, cfg):
    return {
        "symbol": symbol.upper(),
        "as_of": bars[-1]["date"],
        "plan": plan,
        "history": history(symbol, bars, cfg),
        "simulation": monte_carlo(bars, plan, cfg),
        "caveat": (
            "Simulation, not a prediction. Daily NEPSE bars cannot resolve "
            "whether a stop or target printed first inside a session, so a bar "
            "touching both is scored as a stop. Gaps and circuit halts can fill "
            "far worse than the stop price."
        ),
    }
