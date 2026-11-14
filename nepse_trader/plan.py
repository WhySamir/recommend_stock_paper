"""Exit planner, position sizing and pre-trade risk checks.

NEPSE has no intraday round trip (T+2 delivery), so every plan is a
multi-session hold with a hard session limit instead of "flat by the close".
"""

from __future__ import annotations


def trading_days_hint(cfg):
    return (
        "hold up to %d sessions: NEPSE settles T+%d and sells only from demat"
        % (cfg["plan"]["max_hold_days"], cfg["market"]["settlement_days"])
    )


def build_plan(candidate, cfg):
    """Stop from the setup's range midpoint, risk capped at ``atr_mult`` ATR."""
    plan_cfg = cfg["plan"]
    entry = float(candidate.get("entry") or candidate["close"])
    atr = float(candidate.get("atr") or 0.0)
    setup_low = float(candidate.get("setup_low") or 0.0)
    if entry <= 0 or setup_low <= 0 or setup_low >= entry:
        return None

    mid = (entry + setup_low) / 2.0
    stop = mid
    basis = "range midpoint"
    if atr > 0:
        atr_stop = entry - plan_cfg["atr_mult"] * atr
        if atr_stop > stop:
            stop, basis = atr_stop, "%gx ATR" % plan_cfg["atr_mult"]
    risk = entry - stop
    if risk <= 0:
        return None

    target = entry + plan_cfg["target_r"] * risk
    note = ""
    band_upper = float(candidate.get("band_upper") or 0.0)
    # The band limits one session's move. A same-session target cannot exceed
    # it; a multi-session hold can, so it is reported rather than capped.
    if band_upper and target > band_upper:
        if int(plan_cfg["max_hold_days"]) <= 1:
            target = band_upper
            note = "target capped at the %.0f%% daily band" % cfg["market"]["price_band_pct"]
        else:
            note = (
                "target is %.1f%% above entry: needs more than one session "
                "(band top today %s)"
                % ((target / entry - 1) * 100, band_upper)
            )

    return {
        "symbol": candidate["symbol"].upper(),
        "setup": candidate.get("setup", ""),
        "signal_date": candidate.get("date", ""),
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_per_share": round(risk, 2),
        "reward_per_share": round(target - entry, 2),
        "reward_risk": round((target - entry) / risk, 2),
        "stop_basis": basis,
        "setup_low": round(setup_low, 2),
        "atr": round(atr, 2),
        "band_upper": round(band_upper, 2),
        "max_hold_days": plan_cfg["max_hold_days"],
        "entry_basis": candidate.get("entry_basis", "signal close"),
        "note": note,
    }


def _fees(cfg, turnover):
    charges = cfg["charges"]
    return turnover * (charges["broker_pct"] + charges["sebon_pct"]) / 100.0 + charges[
        "dp_charge"
    ]


