"""Offline tests for the future-simulation ledger."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
