"""BUY / SELL / HOLD suggestions from the engine's own capabilities.

One suggestion per symbol, built from three inputs:

1. **Technical score** over daily bars — SMA/EMA trend, MACD, RSI extremes,
   risk-adjusted momentum, ATR volatility. Weights and thresholds live in
   ``config.suggest`` and can be overridden by ``SUGGEST_*`` environment vars.
2. **Time Machine** — the same score is replayed over the symbol's past bars and
   the realised expectancy / win rate / drawdown scale the confidence. A
   strategy that lost money historically produces a lower-confidence signal.
3. **Paper portfolio** — cash, open positions and P&L size every BUY (risk per
   trade, concentration cap, whole board lots) and cap every SELL at the shares
   actually held. NEPSE is long-only, so a negative score without a position is
   HOLD, never a short.

The module is separate from the HTTP layer: ``generate(cfg, symbols)`` returns
plain dicts. One failing symbol never fails the batch.
"""

from __future__ import annotations

import copy
import math
import os
import re
from datetime import datetime, time, timedelta, timezone

from . import data as data_mod
from . import indicators as ind
from . import plan as plan_mod
from . import portfolio as portfolio_mod
from . import timemachine as tm_mod

MIN_BARS = 60
SYMBOL_RE = re.compile(r"^[A-Z0-9&_.-]{1,24}$")
NPT = timezone(timedelta(hours=5, minutes=45))

ENV_OVERRIDES = {
    "risk_per_trade_pct": ("SUGGEST_RISK_PER_TRADE_PCT", float),
    "max_position_pct": ("SUGGEST_MAX_POSITION_PCT", float),
    "max_positions": ("SUGGEST_MAX_POSITIONS", int),
    "min_confidence": ("SUGGEST_MIN_CONFIDENCE", float),
    "lookback_days": ("SUGGEST_LOOKBACK_DAYS", int),
    "buy_threshold": ("SUGGEST_BUY_THRESHOLD", float),
    "sell_threshold": ("SUGGEST_SELL_THRESHOLD", float),
    "atr_mult": ("SUGGEST_ATR_MULT", float),
    "target_r": ("SUGGEST_TARGET_R", float),
    "concurrency": ("SUGGEST_CONCURRENCY", int),
}


