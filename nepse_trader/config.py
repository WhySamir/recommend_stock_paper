"""Configuration loading with NEPSE defaults."""

from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULTS = {
    "timezone": "Asia/Kathmandu",
    "universe": [],
    "session": {"open": "11:00", "close": "15:00", "preopen": "10:30"},
    "market": {
        "lot_size": 10,
        "price_band_pct": 15.0,
        "preopen_band_pct": 5.0,
        "settlement_days": 2,
        "short_selling": False,
        "intraday": False,
    },
    "account": {
        "cash": 100000.0,
        "max_loss_per_trade": 2000.0,
        "max_loss_per_day": 4000.0,
        "max_trades_per_day": 3,
        "min_reward_risk": 1.5,
    },
    "charges": {"broker_pct": 0.36, "sebon_pct": 0.015, "dp_charge": 25.0},
    "scan": {
        "breakout_lookback": 20,
        "volume_lookback": 20,
        "min_volume_ratio": 1.2,
        "flag_bars": 3,
        "flag_max_range_atr": 1.4,
        "flag_min_prior_move_pct": 3.0,
        "bounce_rsi": 15.0,
    },
    "plan": {"target_r": 2.5, "atr_mult": 2.0, "max_hold_days": 10},
    "timemachine": {"iterations": 10000, "seed": 19, "lookback": 250},
    "suggest": {
        "lookback_days": 120,
        "risk_per_trade_pct": 1.0,
        "max_position_pct": 20.0,
        "max_positions": 5,
        "min_confidence": 0.55,
        "buy_threshold": 0.25,
        "sell_threshold": -0.25,
        "sell_fraction": 1.0,
        "atr_mult": 2.0,
        "target_r": 2.5,
        "min_history_trades": 5,
        "weights": {
            "trend": 0.25,
            "ema_cross": 0.15,
            "macd": 0.20,
            "rsi": 0.15,
            "momentum": 0.25,
        },
    },
    "data": {
        "kind": "csv",
        "dir": "data",
        "cache_dir": "data/cache",
        "cache_ttl_minutes": 60,
        "api": {
            "history_url": "",
            "quote_url": "",
            "headers": {},
            "records_path": "data",
            "fields": {
                "date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            },
            "quote_fields": {"last": "lastTradedPrice", "date": "date"},
            "symbol_map": {},
            "timeout": 20,
        },
        "official": {
            "base_url": "https://www.nepalstock.com.np",
            "history_days": 400,
            "security_map_ttl_hours": 24,
            "token_ttl_seconds": 35,
            "min_interval": 0.25,
            "timeout": 30,
            "verify_tls": True,
        },
    },
    "holidays": [],
    "state_dir": "trader",
    "api": {
        "cors_origins": [
            "http://localhost:3000",
        ],
    },
}


def merge(base, override):
    """Recursively merge ``override`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError("config not found: %s" % p)
        cfg = merge(cfg, json.loads(p.read_text(encoding="utf-8")))
    return cfg
