"""Offline tests for the future-simulation ledger."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

from nepse_trader import config, future_simulations as fs


class DateAndOutcomeTests(unittest.TestCase):
    def test_today_npt_uses_kathmandu_offset(self):
        now = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(fs.today_npt(now), date(2026, 11, 21))

    def test_normalise_days_dedupes_and_sorts(self):
        self.assertEqual(fs.normalise_days([10, 4, 10]), [4, 10])
        self.assertEqual(fs.normalise_days(4), [4])

    def test_due_date_rolls_weekends_and_holidays(self):
        self.assertEqual(fs.due_date(date(2026, 11, 20), 4, set()), date(2026, 11, 24))
        self.assertEqual(
            fs.due_date(date(2026, 11, 20), 4, {"2026-11-24"}), date(2026, 11, 25)
        )
        self.assertEqual(fs.due_date(date(2026, 11, 21), 1, set()), date(2026, 11, 23))

    def test_outcome_buy_and_sell_charge_both_legs(self):
        cfg = config.DEFAULTS
        buy = fs.outcome("buy", 10, 574.0, 600.0, cfg)
        sell = fs.outcome("sell", 10, 600.0, 574.0, cfg)
        self.assertAlmostEqual(buy["fees"], 94.03, places=1)
        self.assertAlmostEqual(sell["fees"], 94.03, places=1)
        self.assertEqual((buy["gross_pnl"], sell["gross_pnl"]), (260.0, 260.0))
        self.assertEqual((buy["result"], sell["result"]), ("profit", "profit"))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(config.DEFAULTS))
        self.cfg["timemachine"]["iterations"] = 100
        self.bars = [
            {"date": "2026-11-20", "close": 574.0, "high": 575.0, "low": 573.0, "open": 574.0, "volume": 1},
            {"date": "2026-11-24", "close": 600.0, "high": 601.0, "low": 599.0, "open": 600.0, "volume": 1},
            {"date": "2026-11-30", "close": 620.0, "high": 621.0, "low": 619.0, "open": 620.0, "volume": 1},
        ]
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    def test_new_record_has_due_dates_and_slim_forecast(self):
        record = fs.new_record("NABIL", "buy", 10, 574.0, [10, 4], self.cfg, self.bars,
                               iterations=50, seed=7, now=self.now)
        self.assertEqual([h["days"] for h in record["horizons"]], [4, 10])
        self.assertEqual([h["due_date"] for h in record["horizons"]], ["2026-11-24", "2026-11-30"])
        self.assertEqual(record["status"], "pending")
        self.assertIn("expected_net_pnl", record["horizons"][0]["forecast"])
        self.assertNotIn("sample_outcomes", record["horizons"][0]["forecast"])

    def test_settle_uses_due_bar_and_freezes_result(self):
        record = fs.new_record("NABIL", "buy", 10, 574.0, 4, self.cfg, self.bars, now=self.now)
        later = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)
        settled = fs.settle(record, self.bars, now=later)
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(settled["horizons"][0]["exit_price"], 600.0)
        self.assertEqual(settled["horizons"][0]["result"], "profit")
        changed = [dict(bar, close=999.0) for bar in self.bars]
        again = fs.settle(settled, changed, now=later)
        self.assertEqual(again["horizons"][0]["net_pnl"], settled["horizons"][0]["net_pnl"])

    def test_settle_pending_without_due_bar(self):
        record = fs.new_record("NABIL", "buy", 10, 574.0, 4, self.cfg, self.bars[:1], now=self.now)
        settled = fs.settle(record, self.bars[:1], now=datetime(2026, 11, 25, tzinfo=timezone.utc))
        self.assertEqual(settled["status"], "pending")
        self.assertEqual(settled["horizons"][0]["reason"], "due-date bar not available")
        self.assertEqual(settled["horizons"][0]["last_bar_date"], "2026-11-20")

    def test_settle_partial_multiple_horizons(self):
        record = fs.new_record("NABIL", "sell", 10, 600.0, [4, 10], self.cfg, self.bars, now=self.now)
        settled = fs.settle(record, self.bars[:2], now=datetime(2026, 11, 26, tzinfo=timezone.utc))
        self.assertEqual(settled["status"], "partial")
        self.assertEqual([h["status"] for h in settled["horizons"]], ["settled", "pending"])
        self.assertEqual(settled["horizons"][0]["result"], "loss")


if __name__ == "__main__":
    unittest.main()
