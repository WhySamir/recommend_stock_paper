"""Offline tests: no network, no NEPSE credentials needed."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nepse_trader import alerts, cli, config, data, plan, scan, timemachine


def bars_for_test(count=140, base=500.0):
    """A gentle uptrend, then a hard breakout on the final bar."""
    bars = []
    price = base
    for index in range(count):
        drift = 0.0015
        price = price * (1 + drift)
        open_ = price * 0.997
        close = price
        high = max(open_, close) * 1.004
        low = min(open_, close) * 0.996
        bars.append(
            {
                "date": "2026-%02d-%02d" % (1 + (index // 28) % 12, 1 + index % 28),
                "open": round(open_, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "volume": 12000.0,
            }
        )
    # final breakout bar
    last = bars[-1]
    breakout_close = round(last["close"] * 1.05, 2)
    bars.append(
        {
            "date": "2026-12-30",
            "open": last["close"],
            "high": round(breakout_close * 1.003, 2),
            "low": round(last["close"] * 0.998, 2),
            "close": breakout_close,
            "volume": 48000.0,
        }
    )
    return bars


def cfg_for(data_dir, state_dir):
    cfg = json.loads(json.dumps(config.DEFAULTS))
    cfg["data"]["dir"] = str(data_dir)
    cfg["data"]["kind"] = "csv"
    cfg["state_dir"] = str(state_dir)
    cfg["universe"] = ["NABIL"]
    return cfg


class DataTests(unittest.TestCase):
    def test_csv_roundtrip_sorts_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "NABIL.csv"
            path.write_text(
                "date,open,high,low,close,volume\n"
                "2026-11-11,500,510,495,505,1500\n"
                "2026/11/09,480,500,478,498,1200\n"
                "2026-11-09,480,500,478,498,1200\n",
                encoding="utf-8",
            )
            bars = data.read_csv(path)
            self.assertEqual([b["date"] for b in bars], ["2026-11-09", "2026-11-11"])
            self.assertEqual(bars[-1]["volume"], 1500.0)

    def test_bad_date_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "X.csv"
            path.write_text("date,open,high,low,close,volume\nnope,1,1,1,1,1\n", encoding="utf-8")
            with self.assertRaises(data.DataError):
                data.read_csv(path)

    def test_http_mapping_and_symbol_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfg_for("data", "trader")
            cfg["data"]["kind"] = "http"
            cfg["data"]["cache_dir"] = str(Path(tmp) / "cache")
            cfg["data"]["api"]["history_url"] = "https://api.test/daily/{symbol}"
            cfg["data"]["api"]["symbol_map"] = {"NABIL": "NABIL-123"}
            source = data.HttpSource(cfg)
            self.assertEqual(source._url("NABIL"), "https://api.test/daily/NABIL-123")

            payload = {
                "status": "ok",
                "data": [
                    {"t": "2026-11-10", "o": 480, "h": 500, "l": 478, "c": 498, "q": 1200},
                    {"t": "2026-11-11", "o": 498, "h": 510, "l": 495, "c": 505, "q": 1500},
                ],
            }
            cfg["data"]["api"]["fields"] = {
                "date": "t",
                "open": "o",
                "high": "h",
                "low": "l",
                "close": "c",
                "volume": "q",
            }
            source.api = cfg["data"]["api"]
            source._get_json = lambda url: payload
            bars = source.bars("NABIL")
            self.assertEqual(len(bars), 2)
            self.assertEqual(bars[-1]["close"], 505.0)
            self.assertEqual(bars[-1]["date"], "2026-11-11")

    def test_missing_env_var_fails_loudly(self):
        cfg = cfg_for("data", "trader")
        with self.assertRaises(data.DataError):
            data._expand("Bearer ${NEPSE_TRADER_TEST_UNSET_VAR}")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_for("data", "trader")

    def test_breakout_is_detected(self):
        hits = scan.evaluate("NABIL", bars_for_test(), self.cfg)
        self.assertTrue(any(h["setup"] == "breakout" for h in hits))
        hit = [h for h in hits if h["setup"] == "breakout"][0]
        self.assertGreater(hit["close"], hit["trigger"])
        self.assertGreater(hit["volume_ratio"], 1.2)

    def test_plan_and_size_respect_nepse_rules(self):
        hit = scan.evaluate("NABIL", bars_for_test(), self.cfg)[0]
        trade = plan.build_plan(hit, self.cfg)
        self.assertLess(trade["stop"], trade["entry"])
        self.assertGreater(trade["target"], trade["entry"])
        self.assertLessEqual(trade["reward_risk"], self.cfg["plan"]["target_r"])

        size = plan.size_position(trade, self.cfg)
        self.assertTrue(size["feasible"])
        self.assertEqual(size["shares"] % self.cfg["market"]["lot_size"], 0)
        self.assertGreater(size["shares"], 0)
        self.assertLessEqual(size["net_loss_at_stop"], self.cfg["account"]["max_loss_per_trade"])

        verdict = plan.risk_check(trade, size, self.cfg)
        self.assertEqual(verdict["verdict"], "PASS")

    def test_tiny_account_is_blocked(self):
        self.cfg["account"]["cash"] = 1000.0
        self.cfg["account"]["max_loss_per_trade"] = 50.0
        hit = scan.evaluate("NABIL", bars_for_test(), self.cfg)[0]
        trade = plan.build_plan(hit, self.cfg)
        size = plan.size_position(trade, self.cfg)
        self.assertFalse(size["feasible"])
        self.assertIn("max loss", size["reason"])
        self.assertEqual(plan.risk_check(trade, size, self.cfg)["verdict"], "BLOCK")

    def test_simulation_is_deterministic_and_scores_stop_first(self):
        bars = bars_for_test()
        hit = scan.evaluate("NABIL", bars, self.cfg)[0]
        trade = plan.build_plan(hit, self.cfg)
        self.cfg["timemachine"]["iterations"] = 500
        first = timemachine.monte_carlo(bars, trade, self.cfg)
        second = timemachine.monte_carlo(bars, trade, self.cfg)
        self.assertEqual(first, second)
        self.assertEqual(
            first["target_first_pct"] + first["stop_first_pct"] + first["timeout_pct"], 100.0
        )

    def test_history_replay_counts_outcomes(self):
        bars = bars_for_test()
        result = timemachine.history("NABIL", bars, self.cfg)
        self.assertEqual(
            result["firings"],
            result["target_first"] + result["stop_first"] + result["timeout"],
        )
        self.assertIsInstance(result["expectancy_r"], float)

    def test_history_entry_uses_the_open_not_the_close(self):
        # A gap-down close below the stop must not invalidate a trade that
        # entered at the open above it.
        bars = [
            {"date": "2026-11-01", "open": 100.0, "high": 101.0, "low": 89.0, "close": 90.0},
            {"date": "2026-11-02", "open": 90.0, "high": 96.0, "low": 94.0, "close": 95.0},
        ]
        outcome, r_multiple, sessions = timemachine._simulate_forward(
            bars, 0, 95.0, 110.0, 10
        )
        self.assertEqual(outcome, "stop")
        self.assertEqual(r_multiple, -1.0)
        self.assertEqual(sessions, 1)


class AlertTests(unittest.TestCase):
    def test_stop_and_target_alerts_fire_once(self):
        class Source:
            def __init__(self, price):
                self.price = price

            def last(self, symbol):
                return self.price, "2026-11-12"

        store = []
        alerts.add(store, "NABIL", "stop", 480.0, "stop")
        alerts.add(store, "NABIL", "target", 540.0, "target")

        fired = alerts.check(store, Source(475.0))
        self.assertEqual(len(fired), 1)
        self.assertIn("stop hit", fired[0]["message"])
        self.assertEqual(alerts.check(store, Source(475.0)), [])

    def test_hold_limit_uses_session_count(self):
        store = []
        alerts.add(store, "NABIL", "max_hold", None, "flat", since="2026-11-01", sessions=3)
        series = [
            {"date": "2026-11-01"},
            {"date": "2026-11-02"},
            {"date": "2026-11-03"},
            {"date": "2026-11-04"},
        ]
        fired = alerts.check(store, object(), bars_for=lambda symbol: series)
        self.assertEqual(len(fired), 1)
        self.assertIn("flatten", fired[0]["message"])


class CliTests(unittest.TestCase):
    def test_full_chain_writes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            data.write_csv(data_dir / "NABIL.csv", bars_for_test())

            cfg_path = root / "config.json"
            cfg = cfg_for(data_dir, root / "trader")
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            base = ["--config", str(cfg_path)]
            self.assertEqual(cli.main(base + ["scan"]), 0)
            self.assertEqual(cli.main(base + ["entry", "NABIL"]), 0)
            self.assertEqual(cli.main(base + ["exit"]), 0)
            self.assertEqual(cli.main(base + ["budget"]), 0)
            self.assertEqual(cli.main(base + ["risk"]), 0)
            self.assertEqual(cli.main(base + ["timemachine", "NABIL", "--iterations", "300"]), 0)
            self.assertEqual(cli.main(base + ["alerts", "add", "--from-plan"]), 0)
            self.assertEqual(cli.main(base + ["alerts", "list"]), 0)

            trader = root / "trader"
            for name in ("scan.json", "plans.json", "orders.json", "timemachine.json", "alerts.json"):
                self.assertTrue((trader / name).exists(), name)

            alerts_state = json.loads((trader / "alerts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(alerts_state), 3)
            orders = json.loads((trader / "orders.json").read_text(encoding="utf-8"))
            self.assertIn("risk", orders[0])


if __name__ == "__main__":
    unittest.main()
