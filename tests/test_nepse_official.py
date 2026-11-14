"""Offline tests for the official NEPSE source (no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from nepse_trader import cli, config, data, nepse_api, plan


def row(business_date, high, low, close, quantity):
    return {
        "businessDate": business_date,
        "highPrice": high,
        "lowPrice": low,
        "closePrice": close,
        "totalTradedQuantity": quantity,
    }


class FakeClient:
    def __init__(self, rows, securities=None, quote=None, fail_quote=False):
        self.rows = [dict(r) for r in rows]
        self.securities = securities or [{"id": 131, "symbol": "NABIL"}]
        self.quote_payload = quote
        self.fail_quote = fail_quote
        self.history_calls = 0
        self.quote_calls = 0

    def history(self, security_id, start, end):
        self.history_calls += 1
        return {"content": [dict(r) for r in self.rows]}

    def security_list(self):
        return list(self.securities)

    def quote(self, security_id):
        self.quote_calls += 1
        if self.fail_quote:
            raise nepse_api.NepseError("quote unavailable")
        return self.quote_payload


def make_source(tmp, fake, cache_ttl=0):
    cfg = json.loads(json.dumps(config.DEFAULTS))
    cfg["data"]["kind"] = "nepse"
    cfg["data"]["cache_dir"] = str(Path(tmp) / "cache")
    cfg["data"]["cache_ttl_minutes"] = cache_ttl
    source = data.NepseOfficialSource(cfg)
    source.client = fake
    return source


class TokenTests(unittest.TestCase):
    def test_offsets_match_wasm_samples(self):
        samples = [
            (0, 27, 37, 65, 93, 115),
            (7, 31, 41, 69, 97, 119),
            (42, 28, 42, 70, 98, 116),
            (999, 31, 59, 87, 106, 128),
            (12345, 25, 42, 70, 95, 116),
            (64162, 27, 44, 72, 99, 116),
            (98765, 28, 51, 79, 100, 123),
        ]
        for salt2, c, r, b, n, m in samples:
            self.assertEqual(nepse_api.token_offsets(salt2), (c, r, b, n, m))

    def test_parse_token_removes_exactly_the_five_characters(self):
        token = "".join(chr(ord("A") + (i % 26)) for i in range(250))
        prove = {"accessToken": token, "salt2": 64162}
        parsed = nepse_api.parse_token(prove)
        cuts = set(nepse_api.token_offsets(64162))
        expected = "".join(ch for i, ch in enumerate(token) if i not in cuts)
        self.assertEqual(parsed, expected)
        self.assertEqual(len(parsed), 245)

    def test_bad_prove_payloads_raise(self):
        with self.assertRaises(nepse_api.NepseError):
            nepse_api.parse_token({})
        with self.assertRaises(nepse_api.NepseError):
            nepse_api.parse_token({"accessToken": "abc", "salt2": "not-a-number"})


class OfficialSourceTests(unittest.TestCase):
    def test_history_maps_ohlcv_and_synthesizes_open(self):
        rows = [
            row("2026-11-06", 554, 549, 549, 21117),
            row("2026-11-05", 553, 545, 550, 54860),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeClient(rows)
            source = make_source(tmp, fake)
            bars = source.bars("NABIL")

            self.assertEqual([b["date"] for b in bars], ["2026-11-05", "2026-11-06"])
            self.assertEqual(bars[0]["open"], 550.0)
            self.assertEqual(bars[1]["open"], 550.0)
            self.assertEqual(bars[1]["close"], 549.0)
            self.assertEqual(bars[1]["volume"], 21117.0)
            self.assertEqual(fake.history_calls, 1)

            mapping = json.loads(
                (Path(tmp) / "cache" / "security_map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(mapping["symbols"]["NABIL"], 131)

    def test_refresh_merges_with_older_cache(self):
        first = [
            row("2026-11-06", 554, 549, 549, 21117),
            row("2026-11-05", 553, 545, 550, 54860),
        ]
        second = [
            row("2026-11-09", 560, 549, 558, 30000),
            row("2026-11-06", 555, 548, 552, 25000),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeClient(first)
            source = make_source(tmp, fake)
            source.bars("NABIL")

            fake.rows = second
            bars = source.bars("NABIL", refresh=True)

            self.assertEqual(
                [b["date"] for b in bars], ["2026-11-05", "2026-11-06", "2026-11-09"]
            )
            self.assertEqual(bars[1]["close"], 552.0)  # newer copy wins
            self.assertEqual(bars[2]["open"], 552.0)
            self.assertEqual(bars[2]["close"], 558.0)

    def test_cache_ttl_avoids_repeat_fetches(self):
        rows = [row("2026-11-06", 554, 549, 549, 21117)]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeClient(rows)
            source = make_source(tmp, fake, cache_ttl=60)
            source.bars("NABIL")
            source.bars("NABIL")
            self.assertEqual(fake.history_calls, 1)
            source.bars("NABIL", refresh=True)
            self.assertEqual(fake.history_calls, 2)

    def test_future_dated_cache_rows_are_ignored(self):
        past = (date.today() - timedelta(days=10)).isoformat()
        future = (date.today() + timedelta(days=5)).isoformat()
        fresh_date = (date.today() - timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            data.write_csv(
                cache / "NABIL.csv",
                [
                    {
                        "date": past,
                        "open": 500.0,
                        "high": 501.0,
                        "low": 499.0,
                        "close": 500.0,
                        "volume": 1000.0,
                    },
                    {
                        "date": future,
                        "open": 900.0,
                        "high": 950.0,
                        "low": 890.0,
                        "close": 900.0,
                        "volume": 5000.0,
                    },
                ],
            )
            fake = FakeClient([row(fresh_date, 510, 500, 505, 2000)])
            source = make_source(tmp, fake, cache_ttl=0)
            bars = source.bars("NABIL", refresh=True)

            self.assertEqual([b["date"] for b in bars], [past, fresh_date])
            self.assertEqual(bars[-1]["close"], 505.0)

    def test_stale_cache_survives_a_failed_fetch(self):
        rows = [row("2026-11-06", 554, 549, 549, 21117)]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeClient(rows)
            source = make_source(tmp, fake, cache_ttl=0)
            first = source.bars("NABIL")

            def broken(security_id, start, end):
                raise nepse_api.NepseError("NEPSE is down")

            fake.history = broken
            second = source.bars("NABIL", refresh=True)
            self.assertEqual([b["date"] for b in second], [b["date"] for b in first])
            self.assertEqual(second[0]["close"], 549.0)

    def test_quote_and_cached_close_fallback(self):
        rows = [row("2026-11-06", 554, 549, 549, 21117)]
        quote = {
            "securityMcsData": {
                "lastTradedPrice": 551.5,
                "businessDate": "2026-11-09",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(tmp, FakeClient(rows, quote=quote), cache_ttl=60)
            self.assertEqual(source.last("NABIL"), (551.5, "2026-11-09"))

            failing = make_source(
                tmp, FakeClient(rows, fail_quote=True), cache_ttl=60
            )
            self.assertEqual(failing.last("NABIL"), (549.0, "2026-11-06"))

    def test_session_open_reads_the_quote_open_price(self):
        rows = [row("2026-11-06", 554, 549, 549, 21117)]
        quote = {
            "securityMcsData": {
                "openPrice": 551.0,
                "lastTradedPrice": 555.0,
                "businessDate": "2026-11-09",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(tmp, FakeClient(rows, quote=quote), cache_ttl=60)
            self.assertEqual(source.session_open("NABIL"), (551.0, "2026-11-09"))
            self.assertEqual(source.last("NABIL"), (555.0, "2026-11-09"))

            no_open = {
                "securityMcsData": {
                    "lastTradedPrice": 555.0,
                    "businessDate": "2026-11-09",
                }
            }
            missing = make_source(
                tmp, FakeClient(rows, quote=no_open), cache_ttl=60
            )
            self.assertIsNone(missing.session_open("NABIL"))

    def test_plan_uses_the_next_session_open_when_available(self):
        class StubSource:
            def __init__(self, opened, bars):
                self.opened = opened
                self._bars = bars

            def session_open(self, symbol):
                return self.opened

            def bars(self, symbol, refresh=False):
                return self._bars

        candidate = {"symbol": "NABIL", "date": "2026-11-06"}
        bars = [{"date": "2026-11-06"}, {"date": "2026-11-09"}]
        self.assertEqual(
            cli._entry_for_plan(StubSource((555.0, "2026-11-09"), bars), candidate),
            (555.0, "next session open"),
        )
        self.assertEqual(
            cli._entry_for_plan(StubSource((555.0, "2026-11-10"), bars), candidate),
            (555.0, "later session open (entry late)"),
        )
        self.assertIsNone(
            cli._entry_for_plan(StubSource((555.0, "2026-11-06"), bars), candidate)
        )

    def test_build_plan_accepts_an_entry_override(self):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        candidate = {
            "symbol": "NABIL",
            "setup": "breakout",
            "date": "2026-11-06",
            "close": 549.0,
            "entry": 555.0,
            "entry_basis": "next session open",
            "setup_low": 530.0,
            "atr": 9.5,
            "band_upper": 631.35,
        }
        result = plan.build_plan(candidate, cfg)
        self.assertEqual(result["entry"], 555.0)
        self.assertEqual(result["entry_basis"], "next session open")
        self.assertEqual(result["stop"], 542.5)

    def test_unknown_symbol_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(tmp, FakeClient([]))
            with self.assertRaises(data.DataError) as ctx:
                source.bars("NICA")
            self.assertIn("not in the NEPSE security list", str(ctx.exception))

    def test_open_source_recognizes_nepse(self):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg["data"]["cache_dir"] = ""
        cfg["data"]["kind"] = "nepse"
        self.assertIsInstance(data.open_source(cfg), data.NepseOfficialSource)
        for alias in ("official", "nepalstock"):
            cfg["data"]["kind"] = alias
            self.assertIsInstance(data.open_source(cfg), data.NepseOfficialSource)


if __name__ == "__main__":
    unittest.main()