def params(cfg):
    """config.suggest with SUGGEST_* environment overrides."""
    out = copy.deepcopy(cfg.get("suggest") or {})
    for key, default in (
        ("lookback_days", 120),
        ("risk_per_trade_pct", 1.0),
        ("max_position_pct", 20.0),
        ("max_positions", 5),
        ("min_confidence", 0.55),
        ("buy_threshold", 0.25),
        ("sell_threshold", -0.25),
        ("sell_fraction", 1.0),
        ("atr_mult", 2.0),
        ("target_r", 2.5),
        ("min_history_trades", 5),
        ("concurrency", 4),
    ):
        out.setdefault(key, default)
    out.setdefault(
        "weights",
        (cfg.get("suggest") or {}).get("weights")
        or {"trend": 0.25, "ema_cross": 0.15, "macd": 0.20, "rsi": 0.15, "momentum": 0.25},
    )
    for key, (name, cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            out[key] = cast(raw)
        except ValueError:
            continue
    out["max_hold_days"] = int(cfg["plan"]["max_hold_days"])
    out["momentum_lookback"] = 20
    return out


def market_state(cfg, now=None):
    """NEPSE session state in Asia/Kathmandu (UTC+5:45, no DST)."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(NPT)
    session = cfg.get("session") or {}

    def _clock(name, fallback):
        try:
            hour, minute = str(session.get(name) or fallback).split(":")[:2]
            return time(int(hour), int(minute))
        except (TypeError, ValueError):
            return time(*[int(part) for part in fallback.split(":")])

    opens, closes = _clock("open", "11:00"), _clock("close", "15:00")
    weekday = local.weekday() < 5
    holiday = local.date().isoformat() in set(cfg.get("holidays") or [])
    is_open = weekday and not holiday and opens <= local.time() <= closes
    if holiday:
        status = "closed (holiday)"
    elif not weekday:
        status = "closed (weekend)"
    elif is_open:
        status = "open"
    else:
        status = "closed (outside 11:00-15:00 NPT)"
    return {
        "open": is_open,
        "status": status,
        "local_time": local.replace(microsecond=0).isoformat(),
        "as_of": local.date().isoformat(),
    }


def _clip(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def technical_score(bars, params):
    """Directional score in [-1, 1] plus the raw indicator values."""
    closes = [bar["close"] for bar in bars]
    price = closes[-1]
    sma20 = ind.sma_last(closes, 20)
    sma50 = ind.sma_last(closes, 50)
    ema12 = ind.ema_last(closes, 12)
    ema26 = ind.ema_last(closes, 26)
    macd = ind.macd_last(closes)
    rsi = ind.rsi_last(closes, 14)
    atr = ind.atr_last(bars, 14)
    returns = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ][-20:]
    daily_vol = ind.stdev_last(returns, len(returns)) if len(returns) >= 5 else None
    look = int(params["momentum_lookback"])
    momentum = price / closes[-1 - look] - 1.0 if len(closes) > look else None

    components = {}
    notes = []

    trend = 0.0
    if sma20 and sma50 and sma20 > 0 and sma50 > 0:
        trend = 0.7 * _clip((sma20 / sma50 - 1.0) / 0.05) + 0.3 * _clip(
            (price / sma20 - 1.0) / 0.03
        )
        notes.append(
            "SMA20 %.2f vs SMA50 %.2f (%+.1f%%) with price %+.1f%% vs SMA20: "
            "trend score %+.2f"
            % (
                sma20,
                sma50,
                (sma20 / sma50 - 1.0) * 100,
                (price / sma20 - 1.0) * 100,
                trend,
            )
        )
    components["trend"] = _clip(trend)

    ema_cross = 0.0
    if ema12 and ema26 and ema26 > 0:
        ema_cross = _clip((ema12 / ema26 - 1.0) / 0.03)
        notes.append(
            "EMA12 %.2f vs EMA26 %.2f: %s"
            % (ema12, ema26, "bullish cross" if ema_cross >= 0 else "bearish cross")
        )
    components["ema_cross"] = _clip(ema_cross)

    macd_score = 0.0
    if macd and atr and atr > 0:
        macd_score = _clip(macd["histogram"] / (0.5 * atr))
        notes.append(
            "MACD histogram %+.2f: %s momentum"
            % (macd["histogram"], "positive" if macd_score >= 0 else "negative")
        )
    components["macd"] = _clip(macd_score)

    rsi_score = 0.0
    if rsi is not None:
        if rsi <= 30:
            rsi_score = _clip((30 - rsi) / 15.0)
        elif rsi >= 70:
            rsi_score = -_clip((rsi - 70) / 15.0)
        notes.append(
            "RSI14 %.1f: %s"
            % (
                rsi,
                "oversold" if rsi <= 30 else "overbought" if rsi >= 70 else "neutral",
            )
        )
    components["rsi"] = _clip(rsi_score)

    momentum_score = 0.0
    if momentum is not None and daily_vol and daily_vol > 0:
        momentum_score = _clip(momentum / (daily_vol * math.sqrt(look)))
        notes.append(
            "20-session momentum %+.1f%% against %.1f%% daily volatility"
            % (momentum * 100, daily_vol * 100)
        )
    components["momentum"] = _clip(momentum_score)

    weights = params.get("weights") or {}
    total_weight = sum(abs(float(weights.get(key, 0))) for key in components) or 1.0
    score = sum(
        float(weights.get(key, 0)) * value for key, value in components.items()
    ) / total_weight

    return _clip(score), {
        "score": round(_clip(score), 4),
        "components": {key: round(value, 4) for key, value in components.items()},
        "weights": weights,
        "indicators": {
            "close": round(price, 2),
            "sma20": round(sma20, 2) if sma20 else None,
            "sma50": round(sma50, 2) if sma50 else None,
            "ema12": round(ema12, 2) if ema12 else None,
            "ema26": round(ema26, 2) if ema26 else None,
            "macd": round(macd["macd"], 2) if macd else None,
            "macd_signal": round(macd["signal"], 2) if macd else None,
            "macd_histogram": round(macd["histogram"], 2) if macd else None,
            "rsi14": round(rsi, 1) if rsi is not None else None,
            "atr14": round(atr, 2) if atr else None,
            "momentum_20d_pct": round(momentum * 100, 2)
            if momentum is not None
            else None,
            "daily_volatility_pct": round(daily_vol * 100, 2) if daily_vol else None,
        },
        "notes": notes,
    }


def backtest(bars, params):
    """Replay the score over the symbol's own past bars (long side only)."""
    trades = []
    for index in range(MIN_BARS, len(bars) - 1):
        window = bars[: index + 1]
        score, _ = technical_score(window, params)
        if score < float(params["buy_threshold"]):
            continue
        atr = ind.atr_last(window, 14)
        entry_bar = bars[index + 1]
        entry = entry_bar["open"] or entry_bar["close"]
        if not atr or atr <= 0 or entry <= 0:
            continue
        stop = entry - float(params["atr_mult"]) * atr
        if stop >= entry:
            continue
        target = entry + float(params["target_r"]) * (entry - stop)
        outcome, r_value, sessions = tm_mod._simulate_forward(
            bars, index + 1, stop, target, int(params["max_hold_days"])
        )
        if outcome == "invalid":
            continue
        trades.append(
            {"outcome": outcome, "r": r_value, "sessions": sessions, "score": score}
        )

    total = len(trades)
    wins = sum(1 for trade in trades if trade["r"] > 0)
    equity = peak = drawdown = 0.0
    for trade in trades:
        equity += trade["r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": total,
        "wins": wins,
        "win_rate_pct": round(100.0 * wins / total, 1) if total else 0.0,
        "expectancy_r": round(sum(t["r"] for t in trades) / total, 2) if total else 0.0,
        "total_r": round(equity, 2),
        "max_drawdown_r": round(drawdown, 2),
        "avg_sessions": round(sum(t["sessions"] for t in trades) / total, 1)
        if total
        else 0.0,
    }


def _confidence(score, stats, params):
    """Signal strength scaled by how the same signal actually performed."""
    threshold = max(0.01, abs(float(params["buy_threshold"])) * 2.0)
    strength = _clip(abs(score) / threshold, 0.0, 1.0)
    multiplier = 1.0
    if stats["trades"] >= int(params["min_history_trades"]):
        multiplier += 0.15 * _clip(stats["expectancy_r"] / 2.0, -2.0, 2.0)
        multiplier += 0.20 * ((stats["win_rate_pct"] - 50.0) / 50.0)
        multiplier -= 0.10 * _clip(stats["max_drawdown_r"] / 10.0, 0.0, 1.0)
        multiplier = _clip(multiplier, 0.6, 1.4)
    return round(_clip(strength * multiplier, 0.0, 1.0), 3), round(multiplier, 2)


def _plan_for(price, atr, params):
    risk = float(params["atr_mult"]) * atr
    stop = price - risk
    if risk <= 0 or stop <= 0:
        return None
    return {
        "entry": round(price, 2),
        "stop": round(stop, 2),
        "target": round(price + float(params["target_r"]) * risk, 2),
        "risk_per_share": round(risk, 2),
        "reward_per_share": round(float(params["target_r"]) * risk, 2),
        "max_hold_days": int(params["max_hold_days"]),
        "band_upper": 0.0,
    }


def _size_buy(cfg, params, price, atr, portfolio_value, cash, existing_value):
    """Risk-sized, cash-capped, concentration-capped, whole board lots."""
    trade = _plan_for(price, atr, params)
    if trade is None:
        return {"shares": 0, "lots": 0, "cash_used": 0.0, "reason": "ATR is zero"}
    local = copy.deepcopy(cfg)
    local["account"]["cash"] = max(0.0, cash)
    local["account"]["max_loss_per_trade"] = min(
        float(cfg["account"]["max_loss_per_trade"]),
        portfolio_value * float(params["risk_per_trade_pct"]) / 100.0,
    )
    size = plan_mod.size_position(trade, local)
    shares = int(size.get("shares") or 0)

    lot = int(cfg["market"]["lot_size"])
    room = portfolio_value * float(params["max_position_pct"]) / 100.0 - existing_value
    fee_rate = (
        float(cfg["charges"]["broker_pct"]) + float(cfg["charges"]["sebon_pct"])
    ) / 100.0
    per_share = price * (1 + fee_rate)
    if per_share > 0:
        affordable = int(max(0.0, cash) // per_share)
        shares = min(shares, affordable)
    if per_share > 0:
        allowed = int(max(0.0, room) // per_share)
        shares = min(shares, allowed)
    shares = (shares // lot) * lot if lot > 0 else shares
    shares = max(0, shares)

    reason = ""
    if shares == 0:
        if room <= 0:
            reason = "position concentration limit reached"
        elif cash <= 0:
            reason = "no cash available"
        else:
            reason = "risk budget or cash cannot cover one board lot"
    return {
        "shares": shares,
        "lots": shares // lot if lot else 0,
        "cash_used": round(shares * per_share, 2),
        "stop": trade["stop"],
        "target": trade["target"],
        "risk_per_share": trade["risk_per_share"],
        "reason": reason,
        "size": size,
    }


def _error_suggestion(symbol, message, market):
    return {
        "symbol": symbol,
        "action": "HOLD",
        "quantity": 0,
        "currentPrice": None,
        "confidence": 0.0,
        "reasoning": ["No suggestion for %s: %s" % (symbol, message)],
        "signals": {"error": message, "market": market},
    }


def generate(cfg, symbols, source=None):
    """One suggestion per symbol. Never raises for a single bad symbol."""
    params = params_for(cfg)
    market = market_state(cfg)
    names = []
    for raw in symbols or []:
        name = str(raw).strip().upper()
        if name and name not in names:
            names.append(name)

    source = source or data_mod.open_source(cfg)
    bars_by_symbol, errors, prices = {}, {}, {}
    # Market data is fetched one symbol at a time: the official NEPSE client
    # keeps a single short-lived token and is not safe to drive concurrently.
    for symbol in names:
        if not SYMBOL_RE.match(symbol):
            errors[symbol] = (
                "invalid symbol (letters, numbers, &, _, ., - only)"
            )
            continue
        try:
            bars = source.bars(symbol)
        except data_mod.DataError as exc:
            errors[symbol] = str(exc)
            continue
        if len(bars) < MIN_BARS:
            errors[symbol] = "not enough price history (%d bars, need %d)" % (
                len(bars),
                MIN_BARS,
            )
            continue
        bars_by_symbol[symbol] = bars
        prices[symbol] = bars[-1]["close"]

    lookback = max(MIN_BARS, int(params["lookback_days"]))

    def analyse(symbol):
        window = bars_by_symbol[symbol][-lookback:]
        score, signals = technical_score(window, params)
        return symbol, window, score, signals, backtest(window, params)

    workers = max(1, min(int(params.get("concurrency") or 1), len(bars_by_symbol)))
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            analysed = list(pool.map(analyse, list(bars_by_symbol)))
    else:
        analysed = [analyse(symbol) for symbol in bars_by_symbol]
    analysis = {row[0]: row[1:] for row in analysed}

    book = portfolio_mod.snapshot(cfg, prices=prices, source=source)
    portfolio_value = book["portfolio_value"] or float(cfg["account"]["cash"])
    held = {row["symbol"]: row for row in book["positions"]}
    cash = book["cash"]
    open_positions = len(held)
    spent = {symbol: row["market_value"] for symbol, row in held.items()}

    suggestions = []
    for symbol in names:
        if symbol in errors:
            suggestions.append(_error_suggestion(symbol, errors[symbol], market))
            continue

        window, score, signals, stats = analysis[symbol]
        confidence, multiplier = _confidence(score, stats, params)
        price = window[-1]["close"]
        atr = ind.atr_last(window, 14) or 0.0
        position = held.get(symbol)
        quantity_held = int(position["quantity"]) if position else 0

        reasoning = list(signals["notes"])
        reasoning.append(
            "Composite technical score %+.2f (weights: %s)"
            % (
                score,
                ", ".join(
                    "%s %.2f" % (key, float(value))
                    for key, value in (signals["weights"] or {}).items()
                ),
            )
        )
        if stats["trades"] >= int(params["min_history_trades"]):
            reasoning.append(
                "Time Machine replay: %d trades, %.1f%% win rate, %.2fR expectancy, "
                "%.2fR max drawdown -> confidence x%.2f"
                % (
                    stats["trades"],
                    stats["win_rate_pct"],
                    stats["expectancy_r"],
                    stats["max_drawdown_r"],
                    multiplier,
                )
            )
        else:
            reasoning.append(
                "Time Machine replay: only %d past signal(s), confidence not adjusted"
                % stats["trades"]
            )
        reasoning.append(
            "Paper portfolio: cash Rs %.2f, positions Rs %.2f, unrealized P&L Rs %.2f"
            % (book["cash"], book["position_value"], book["unrealized_pnl"])
        )
        if not market["open"]:
            reasoning.append(
                "Market is %s; signal uses the last completed session (%s)"
                % (market["status"], window[-1]["date"])
            )

        quantity = 0
        action = "HOLD"
        if score >= float(params["buy_threshold"]) and confidence >= float(
            params["min_confidence"]
        ):
            room = portfolio_value * float(params["max_position_pct"]) / 100.0
            existing = spent.get(symbol, 0.0)
            if open_positions >= int(params["max_positions"]) and not position:
                reasoning.append(
                    "HOLD: already holding the maximum %d positions"
                    % int(params["max_positions"])
                )
            elif room - existing <= 0:
                reasoning.append(
                    "HOLD: %s is already at the %.1f%% concentration limit"
                    % (symbol, float(params["max_position_pct"]))
                )
            else:
                sized = _size_buy(
                    cfg, params, price, atr, portfolio_value, cash, existing
                )
                quantity = sized["shares"]
                if quantity > 0:
                    action = "BUY"
                    cash -= sized["cash_used"]
                    spent[symbol] = existing + sized["cash_used"]
                    if not position:
                        open_positions += 1
                    reasoning.append(
                        "BUY %d shares (%d lots) at Rs %.2f, stop Rs %.2f, target "
                        "Rs %.2f, cash used Rs %.2f (risk %.1f%% of portfolio)"
                        % (
                            quantity,
                            sized["lots"],
                            price,
                            sized["stop"],
                            sized["target"],
                            sized["cash_used"],
                            float(params["risk_per_trade_pct"]),
                        )
                    )
                else:
                    reasoning.append("HOLD: %s" % sized["reason"])
        elif score <= float(params["sell_threshold"]) and confidence >= float(
            params["min_confidence"]
        ):
            if quantity_held <= 0:
                reasoning.append(
                    "HOLD: bearish score but no position to sell (NEPSE is long-only)"
                )
            else:
                fraction = float(params["sell_fraction"])
                if fraction >= 1.0:
                    quantity = quantity_held
                else:
                    lot = int(cfg["market"]["lot_size"])
                    quantity = int(quantity_held * fraction)
                    if lot > 1:
                        quantity = (quantity // lot) * lot
                    quantity = max(0, min(quantity, quantity_held))
                if quantity > 0:
                    action = "SELL"
                    spent[symbol] = max(0.0, spent.get(symbol, 0.0) - quantity * price)
                    reasoning.append(
                        "SELL %d of %d held shares at Rs %.2f (never more than held)"
                        % (quantity, quantity_held, price)
                    )
                else:
                    reasoning.append("HOLD: sell fraction rounds to zero shares")
        else:
            if score >= float(params["buy_threshold"]):
                reasoning.append(
                    "HOLD: bullish score but confidence %.2f is below the %.2f minimum"
                    % (confidence, float(params["min_confidence"]))
                )
            elif score <= float(params["sell_threshold"]):
                reasoning.append(
                    "HOLD: bearish score but confidence %.2f is below the %.2f minimum"
                    % (confidence, float(params["min_confidence"]))
                )
            else:
                reasoning.append(
                    "HOLD: score %+.2f is inside the +/-%.2f action band"
                    % (score, float(params["buy_threshold"]))
                )

        suggestions.append(
            {
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "currentPrice": round(price, 2),
                "confidence": confidence,
                "reasoning": reasoning,
                "signals": {
                    **signals,
                    "backtest": stats,
                    "position": position or None,
                    "portfolio_after": {
                        "cash": round(cash, 2),
                        "open_positions": open_positions,
                    },
                    "market": market,
                    "as_of": window[-1]["date"],
                },
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "params": params,
        "market": market,
        "portfolio": book,
        "suggestions": suggestions,
        "disclaimer": (
            "Paper-trading suggestions from cached daily bars and a simulated "
            "Time Machine. Not financial advice."
        ),
    }


def params_for(cfg):
    return params(cfg)
