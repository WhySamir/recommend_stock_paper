"""Command line: scan -> entry -> exit/plan -> budget -> risk -> timemachine, alerts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import alerts as alerts_mod
from . import config as config_mod
from . import data as data_mod
from . import plan as plan_mod
from . import scan as scan_mod
from . import store
from . import timemachine as tm_mod


def _p(value):
    return "Rs %s" % format(value, ",.2f")


def _state(cfg, name):
    directory = Path(cfg["state_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def _write(path, payload):
    if store.backend() == "postgres":
        store.write_state(Path(path).stem, payload)
        return
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read(path):
    if store.backend() == "postgres":
        return store.read_state(Path(path).stem)
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _need(payload, hint):
    if payload is None:
        raise SystemExit("nothing to work with yet: %s" % hint)
    return payload


def cmd_scan(cfg, args):
    source = data_mod.open_source(cfg)
    names = data_mod.universe(cfg, source, args.symbols, args.limit)
    if not names:
        raise SystemExit(
            "no symbols: pass --symbols, set 'universe' in the config, or put "
            "%s/*.csv files in place" % cfg["data"]["dir"]
        )

    candidates, errors = [], []
    for symbol in names:
        try:
            bars = source.bars(symbol, refresh=args.refresh)
        except data_mod.DataError as exc:
            errors.append(str(exc))
            continue
        candidates.extend(scan_mod.evaluate(symbol, bars, cfg))

    candidates.sort(key=lambda c: (-c["volume_ratio"], c["symbol"]))
    payload = {
        "as_of": candidates[0]["date"] if candidates else "",
        "scanned": len(names),
        "candidates": candidates,
        "errors": errors,
    }
    _write(_state(cfg, "scan.json"), payload)

    print("Scanned %d symbols, %d setup(s) fired." % (len(names), len(candidates)))
    if candidates:
        print(
            "%-10s %-9s %10s %10s %10s %6s %8s %s"
            % ("SYMBOL", "SETUP", "CLOSE", "TRIGGER", "STOP REF", "ATR", "VOL", "DATE")
        )
        for c in candidates:
            print(
                "%-10s %-9s %10s %10s %10s %6.1f %7.2fx %s"
                % (
                    c["symbol"],
                    c["setup"],
                    c["close"],
                    c["trigger"],
                    c["setup_low"],
                    c["atr"],
                    c["volume_ratio"],
                    c["date"],
                )
            )
    else:
        print("No setups on the latest session. Nothing to plan.")
    for error in errors:
        print("  ! %s" % error, file=sys.stderr)
    return 0


def cmd_entry(cfg, args):
    source = data_mod.open_source(cfg)
    symbol = args.symbol.upper()
    bars = source.bars(symbol, refresh=args.refresh)
    status, hits = scan_mod.entry_status(symbol, bars, cfg, args.setup)
    print("%s %s as of %s (close %s)" % (symbol, status, bars[-1]["date"], _p(bars[-1]["close"])))
    for hit in hits:
        print(
            "  %s: %s | trigger %s | stop ref %s | band %s - %s"
            % (
                hit["setup"],
                hit["detail"],
                _p(hit["trigger"]),
                _p(hit["setup_low"]),
                hit["band_lower"],
                hit["band_upper"],
            )
        )
    if not hits:
        print("  no setup fired on the latest bar; nothing to enter today.")
        print("  (NEPSE is delivery-based: a buy today cannot be sold for two sessions.)")
    return 0


def _entry_for_plan(source, candidate):
    """Use the actual next-session open when NEPSE has one; else return None."""
    opener = getattr(source, "session_open", None)
    if opener is None:
        return None
    symbol = candidate["symbol"].upper()
    try:
        opened = opener(symbol)
        if not opened:
            return None
        price, as_of = opened
        if price <= 0 or not as_of or as_of <= candidate["date"]:
            return None
        bars = source.bars(symbol)
    except data_mod.DataError:
        return None
    dates = [bar["date"] for bar in bars]
    try:
        index = dates.index(candidate["date"])
    except ValueError:
        return price, "later session open (entry late)"
    if index + 1 < len(dates) and dates[index + 1] == as_of:
        return price, "next session open"
    return price, "later session open (entry late)"


def cmd_plan(cfg, args):
    state = _need(_read(_state(cfg, "scan.json")), "run 'scan' first")
    candidates = state.get("candidates") or []
    if not candidates:
        _write(_state(cfg, "plans.json"), [])
        print("No candidates in the last scan, so there is nothing to plan.")
        print("Run 'scan --symbols <SYMBOL>' again when a setup fires.")
        return 0
    source = data_mod.open_source(cfg)
    # Several setups can fire on one bar of one stock. That is one position,
    # not one position per setup, so the group becomes a single plan.
    groups = {}
    for candidate in candidates:
        groups.setdefault(candidate["symbol"], []).append(candidate)

    plans = []
    for group in groups.values():
        group.sort(key=lambda c: c.get("volume_ratio", 0), reverse=True)
        candidate = dict(group[0])
        setups = []
        for item in group:
            name = item.get("setup")
            if name and name not in setups:
                setups.append(name)
        candidate["setups"] = setups
        actual = _entry_for_plan(source, candidate)
        if actual:
            candidate["entry"], candidate["entry_basis"] = actual
        plan = plan_mod.build_plan(candidate, cfg)
        if plan:
            plan["volume_ratio"] = candidate.get("volume_ratio", 0)
            plan["setups"] = setups
            plans.append(plan)
    _write(_state(cfg, "plans.json"), plans)

    if not plans:
        print("No plan: every candidate was invalid (stop above entry).")
        return 0
    print(
        "%-10s %-18s %10s %10s %10s %7s %5s"
        % ("SYMBOL", "SETUP", "ENTRY", "STOP", "TARGET", "RISK/SH", "R:R")
    )
    for plan in plans:
        print(
            "%-10s %-18s %10s %10s %10s %7.2f %5.2f"
            % (
                plan["symbol"],
                "+".join(plan.get("setups") or [plan["setup"]]),
                plan["entry"],
                plan["stop"],
                plan["target"],
                plan["risk_per_share"],
                plan["reward_risk"],
            )
        )
    print("\nHold up to %d sessions; stop basis %s." % (plans[0]["max_hold_days"], plans[0]["stop_basis"]))
    for plan in plans:
        basis = plan.get("entry_basis") or ""
        if basis in ("signal close", "signal close (provisional)"):
            print(
                "  %s: entry %s is the signal close (provisional); the real fill is the next session's open."
                % (plan["symbol"], _p(plan["entry"]))
            )
        elif basis.startswith("later session open"):
            print(
                "  %s: entry %s is a later session's open; the setup is stale, rescan before acting."
                % (plan["symbol"], _p(plan["entry"]))
            )
        if plan["note"]:
            print("  %s: %s" % (plan["symbol"], plan["note"]))
    return 0


def cmd_budget(cfg, args):
    plans = _need(_read(_state(cfg, "plans.json")), "run 'exit' (or 'plan') first")
    orders = []
    for plan in plans:
        orders.append({"plan": plan, "size": plan_mod.size_position(plan, cfg)})
    _write(_state(cfg, "orders.json"), orders)

    print("Account %s, max loss %s per trade, lot %d." % (
        _p(cfg["account"]["cash"]),
        _p(cfg["account"]["max_loss_per_trade"]),
        cfg["market"]["lot_size"],
    ))
    for order in orders:
        plan, size = order["plan"], order["size"]
        if not size["feasible"]:
            print("%-10s BLOCKED - %s" % (plan["symbol"], size["reason"]))
            continue
        print(
            "%-10s %d shares (%d lots) cost %s | at stop -%s net | at target +%s net | %.2fR"
            % (
                plan["symbol"],
                size["shares"],
                size["lots"],
                _p(size["position_cost"]),
                _p(size["net_loss_at_stop"]),
                _p(size["net_reward_at_target"]),
                size["net_reward_risk"],
            )
        )
    return 0


def cmd_risk(cfg, args):
    orders = _need(_read(_state(cfg, "orders.json")), "run 'budget' first")
    verdicts = []
    for order in orders:
        verdict = plan_mod.risk_check(order["plan"], order["size"], cfg)
        order["risk"] = verdict
        verdicts.append((order["plan"]["symbol"], verdict))
    _write(_state(cfg, "orders.json"), orders)

    for symbol, verdict in verdicts:
        print("%s: %s" % (symbol, verdict["verdict"]))
        for check in verdict["checks"]:
            print("  [%s] %-34s %s" % ("ok" if check["ok"] else "XX", check["rule"], check["detail"]))
    blocked = [s for s, v in verdicts if v["verdict"] != "PASS"]
    print("\n%d order(s) PASS, %d BLOCKED." % (len(verdicts) - len(blocked), len(blocked)))
    return 0


def _plan_from_anywhere(cfg, args, bars):
    if args.entry and args.stop:
        entry, stop = float(args.entry), float(args.stop)
        if stop >= entry:
            raise SystemExit("--stop must be below --entry (NEPSE is long only)")
        risk = entry - stop
        target = float(args.target) if args.target else entry + cfg["plan"]["target_r"] * risk
        return {
            "symbol": args.symbol.upper(),
            "setup": "manual",
            "signal_date": bars[-1]["date"],
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "risk_per_share": round(risk, 2),
            "reward_per_share": round(target - entry, 2),
            "reward_risk": round((target - entry) / risk, 2),
            "stop_basis": "manual",
            "band_upper": round(bars[-1]["close"] * 1.15, 2),
            "max_hold_days": cfg["plan"]["max_hold_days"],
            "note": "",
        }

    plans = _read(_state(cfg, "plans.json")) or []
    for plan in plans:
        if plan["symbol"] == args.symbol.upper():
            return plan

    hits = scan_mod.evaluate(args.symbol.upper(), bars, cfg)
    if hits:
        return plan_mod.build_plan(hits[0], cfg)
    raise SystemExit(
        "no plan for %s: run 'scan' then 'exit', or pass --entry/--stop/--target" % args.symbol
    )


def cmd_timemachine(cfg, args):
    source = data_mod.open_source(cfg)
    symbol = args.symbol.upper()
    bars = source.bars(symbol, refresh=args.refresh)
    plan = _plan_from_anywhere(cfg, args, bars)
    cfg = json.loads(json.dumps(cfg))
    cfg["timemachine"]["iterations"] = args.iterations or cfg["timemachine"]["iterations"]
    cfg["timemachine"]["seed"] = args.seed if args.seed is not None else cfg["timemachine"]["seed"]

    result = tm_mod.run(symbol, bars, plan, cfg)
    _write(_state(cfg, "timemachine.json"), result)

    hist = result["history"]
    sim = result["simulation"]
    print("Time Machine: %s, plan entry %s / stop %s / target %s" % (
        symbol, _p(plan["entry"]), _p(plan["stop"]), _p(plan["target"])))
    print("  history: %d past firing(s) of the setup | %d target first, %d stop first, %d timed out"
          % (hist["firings"], hist["target_first"], hist["stop_first"], hist["timeout"]))
    print("           hit rate %.1f%%, expectancy %.2fR per trade"
          % (hist["hit_rate_pct"], hist["expectancy_r"]))
    if sim.get("iterations"):
        print("  simulation: %d replays (seed %s, %d sessions bootstrapped)"
              % (sim["iterations"], sim["seed"], sim["sessions_bootstrapped"]))
        print("           target first %.1f%%, stop first %.1f%%, timeout %.1f%%, expectancy %.2fR"
              % (
                  sim["target_first_pct"],
                  sim["stop_first_pct"],
                  sim["timeout_pct"],
                  sim["expectancy_r"],
              ))
    print("  %s" % result["caveat"])
    return 0


def cmd_alerts(cfg, args):
    path = alerts_mod.path_for(cfg)
    current = alerts_mod.load(path)

    if args.action == "list":
        if not current:
            print("No alerts. Add some with 'alerts add --from-plan' after 'exit'.")
            return 0
        for alert in current:
            state = "TRIGGERED %s" % alert["triggered_on"] if alert["triggered"] else "armed"
            print("%-10s %-8s %-10s %s  (%s)" % (
                alert["symbol"], alert["kind"],
                "-" if alert["level"] is None else alert["level"], alert["note"], state))
        return 0

    if args.action == "add":
        plans = _need(_read(_state(cfg, "plans.json")), "run 'exit' (or 'plan') first")
        if not plans:
            print("No plans to watch.")
            return 0
        wanted = {s.upper() for s in args.symbols} if args.symbols else None
        added = 0
        for plan in plans:
            if wanted and plan["symbol"] not in wanted:
                continue
            alerts_mod.from_plan(current, plan, cfg)
            added += 3
        alerts_mod.save(path, current)
        print("Added %d alert(s) to %s." % (added, path))
        return 0

    source = data_mod.open_source(cfg)
    fired = alerts_mod.check(current, source, bars_for=source.bars)
    alerts_mod.save(path, current)
    if not fired:
        print("Nothing hit. %d alert(s) still armed." % sum(1 for a in current if not a["triggered"]))
        return 0
    for alert in fired:
        print(alert["message"])
    return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to config.json")
    common.add_argument(
        "--state-dir", default=argparse.SUPPRESS, help="where scan/plans/orders live"
    )

    parser = argparse.ArgumentParser(
        prog="nepse-trader",
        description="NEPSE-adapted trading engine: scan, entry, exit, budget, risk, timemachine, alerts.",
    )
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--state-dir", default=None, help="where scan/plans/orders live")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common], help="find setups in your NEPSE data")
    scan.add_argument("--symbols", nargs="*", help="symbols to scan")
    scan.add_argument("--limit", type=int, default=None)
    scan.add_argument("--refresh", action="store_true", help="bypass the local bar cache")
    scan.set_defaults(func=cmd_scan)

    entry = sub.add_parser("entry", parents=[common], help="did the setup trigger?")
    entry.add_argument("symbol")
    entry.add_argument("--setup", choices=["breakout", "flag", "bounce"], default=None)
    entry.add_argument("--refresh", action="store_true")
    entry.set_defaults(func=cmd_entry)

    for name in ("exit", "plan"):
        plan_cmd = sub.add_parser(
            name, parents=[common], help="set stop and target for every scan candidate"
        )
        plan_cmd.set_defaults(func=cmd_plan)

    budget = sub.add_parser("budget", parents=[common], help="size positions to your account")
    budget.set_defaults(func=cmd_budget)

    risk = sub.add_parser("risk", parents=[common], help="PASS/BLOCK against your limits")
    risk.set_defaults(func=cmd_risk)

    tm = sub.add_parser("timemachine", parents=[common], help="replay outcomes before you risk money")
    tm.add_argument("symbol")
    tm.add_argument("--entry", type=float, default=None)
    tm.add_argument("--stop", type=float, default=None)
    tm.add_argument("--target", type=float, default=None)
    tm.add_argument("--iterations", type=int, default=None)
    tm.add_argument("--seed", type=int, default=None)
    tm.add_argument("--refresh", action="store_true")
    tm.set_defaults(func=cmd_timemachine)

    alerts = sub.add_parser("alerts", parents=[common], help="stops, targets and hold limits")
    alerts.add_argument("action", choices=["add", "check", "list"])
    alerts.add_argument("--from-plan", action="store_true")
    alerts.add_argument("--symbols", nargs="*", default=None)
    alerts.set_defaults(func=cmd_alerts)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    if args.state_dir:
        cfg["state_dir"] = args.state_dir
    try:
        return args.func(cfg, args)
    except data_mod.DataError as exc:
        print("data error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
