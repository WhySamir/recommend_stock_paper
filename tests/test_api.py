"""Offline tests for the JSON API (no network, CSV source)."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import date
from unittest.mock import patch
from pathlib import Path

from nepse_trader import alerts, api, config, data
from tests.test_offline import bars_for_test


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data_dir = root / "data"
        data_dir.mkdir()
        data.write_csv(data_dir / "NABIL.csv", bars_for_test())

        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg["data"]["dir"] = str(data_dir)
        cfg["data"]["cache_dir"] = str(data_dir)
        cfg["data"]["kind"] = "csv"
        cfg["state_dir"] = str(root / "trader")
        cfg["universe"] = ["NABIL"]
        cfg["timemachine"]["iterations"] = 300
        cls.cfg = cfg
        cls.alerts_path = alerts.path_for(cfg)

        cls.server = api.build_server(cfg, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def request(self, path, method="GET", body=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if origin is not None:
            headers["Origin"] = origin
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        headers = dict(response.getheaders())
        conn.close()
        return response.status, json.loads(body), headers

    def test_health_and_index(self):
        status, body, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body, _ = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("endpoints", body)

    def test_cors_allows_configured_origins_only(self):
        _, _, headers = self.request(
            "/api/health", origin="http://localhost:3000"
        )
        self.assertEqual(
            headers.get("Access-Control-Allow-Origin"), "http://localhost:3000"
        )

        _, _, headers = self.request("/api/health", origin="https://evil.example")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")

    def test_cors_reads_frontend_origin_from_environment(self):
        with patch.dict(
            os.environ,
            {"FRONTEND_CORS_ORIGIN": "https://frontend.example/"},
        ):
            server = api.build_server(self.cfg, "127.0.0.1", 0)
        try:
            self.assertEqual(
                server.RequestHandlerClass.cors_origins,
                frozenset({"http://localhost:3000", "https://frontend.example"}),
            )
        finally:
            server.server_close()

    def test_bars_returns_the_full_history(self):
        status, body, _ = self.request("/api/bars/NABIL")
        self.assertEqual(status, 200)
        self.assertEqual(body["symbol"], "NABIL")
        self.assertEqual(body["data"]["count"], len(body["bars"]))
        self.assertGreater(body["data"]["count"], 100)
        status, body, _ = self.request("/api/bars/NABIL?limit=5")
        self.assertEqual(len(body["bars"]), 5)

    def test_analyze_contains_every_stage(self):
        status, body, _ = self.request("/api/analyze/NABIL?iterations=300")
        self.assertEqual(status, 200)
        self.assertEqual(body["verdict"], "PASS")
        self.assertTrue(body["setups"])
        self.assertIsNotNone(body["plan"])
        self.assertIsNotNone(body["sizing"])
        self.assertIsNotNone(body["risk"])
        self.assertIn("history", body["timemachine"])
        self.assertIn("simulation", body["timemachine"])
        self.assertIn("bars", body)
        self.assertEqual(body["risk"]["verdict"], "PASS")

    def test_manual_levels_fill_every_stage(self):
        status, body, _ = self.request(
            "/api/analyze/NABIL?bars=0&iterations=200&entry=640&stop=620&target=700"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["plan_source"], "manual")
        self.assertIsNotNone(body["plan"])
        self.assertIsNotNone(body["sizing"])
        self.assertIsNotNone(body["risk"])
        self.assertIsNotNone(body["timemachine"])
        self.assertEqual(body["plan"]["entry"], 640.0)
        self.assertEqual(body["plan"]["stop"], 620.0)
        self.assertEqual(body["plan"]["target"], 700.0)
        self.assertIn(body["verdict"], ("PASS", "BLOCK"))

    def test_target_defaults_from_target_r(self):
        _, body, _ = self.request(
            "/api/analyze/NABIL?bars=0&iterations=100&entry=640&stop=620"
        )
        self.assertEqual(body["plan"]["target"], 690.0)  # 2.5R by default

    def test_bad_levels_are_400(self):
        status, body, _ = self.request(
            "/api/analyze/NABIL?bars=0&entry=620&stop=640"
        )
        self.assertEqual(status, 400)
        self.assertIn("stop", body["error"])
        status, body, _ = self.request("/api/analyze/NABIL?bars=0&entry=640")
        self.assertEqual(status, 400)

    def test_analyze_can_omit_bars(self):
        _, body, _ = self.request("/api/analyze/NABIL?bars=0&iterations=200")
        self.assertNotIn("bars", body)
        self.assertEqual(body["symbol"], "NABIL")

    def test_scan_reports_candidates(self):
        status, body, _ = self.request("/api/scan?symbols=NABIL")
        self.assertEqual(status, 200)
        self.assertEqual(body["scanned"], 1)
        self.assertTrue(body["candidates"])
        self.assertEqual(body["errors"], [])

    def test_report_bundles_symbols(self):
        status, body, _ = self.request("/api/report?symbols=NABIL&bars=1&iterations=200")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 1)
        self.assertIn("bars", body["results"][0])

    def test_cache_inventory(self):
        status, body, _ = self.request("/api/cache")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["symbols_cached"], 1)
        self.assertEqual(body["symbols"][0]["symbol"], "NABIL")

    def test_cache_all_returns_every_symbol_with_bars(self):
        status, body, _ = self.request("/api/cache/all")
        self.assertEqual(status, 200)
        self.assertEqual(body["symbols_cached"], 1)
        entry = body["symbols"][0]
        self.assertEqual(entry["symbol"], "NABIL")
        self.assertIn("bars", entry)
        self.assertEqual(len(entry["bars"]), entry["count"])

        status, body, _ = self.request("/api/cache/all?bars=0")
        self.assertEqual(status, 200)
        self.assertNotIn("bars", body["symbols"][0])

    def test_raw_csv_download(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", "/api/cache/NABIL.csv")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        headers = dict(response.getheaders())
        status = response.status
        conn.close()
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertTrue(body.startswith("date,open,high,low,close,volume"))
        self.assertIn("2026", body)

    def test_raw_csv_missing_is_404(self):
        status, body, _ = self.request("/api/cache/NOPE.csv")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_symbols_endpoint(self):
        status, body, _ = self.request("/api/symbols")
        self.assertEqual(status, 200)
        self.assertIn("NABIL", body["universe"])

    def test_custom_simulation_is_saved_separately_from_cache(self):
        status, body, _ = self.request(
            "/api/simulations",
            method="POST",
            body={"symbol": "NABIL", "action": "buy", "shares": 100, "days": 4, "iterations": 30, "seed": 7},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["shares"], 100)
        self.assertEqual(body["days"], 4)
        self.assertIn("expected_net_pnl", body["forecast"])
        self.assertTrue(Path(body["simulation_dir"], "result.json").is_file())
        status, saved, _ = self.request("/api/simulations/" + body["id"])
        self.assertEqual(status, 200)
        self.assertEqual(saved["id"], body["id"])
        status, listing, _ = self.request("/api/simulations")
        self.assertEqual(status, 200)
        self.assertTrue(any(row["id"] == body["id"] for row in listing["simulations"]))

    def test_unknown_symbol_is_404_json(self):
        status, body, _ = self.request("/api/analyze/NOPE")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_unknown_route_is_404(self):
        status, body, _ = self.request("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_bad_integer_is_400(self):
        status, body, _ = self.request("/api/bars/NABIL?limit=abc")
        self.assertEqual(status, 400)
        self.assertIn("limit", body["error"])

    def test_alert_check_post_fires_once(self):
        store = alerts.load(self.alerts_path)
        alerts.add(store, "NABIL", "above", 1.0, "test above")
        alerts.save(self.alerts_path, store)

        status, body, _ = self.request("/api/alerts/check", method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["fired"]), 1)
        self.assertEqual(body["armed"], 0)

        status, body, _ = self.request("/api/alerts/check", method="POST")
        self.assertEqual(len(body["fired"]), 0)

    def test_post_elsewhere_is_405(self):
        status, _, _ = self.request("/api/scan", method="POST")
        self.assertEqual(status, 405)


class FutureSimulationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data_dir = root / "data"
        data_dir.mkdir()
        closes = [574.0, 576.0, 600.0, 605.0, 610.0, 615.0, 620.0]
        dates = [
            "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25",
            "2026-11-26", "2026-11-27", "2026-11-30",
        ]
        bars = [
            {
                "date": day,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000.0,
            }
            for day, close in zip(dates, closes)
        ]
        data.write_csv(data_dir / "NABIL.csv", bars)

        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg["data"]["dir"] = str(data_dir)
        cfg["data"]["cache_dir"] = str(data_dir)
        cfg["data"]["kind"] = "csv"
        cfg["state_dir"] = str(root / "trader")
        cfg["universe"] = ["NABIL"]
        cls.cfg = cfg
        cls.today = date(2026, 11, 20)
        cls.patcher = patch(
            "nepse_trader.future_simulations.today_npt",
            side_effect=lambda *args, **kwargs: cls.today,
        )
        cls.patcher.start()
        cls.server = api.build_server(cfg, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.patcher.stop()
        cls.tmp.cleanup()

    def request(self, path, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, payload

    def test_log_settle_and_list_future_simulation(self):
        status, body = self.request(
            "/api/future-simulations",
            method="POST",
            body={
                "symbol": "NABIL",
                "action": "buy",
                "shares": 10,
                "entry_price": 574,
                "days": [4, 10],
                "iterations": 50,
                "seed": 7,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            [h["due_date"] for h in body["horizons"]], ["2026-11-24", "2026-11-30"]
        )
        self.assertTrue(Path(body["simulation_dir"], body["id"] + ".json").is_file())

        self.__class__.today = date(2026, 11, 30)
        status, settled = self.request("/api/future-simulations/" + body["id"])
        self.assertEqual(status, 200)
        self.assertEqual([h["status"] for h in settled["horizons"]], ["settled", "settled"])
        self.assertEqual(settled["status"], "settled")

        status, listing = self.request("/api/future-simulations")
        self.assertEqual(status, 200)
        self.assertTrue(any(row["id"] == body["id"] for row in listing["simulations"]))

    def test_invalid_horizon_is_400(self):
        status, error = self.request(
            "/api/future-simulations",
            method="POST",
            body={"symbol": "NABIL", "shares": 1, "price": 10, "days": [0]},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", error)


if __name__ == "__main__":
    unittest.main()
