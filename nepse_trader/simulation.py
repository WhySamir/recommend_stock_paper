"""Custom paper-trade simulations.

This module deliberately does not write to the market-data cache.  A simulation
is a separate scenario which is saved by the API under ``state_dir``.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone


def _fees(cfg, turnover):
    charges = cfg["charges"]
    return turnover * (float(charges["broker_pct"]) + float(charges["sebon_pct"])) / 100.0 + float(
        charges["dp_charge"]
    )


def run(symbol, bars, action, shares, days, entry_price, iterations, seed, cfg):
    """Forecast a custom long buy or an already-owned sell.

    Daily close-to-close returns are bootstrapped from the cached bars.  This
    gives a range of paper outcomes rather than pretending that a future price
    is known.  ``sell`` means selling shares already owned; NEPSE short selling
    is not assumed.
    """
    returns = []
    for previous, current in zip(bars, bars[1:]):
        if previous["close"] > 0:
            returns.append(current["close"] / previous["close"] - 1.0)
    if not returns:
        raise ValueError("not enough cached history to simulate %s" % symbol)

    rng = random.Random(seed)
    results = []
    fee_rate = (float(cfg["charges"]["broker_pct"]) + float(cfg["charges"]["sebon_pct"])) / 100.0
    for _ in range(iterations):
        exit_price = float(entry_price)
        for _day in range(days):
            exit_price *= 1.0 + rng.choice(returns)
        buy_turnover = shares * entry_price
        sell_turnover = shares * exit_price
        fees = _fees(cfg, buy_turnover) + _fees(cfg, sell_turnover)
        gross = shares * (exit_price - entry_price)
        if action == "sell":
            gross = -gross
        net = gross - fees
        results.append({"exit_price": round(exit_price, 2), "gross_pnl": round(gross, 2), "fees": round(fees, 2), "net_pnl": round(net, 2)})

    net_values = sorted(item["net_pnl"] for item in results)
    positive = sum(1 for value in net_values if value > 0)
    negative = sum(1 for value in net_values if value < 0)
    mean = sum(net_values) / len(net_values)
    percentile = lambda fraction: net_values[min(len(net_values) - 1, int((len(net_values) - 1) * fraction))]
    return {
        "symbol": symbol.upper(),
        "action": action,
        "shares": shares,
        "days": days,
        "entry_price": round(entry_price, 2),
        "as_of": bars[-1]["date"],
        "history_bars": len(bars),
        "sessions_bootstrapped": len(returns),
        "iterations": iterations,
        "seed": seed,
        "forecast": {
            "expected_exit_price": round(sum(item["exit_price"] for item in results) / len(results), 2),
            "expected_net_pnl": round(mean, 2),
            "best_net_pnl": round(net_values[-1], 2),
            "worst_net_pnl": round(net_values[0], 2),
            "p10_net_pnl": round(percentile(0.10), 2),
            "p50_net_pnl": round(percentile(0.50), 2),
            "p90_net_pnl": round(percentile(0.90), 2),
            "profit_probability_pct": round(100.0 * positive / len(results), 1),
            "loss_probability_pct": round(100.0 * negative / len(results), 1),
        },
        "sample_outcomes": results[:25],
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "caveat": "Forecast only: future prices are bootstrapped from cached daily close returns. It is not a prediction or a broker order. Sell assumes you already own the shares; fees are estimated from config.",
    }
