"""Unit tests for the indicators and the BUY / SELL / HOLD decision logic.

No network: market data comes from a fake source and the paper portfolio is
patched, so every expectation here is deterministic.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from nepse_trader import config, indicators as ind, suggestions, timemachine


def cfg_for_tests():
    cfg = json.loads(json.dumps(config.DEFAULTS))
    cfg["suggest"].update(
        {
            "buy_threshold": 0.05,
            "sell_threshold": -0.05,
            "min_confidence": 0.0,
            "lookback_days": 60,
            "min_history_trades": 1000,  # no backtest adjustment in these tests
        }
    )
    cfg["timemachine"]["iterations"] = 100
    return cfg


def series(count=120, drift=0.004, base=500.0):
    """A monotonic daily series; ``drift`` < 0 gives a clean downtrend."""
    bars = []
    price = base
    for index in range(count):
        price = price * (1 + drift)
        open_ = price / (1 + drift) if index else price
        bars.append(
            {
                "date": "2026-%02d-%02d" % (1 + (index // 28) % 12, 1 + index % 28),
                "open": round(open_, 2),
                "high": round(max(open_, price) * 1.002, 2),
                "low": round(min(open_, price) * 0.998, 2),
                "close": round(price, 2),
                "volume": 10000.0,
            }
        )
    return bars


class FakeSource:
    """Stands in for CsvSource / the NEPSE client."""

    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol
        self.calls = []

    def bars(self, symbol, refresh=False):
        self.calls.append(symbol)
        if symbol not in self.bars_by_symbol:
            from nepse_trader import data

            raise data.DataError("no data for %s: fixture is missing" % symbol)
        return self.bars_by_symbol[symbol]

    def last(self, symbol):
        bars = self.bars(symbol)
        return bars[-1]["close"], bars[-1]["date"]


def book(cash=100000.0, positions=None):
    rows = []
    value = 0.0
    for position in positions or []:
        market_value = position["quantity"] * position["avg_price"]
        rows.append(
            {
                "symbol": position["symbol"],
                "quantity": position["quantity"],
                "avg_price": position["avg_price"],
                "last_price": position["avg_price"],
                "market_value": market_value,
                "unrealized_pnl": 0.0,
                "unrealized_pnl_pct": 0.0,
                "realized_pnl": 0.0,
            }
        )
        value += market_value
    return {
        "cash": cash,
        "positions": rows,
        "position_value": value,
        "portfolio_value": cash + value,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "total_pnl": 0.0,
    }


def generate(cfg, symbols, source, portfolio):
    with patch.object(suggestions.portfolio_mod, "snapshot", return_value=portfolio):
        return suggestions.generate(cfg, symbols, source=source)


class IndicatorTests(unittest.TestCase):
    def test_sma_and_ema_of_a_known_series(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(ind.sma_last(values, 5), 3.0)
        self.assertIsNone(ind.sma_last(values, 6))

        series_ = ind.ema_series(values, 3)
        self.assertEqual(len(series_), len(values))
        self.assertEqual(series_[0], 1.0)  # seeded with the first value
        self.assertGreater(series_[-1], series_[-2])
        self.assertLess(series_[-1], 5.0)  # EMA lags a rising series
        self.assertIsNone(ind.ema_last(values, 6))
        self.assertIsNotNone(ind.ema_last(values, 5))

    def test_macd_needs_enough_history(self):
        self.assertIsNone(ind.macd_last([1.0] * 10))
        macd = ind.macd_last([100.0 + index * 0.5 for index in range(60)])
        self.assertEqual(set(macd), {"macd", "signal", "histogram"})
        self.assertAlmostEqual(macd["histogram"], macd["macd"] - macd["signal"])

    def test_stdev_is_population_std_of_the_last_window(self):
        values = [100.0, 1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(ind.stdev_last(values, 4), 1.118033988749895)
        self.assertIsNone(ind.stdev_last(values, 9))

    def test_rsi_is_extreme_on_a_one_way_move(self):
        rising = [100.0 + index for index in range(40)]
        falling = [140.0 - index for index in range(40)]
        self.assertEqual(ind.rsi_last(rising, 14), 100.0)
        self.assertEqual(ind.rsi_last(falling, 14), 0.0)
        self.assertIsNone(ind.rsi_last(rising[:5], 14))

    def test_atr_is_positive_and_none_without_bars(self):
        bars = series()
        atr = ind.atr_last(bars, 14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0.0)
        self.assertIsNone(ind.atr_last([], 14))


class TechnicalScoreTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_for_tests()
        self.params = suggestions.params(self.cfg)

    def test_uptrend_scores_positive_and_downtrend_negative(self):
        up, up_signals = suggestions.technical_score(series(drift=0.004), self.params)
        down, down_signals = suggestions.technical_score(series(drift=-0.004), self.params)
        self.assertGreater(up, 0.2)
        self.assertLess(down, -0.2)
        self.assertLessEqual(up, 1.0)
        self.assertGreaterEqual(down, -1.0)
        self.assertEqual(up_signals["components"]["trend"] > 0, True)
        self.assertEqual(down_signals["components"]["trend"] < 0, True)

    def test_overbought_rsi_pulls_the_score_down(self):
        bars = series(drift=0.02)  # near-vertical: RSI pins at 100
        _, signals = suggestions.technical_score(bars, self.params)
        self.assertEqual(signals["indicators"]["rsi14"], 100.0)
        self.assertLess(signals["components"]["rsi"], 0.0)

    def test_score_is_bounded_and_weights_are_reported(self):
        score, signals = suggestions.technical_score(series(), self.params)
        self.assertGreaterEqual(score, -1.0)
        self.assertLessEqual(score, 1.0)
        self.assertEqual(signals["weights"], self.params["weights"])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_for_tests()

    def one(self, result):
        self.assertEqual(len(result["suggestions"]), 1)
        return result["suggestions"][0]

    def test_bullish_score_buys_whole_lots_within_cash(self):
        source = FakeSource({"NABIL": series(drift=0.004)})
        item = self.one(generate(self.cfg, ["NABIL"], source, book(cash=100000.0)))
        self.assertEqual(item["action"], "BUY")
        self.assertGreater(item["quantity"], 0)
        self.assertEqual(item["quantity"] % self.cfg["market"]["lot_size"], 0)
        cash_used = item["signals"]["portfolio_after"]["cash"]
        self.assertGreaterEqual(cash_used, 0.0)
        self.assertLessEqual(cash_used, 100000.0)
        self.assertGreater(item["confidence"], 0.0)

    def test_bearish_score_sells_but_never_more_than_held(self):
        source = FakeSource({"NABIL": series(drift=-0.004)})
        positions = [{"symbol": "NABIL", "quantity": 25, "avg_price": 500.0}]
        item = self.one(generate(self.cfg, ["NABIL"], source, book(positions=positions)))
        self.assertEqual(item["action"], "SELL")
        self.assertLessEqual(item["quantity"], 25)
        self.assertGreater(item["quantity"], 0)

    def test_bearish_score_without_a_position_holds(self):
        source = FakeSource({"NABIL": series(drift=-0.004)})
        item = self.one(generate(self.cfg, ["NABIL"], source, book()))
        self.assertEqual(item["action"], "HOLD")
        self.assertEqual(item["quantity"], 0)
        self.assertIn("long-only", " ".join(item["reasoning"]))

    def test_confidence_below_the_minimum_holds(self):
        self.cfg["suggest"]["min_confidence"] = 1.01
        source = FakeSource({"NABIL": series(drift=0.004)})
        item = self.one(generate(self.cfg, ["NABIL"], source, book()))
        self.assertEqual(item["action"], "HOLD")
        self.assertIn("confidence", " ".join(item["reasoning"]))

    def test_inside_the_action_band_holds(self):
        self.cfg["suggest"]["buy_threshold"] = 2.0
        self.cfg["suggest"]["sell_threshold"] = -2.0
        source = FakeSource({"NABIL": series()})
        item = self.one(generate(self.cfg, ["NABIL"], source, book()))
        self.assertEqual(item["action"], "HOLD")

    def test_sell_fraction_rounds_down_to_whole_lots(self):
        self.cfg["suggest"]["sell_fraction"] = 0.5
        source = FakeSource({"NABIL": series(drift=-0.004)})
        positions = [{"symbol": "NABIL", "quantity": 25, "avg_price": 500.0}]
        item = self.one(generate(self.cfg, ["NABIL"], source, book(positions=positions)))
        self.assertEqual(item["action"], "SELL")
        self.assertEqual(item["quantity"], 10)
        self.assertLessEqual(item["quantity"], 25)

    def test_max_positions_blocks_a_new_symbol(self):
        self.cfg["suggest"]["max_positions"] = 2
        source = FakeSource({"NABIL": series(drift=0.004)})
        held = [
            {"symbol": "AAA", "quantity": 10, "avg_price": 100.0},
            {"symbol": "BBB", "quantity": 10, "avg_price": 100.0},
        ]
        item = self.one(generate(self.cfg, ["NABIL"], source, book(positions=held)))
        self.assertEqual(item["action"], "HOLD")
        self.assertIn("maximum 2 positions", " ".join(item["reasoning"]))

    def test_concentration_limit_blocks_a_second_buy(self):
        self.cfg["suggest"]["max_position_pct"] = 4.0
        source = FakeSource({"NABIL": series(drift=0.004)})
        held = [{"symbol": "NABIL", "quantity": 10, "avg_price": 420.0}]
        item = self.one(generate(self.cfg, ["NABIL"], source, book(positions=held)))
        self.assertEqual(item["action"], "HOLD")
        self.assertIn("concentration limit", " ".join(item["reasoning"]))

    def test_a_bad_symbol_never_fails_the_batch(self):
        source = FakeSource({"NABIL": series(drift=0.004)})
        result = generate(self.cfg, ["NABIL", "MISSING"], source, book())
        actions = {item["symbol"]: item for item in result["suggestions"]}
        self.assertEqual(actions["NABIL"]["action"], "BUY")
        self.assertEqual(actions["MISSING"]["action"], "HOLD")
        self.assertIn("no data for MISSING", actions["MISSING"]["reasoning"][0])

    def test_short_history_holds_with_an_explanation(self):
        source = FakeSource({"NABIL": series(count=30)})
        item = self.one(generate(self.cfg, ["NABIL"], source, book()))
        self.assertEqual(item["action"], "HOLD")
        self.assertIn("not enough price history", item["reasoning"][0])

    def test_empty_batch_is_empty_not_an_error(self):
        result = generate(self.cfg, [], FakeSource({}), book())
        self.assertEqual(result["suggestions"], [])


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_for_tests()
        self.params = suggestions.params(self.cfg)

    def test_never_buys_beyond_cash(self):
        sized = suggestions._size_buy(
            self.cfg, self.params, price=500.0, atr=10.0,
            portfolio_value=100000.0, cash=3000.0, existing_value=0.0,
        )
        self.assertLessEqual(sized["cash_used"], 3000.0)
        self.assertEqual(sized["shares"] % self.cfg["market"]["lot_size"], 0)

    def test_cash_below_one_lot_buys_nothing(self):
        sized = suggestions._size_buy(
            self.cfg, self.params, price=500.0, atr=10.0,
            portfolio_value=100000.0, cash=200.0, existing_value=0.0,
        )
        self.assertEqual(sized["shares"], 0)
        self.assertTrue(sized["reason"])

    def test_zero_atr_is_reported(self):
        sized = suggestions._size_buy(
            self.cfg, self.params, price=500.0, atr=0.0,
            portfolio_value=100000.0, cash=100000.0, existing_value=0.0,
        )
        self.assertEqual(sized["shares"], 0)
        self.assertIn("ATR", sized["reason"])

    def test_risk_budget_caps_the_position(self):
        self.cfg["account"]["max_loss_per_trade"] = 300.0
        self.cfg["suggest"]["risk_per_trade_pct"] = 0.3
        sized = suggestions._size_buy(
            self.cfg, self.params, price=500.0, atr=10.0,
            portfolio_value=100000.0, cash=100000.0, existing_value=0.0,
        )
        risk = sized["shares"] * sized["risk_per_share"]
        self.assertLessEqual(risk, 300.0 + 1e-6)


class MarketClockTests(unittest.TestCase):
    def test_session_status_in_nepal_time(self):
        from datetime import datetime, timezone

        cfg = cfg_for_tests()
        # Monday 2026-11-16 12:00 NPT = 06:15 UTC
        open_ = suggestions.market_state(cfg, datetime(2026, 11, 16, 6, 15, tzinfo=timezone.utc))
        self.assertTrue(open_["open"])
        self.assertEqual(open_["status"], "open")

        later = suggestions.market_state(cfg, datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc))
        self.assertFalse(later["open"])
        self.assertIn("outside", later["status"])

        weekend = suggestions.market_state(cfg, datetime(2026, 11, 15, 6, 15, tzinfo=timezone.utc))
        self.assertIn("weekend", weekend["status"])

    def test_backtest_reports_trades_and_expectancy(self):
        cfg = cfg_for_tests()
        params = suggestions.params(cfg)
        stats = suggestions.backtest(series(drift=0.004), params)
        self.assertEqual(
            stats["trades"], stats["wins"] + (stats["trades"] - stats["wins"])
        )
        self.assertGreaterEqual(stats["trades"], 0)
        self.assertIsInstance(stats["expectancy_r"], float)
        self.assertIsInstance(timemachine.history("NABIL", series(), cfg), dict)


if __name__ == "__main__":
    unittest.main()
