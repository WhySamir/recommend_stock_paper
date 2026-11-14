"""Small indicator helpers. Last-value only; bars are daily OHLCV dicts."""

from __future__ import annotations


def sma_last(closes, period):
    if period <= 0 or len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def avg_volume_last(bars, period):
    if len(bars) < period:
        return None
    total = sum(b["volume"] for b in bars[-period:])
    return total / period


def atr_last(bars, period=14):
    """Wilder's average true range."""
    if len(bars) < period + 1:
        return None
    ranges = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(ranges[:period]) / period
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def rsi_last(closes, period=14):
    """Wilder's RSI over the available history."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period
    for delta in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