def size_position(plan, cfg):
    """Whole board lots only, capped by cash and by max loss per trade.

    Broker, SEBON and DP charges are part of the loss and the reward, so a
    position that only fits the limit before charges gets one lot smaller.
    """
    market = cfg["market"]
    account = cfg["account"]
    charges = cfg["charges"]
    lot = int(market["lot_size"])
    risk = float(plan["risk_per_share"])
    entry = float(plan["entry"])
    stop = float(plan["stop"])
    cash = float(account["cash"])
    max_loss = float(account["max_loss_per_trade"])
    fee_rate = (charges["broker_pct"] + charges["sebon_pct"]) / 100.0
    dp = float(charges["dp_charge"])

    if risk <= 0 or stop >= entry:
        return {"feasible": False, "shares": 0, "lots": 0, "reason": "stop must sit below entry"}

    def measure(shares):
        buy_turnover = shares * entry
        buy_fees = (buy_turnover * fee_rate + dp) if shares else 0.0
        sell_fees = (shares * stop * fee_rate + dp) if shares else 0.0
        sell_target_fees = (shares * plan["target"] * fee_rate + dp) if shares else 0.0
        gross_loss = shares * risk
        net_loss = gross_loss + buy_fees + sell_fees
        net_reward = shares * plan["reward_per_share"] - buy_fees - sell_target_fees
        return {
            "buy_turnover": buy_turnover,
            "buy_fees": round(buy_fees, 2),
            "gross_loss": gross_loss,
            "net_loss": net_loss,
            "net_reward": net_reward,
            "cash_used": buy_turnover + buy_fees,
        }

    risk_ps_net = risk + fee_rate * (entry + stop)
    if risk_ps_net <= 0 or entry <= 0:
        return {"feasible": False, "shares": 0, "lots": 0, "reason": "invalid plan prices"}

    budget_lots = int(max(0.0, max_loss - 2 * dp) // (risk_ps_net * lot))
    cash_lots = int(max(0.0, cash - dp) // (entry * lot * (1 + fee_rate)))
    lots = max(0, min(budget_lots, cash_lots))
    while lots > 0 and measure(lots * lot)["net_loss"] > max_loss:
        lots -= 1
    shares = lots * lot
    figures = measure(shares)

    reason = ""
    if shares == 0:
        if budget_lots == 0:
            reason = (
                "max loss Rs %s cannot cover one board lot of %d shares "
                "(risk Rs %s plus charges)"
                % (max_loss, lot, round(risk * lot, 2))
            )
        else:
            reason = "not enough cash for one board lot (Rs %s needed)" % round(
                entry * lot, 2
            )

    net_reward_risk = (
        figures["net_reward"] / figures["net_loss"] if figures["net_loss"] > 0 else 0.0
    )
    return {
        "feasible": shares > 0,
        "shares": shares,
        "lots": lots,
        "position_cost": round(figures["buy_turnover"], 2),
        "cash_used": round(figures["cash_used"], 2),
        "buy_fees": figures["buy_fees"],
        "gross_loss_at_stop": round(figures["gross_loss"], 2),
        "net_loss_at_stop": round(figures["net_loss"], 2),
        "net_reward_at_target": round(figures["net_reward"], 2),
        "net_reward_risk": round(net_reward_risk, 2),
        "reason": reason,
    }


def risk_check(plan, size, cfg):
    """PASS/BLOCK with one reason per rule."""
    account = cfg["account"]
    market = cfg["market"]
    checks = []

    def add(name, ok, detail):
        checks.append({"rule": name, "ok": bool(ok), "detail": detail})

    add(
        "position exists",
        size["feasible"],
        size["reason"] or "%d shares in %d lots" % (size["shares"], size["lots"]),
    )
    if size["feasible"]:
        add(
            "loss at stop within per-trade limit",
            size["net_loss_at_stop"] <= account["max_loss_per_trade"],
            "Rs %s vs limit Rs %s (incl. charges)"
            % (size["net_loss_at_stop"], account["max_loss_per_trade"]),
        )
        add(
            "loss at stop within daily limit",
            size["net_loss_at_stop"] <= account["max_loss_per_day"],
            "Rs %s vs Rs %s" % (size["net_loss_at_stop"], account["max_loss_per_day"]),
        )
        add(
            "cash covers position",
            size["cash_used"] <= account["cash"],
            "Rs %s of Rs %s" % (size["cash_used"], account["cash"]),
        )
        add(
            "reward:risk after charges",
            size["net_reward_risk"] >= account["min_reward_risk"],
            "%.2fR vs %.2fR minimum"
            % (size["net_reward_risk"], account["min_reward_risk"]),
        )
        add(
            "whole board lots",
            size["shares"] % int(market["lot_size"]) == 0,
            "lot size %d" % market["lot_size"],
        )
    add(
        "entry inside the daily price band",
        (not plan["band_upper"]) or plan["entry"] <= plan["band_upper"],
        "entry Rs %s, band top Rs %s" % (plan["entry"], plan["band_upper"] or "-"),
    )
    add(
        "hold within settlement reality",
        market["intraday"] or plan["max_hold_days"] >= 1,
        trading_days_hint(cfg),
    )
    if not market["short_selling"]:
        add(
            "long only",
            plan["stop"] < plan["entry"],
            "NEPSE has no short selling; this is a delivery long",
        )

    return {
        "verdict": "PASS" if all(c["ok"] for c in checks) else "BLOCK",
        "checks": checks,
    }
