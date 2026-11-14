"""Setup scanner over daily NEPSE bars.

Three setups, all evaluated on the last completed session:

* breakout - close above the prior N-day high on above-average volume
* flag     - tight pause after an up move, close above the pause
* bounce   - short-term oversold (RSI2) then an up day, above the 50-day mean
"""

from __future__ import annotations

from . import indicators as ind


def _pct(value, base):
    return 0.0 if not base else (value / base - 1.0) * 100.0


def min_bars(cfg):
    scan = cfg["scan"]
    return max(
        scan["breakout_lookback"] + 2,
        scan["volume_lookback"] + 2,
        scan["flag_bars"] + 12,
        16,
    )


def evaluate(symbol, bars, cfg):
    """Return every setup that fired on the final bar of ``bars``."""
    scan = cfg["scan"]
    if len(bars) < min_bars(cfg):
        return []

    closes = [b["close"] for b in bars]
    today = bars[-1]
    prev_close = bars[-2]["close"]
    atr = ind.atr_last(bars, 14)
    avg_volume = ind.avg_volume_last(bars[:-1], scan["volume_lookback"])
    if not atr or atr <= 0:
        return []
    volume_ratio = (today["volume"] / avg_volume) if avg_volume else 0.0
    band = cfg["market"]["price_band_pct"] / 100.0
    base = {
        "symbol": symbol.upper(),
        "date": today["date"],
        "close": round(today["close"], 2),
        "prev_close": round(prev_close, 2),
        "atr": round(atr, 2),
        "volume": int(today["volume"]),
        "volume_ratio": round(volume_ratio, 2),
        "band_upper": round(prev_close * (1 + band), 2),
        "band_lower": round(prev_close * (1 - band), 2),
        "needs_intraday": False,
    }
    found = []

    look = scan["breakout_lookback"]
    window = bars[-look - 1 : -1]
    prior_high = max(b["high"] for b in window)
    base_low = min(b["low"] for b in window)
    if today["close"] > prior_high and volume_ratio >= scan["min_volume_ratio"]:
        found.append(
            dict(
                base,
                setup="breakout",
                trigger=round(prior_high, 2),
                setup_low=round(base_low, 2),
                detail="closed %.2f%% above the %d-day high on %.2fx volume"
                % (_pct(today["close"], prior_high), look, volume_ratio),
            )
        )

    flag_bars = scan["flag_bars"]
    if len(bars) >= flag_bars + 11:
        body = bars[-flag_bars - 1 : -1]
        body_high = max(b["high"] for b in body)
        body_low = min(b["low"] for b in body)
        anchor = bars[-flag_bars - 6]["close"]
        prior_move = _pct(body[-1]["close"], anchor)
        tight = (body_high - body_low) <= scan["flag_max_range_atr"] * atr
        if (
            tight
            and today["close"] > body_high
            and prior_move >= scan["flag_min_prior_move_pct"]
        ):
            found.append(
                dict(
                    base,
                    setup="flag",
                    trigger=round(body_high, 2),
                    setup_low=round(body_low, 2),
                    detail="broke a %d-bar pause after a %.1f%% run"
                    % (flag_bars, prior_move),
                )
            )

    rsi2 = ind.rsi_last(closes[:-1], 2)
    sma50 = ind.sma_last(closes, 50)
    if (
        rsi2 is not None
        and rsi2 < scan["bounce_rsi"]
        and today["close"] > prev_close
        and (sma50 is None or today["close"] > sma50)
    ):
        found.append(
            dict(
                base,
                setup="bounce",
                trigger=round(today["close"], 2),
                setup_low=round(min(b["low"] for b in bars[-3:]), 2),
                detail="RSI2 was %.0f, then closed up on the day" % rsi2,
                rsi2=round(rsi2, 1),
            )
        )

    return found


def entry_status(symbol, bars, cfg, setup=None):
    """WAITING / TRIGGERED / EXPIRED for the latest bar."""
    hits = evaluate(symbol, bars, cfg)
    if setup:
        hits = [hit for hit in hits if hit["setup"] == setup]
    if hits:
        return "TRIGGERED", hits
    return "WAITING", []
