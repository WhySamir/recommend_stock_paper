"""Paper-trading portfolio: cash plus the positions held.

Cash is ``config.account.cash`` — the same number ``budget`` sizes against.
Positions live in Neon (``positions``) when ``NEON_CONN_URL`` is set, otherwise
in ``state_dir/positions.json``. This module never places an order.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import store


def path_for(cfg):
    return Path(cfg["state_dir"]) / "positions.json"


def load_positions(cfg):
    if store.backend() == "postgres":
        return store.load_positions()
    path = path_for(cfg)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("symbol")]


def save_positions(cfg, positions):
    if store.backend() == "postgres":
        for row in positions:
            store.save_position(
                row["symbol"],
                int(row.get("quantity") or 0),
                float(row.get("avg_price") or 0),
                float(row.get("realized_pnl") or 0),
            )
        return
    path = path_for(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(positions, indent=2) + "\n", encoding="utf-8")


def set_position(cfg, symbol, quantity, avg_price, realized_pnl=0.0):
    symbol = symbol.upper()
    if store.backend() == "postgres":
        if int(quantity) <= 0:
            store.delete_position(symbol)
            return
        store.save_position(symbol, quantity, avg_price, realized_pnl)
        return
    positions = [row for row in load_positions(cfg) if row["symbol"].upper() != symbol]
    if int(quantity) > 0:
        positions.append(
            {
                "symbol": symbol,
                "quantity": int(quantity),
                "avg_price": float(avg_price),
                "realized_pnl": float(realized_pnl),
            }
        )
    save_positions(cfg, positions)


def snapshot(cfg, prices=None, source=None):
    """Cash, positions and unrealized P&L marked at the latest known price."""
    prices = prices or {}
    rows = []
    market_value = 0.0
    unrealized = 0.0
    realized = 0.0
    for raw in load_positions(cfg):
        symbol = str(raw["symbol"]).upper()
        quantity = int(raw.get("quantity") or 0)
        avg_price = float(raw.get("avg_price") or 0)
        last = prices.get(symbol)
        if last is None and source is not None:
            try:
                last = source.last(symbol)[0]
            except Exception:
                last = None
        price = float(last) if last else avg_price
        value = quantity * price
        pnl = quantity * (price - avg_price) if avg_price else 0.0
        market_value += value
        unrealized += pnl
        realized += float(raw.get("realized_pnl") or 0)
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "avg_price": round(avg_price, 2),
                "last_price": round(price, 2),
                "market_value": round(value, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(100.0 * (price / avg_price - 1), 2)
                if avg_price
                else 0.0,
                "realized_pnl": round(float(raw.get("realized_pnl") or 0), 2),
            }
        )
    cash = float(cfg["account"]["cash"])
    return {
        "cash": round(cash, 2),
        "positions": rows,
        "position_value": round(market_value, 2),
        "portfolio_value": round(cash + market_value, 2),
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl": round(realized, 2),
        "total_pnl": round(unrealized + realized, 2),
    }
