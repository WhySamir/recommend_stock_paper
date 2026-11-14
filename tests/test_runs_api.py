"""Integration tests for POST /api/suggestions against the Neon database.

They run only when ``NEON_CONN_URL`` is available (environment or ``.env``) and
skip otherwise, so the suite still passes offline. Every run created here is
deleted again, and watchlist symbols added by a test are removed.

Discord is always mocked: no test here can post to a real webhook.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from nepse_trader import alerts, api, config, data, db, store
from tests.test_offline import bars_for_test

ROOT = Path(__file__).resolve().parent.parent
SECRET = "test-secret-" + uuid.uuid4().hex[:8]


class RunsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.load_env_files(ROOT / ".env", ROOT / ".env.local")
        if not db.enabled():
            raise unittest.SkipTest("NEON_CONN_URL is not set; skipping DB integration tests")

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data_dir = root / "data"
        data_dir.mkdir()
        data.write_csv(data_dir / "NABIL.csv", bars_for_test())

        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg["data"].update(
            {"kind": "csv", "dir": str(data_dir), "cache_dir": str(data_dir)}
        )
        cfg["state_dir"] = str(root / "trader")
        cfg["timemachine"]["iterations"] = 100
        cfg["suggest"]["lookback_days"] = 60
        cls.cfg = cfg
        cls.alerts_path = alerts.path_for(cfg)

        cls._saved_secret = os.environ.get("CRON_SECRET")
        cls._saved_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        os.environ["CRON_SECRET"] = SECRET
        os.environ["DISCORD_WEBHOOK_URL"] = ""

        cls.server = api.build_server(cfg, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()
        for name, value in (
            ("CRON_SECRET", cls._saved_secret),
            ("DISCORD_WEBHOOK_URL", cls._saved_webhook),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def setUp(self):
        self.run_ids = []
        self.added_symbols = []

    def tearDown(self):
        if self.run_ids:
            db.execute(
                "delete from notification_log where run_id = any(%s)",
                (list(self.run_ids),),
            )
            db.execute(
                "delete from suggestion_runs where id = any(%s)", (list(self.run_ids),)
            )
        for symbol in self.added_symbols:
            store.remove_watchlist_symbol(symbol)
        self.assertEqual(
            db.scalar(
                "select count(*) from suggestion_runs where id = any(%s)",
                (list(self.run_ids) or [""],),
            ),
            0,
        )

    # -- helpers ------------------------------------------------------

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        conn.request(
            method,
            path,
            None if body is None else json.dumps(body),
            {"Content-Type": "application/json", **(headers or {})},
        )
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        # Register immediately: a failing assertion later must not leak a run.
        if isinstance(parsed, dict) and parsed.get("run_id"):
            if parsed["run_id"] not in self.run_ids:
                self.run_ids.append(parsed["run_id"])
        return response.status, parsed

    def auth(self, key=None):
        headers = {"Authorization": "Bearer " + SECRET}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def track(self, body):
        self.run_ids.append(body["run_id"])
        return body

    # -- auth ---------------------------------------------------------

    def test_auth_is_required(self):
        status, body = self.call("POST", "/api/suggestions", {"symbols": ["NABIL"]})
        self.assertEqual(status, 401)
        self.assertIn("Authorization", body["error"])

        status, _ = self.call(
            "POST", "/api/suggestions", {"symbols": ["NABIL"]},
            {"Authorization": "Bearer wrong-secret"},
        )
        self.assertEqual(status, 401)

        status, _ = self.call(
            "POST", "/api/suggestions", {"symbols": ["NABIL"]},
            {"Authorization": SECRET},  # no "Bearer " prefix
        )
        self.assertEqual(status, 401)

    def test_missing_server_secret_is_503(self):
        os.environ["CRON_SECRET"] = ""
        try:
            status, body = self.call(
                "POST", "/api/suggestions", {"symbols": ["NABIL"]}, self.auth()
            )
        finally:
            os.environ["CRON_SECRET"] = SECRET
        self.assertEqual(status, 503)
        self.assertIn("CRON_SECRET", body["error"])

    # -- validation ---------------------------------------------------

    def test_validation_errors_are_400(self):
        cases = [
            {"symbols": "NABIL"},
            {"symbols": [1, 2]},
            {"symbols": ["BAD SYMBOL!"]},
            {"symbols": ["S%02d" % i for i in range(51)]},
            {"symbols": ["NABIL"], "notify": "yes"},
            {"symbols": ["NABIL"], "notify": 1},
        ]
        for body in cases:
            status, payload = self.call("POST", "/api/suggestions", body, self.auth())
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)

    def test_body_must_be_an_object(self):
        status, _ = self.call("POST", "/api/suggestions", ["NABIL"], self.auth())
        self.assertEqual(status, 400)

    def test_duplicate_symbols_are_collapsed(self):
        status, body = self.call(
            "POST", "/api/suggestions", {"symbols": ["nabil", "NABIL", " "]} , self.auth()
        )
        self.assertEqual(status, 201)
        self.track(body)
        self.assertEqual(body["symbols"], ["NABIL"])

    # -- success path -------------------------------------------------

    def test_success_persists_the_run_and_its_suggestions(self):
        status, body = self.call(
            "POST", "/api/suggestions", {"symbols": ["NABIL"]}, self.auth()
        )
        self.assertEqual(status, 201)
        self.track(body)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["symbols"], ["NABIL"])
        self.assertEqual(len(body["suggestions"]), 1)
        self.assertIn(body["suggestions"][0]["action"], ("BUY", "SELL", "HOLD"))
        self.assertIsNotNone(body["market"])
        self.assertIsNotNone(body["portfolio"])
        self.assertTrue(body["disclaimer"])
        self.assertFalse(body["idempotent"])

        row = db.fetch_all(
            "select status, symbols, result from suggestion_runs where id = %s",
            (body["run_id"],),
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0][0], "ok")
        self.assertEqual(list(row[0][1]), ["NABIL"])
        self.assertIn("market", row[0][2])
        stored = db.fetch_all(
            "select symbol, action from suggestions where run_id = %s",
            (body["run_id"],),
        )
        self.assertEqual([item[0] for item in stored], ["NABIL"])

    def test_latest_returns_the_newest_run(self):
        _, body = self.call("POST", "/api/suggestions", {"symbols": ["NABIL"]}, self.auth())
        self.track(body)
        status, latest = self.call("GET", "/api/suggestions/latest")
        self.assertEqual(status, 200)
        self.assertEqual(latest["run_id"], body["run_id"])
        self.assertEqual(len(latest["suggestions"]), 1)

    def test_idempotency_key_returns_the_same_run(self):
        key = "test-" + uuid.uuid4().hex
        status, first = self.call(
            "POST", "/api/suggestions", {"symbols": ["NABIL"]}, self.auth(key)
        )
        self.assertEqual(status, 201)
        self.track(first)

        status, second = self.call(
            "POST", "/api/suggestions", {"symbols": ["NABIL"]}, self.auth(key)
        )
        self.assertEqual(status, 200)
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(len(second["suggestions"]), 1)

        rows = db.scalar(
            "select count(*) from suggestion_runs where idempotency_key = %s", (key,)
        )
        self.assertEqual(rows, 1)

    def test_oversized_idempotency_key_is_rejected(self):
        status, _ = self.call(
            "POST", "/api/suggestions", {"symbols": ["NABIL"]}, self.auth("k" * 201)
        )
        self.assertEqual(status, 400)

    # -- watchlist ----------------------------------------------------

    def test_watchlist_crud_and_run_fallback(self):
        symbol = "ZZTEST1"
        self.added_symbols.extend([symbol, "ZZTEST2"])
        status, body = self.call(
            "POST", "/api/watchlist", {"symbols": [symbol, "ZZTEST2"]}
        )
        self.assertEqual(status, 200)
        self.assertIn(symbol, body["symbols"])

        status, listed = self.call("GET", "/api/watchlist")
        self.assertEqual(status, 200)
        self.assertIn(symbol, listed["symbols"])

        status, body = self.call("POST", "/api/suggestions", {}, self.auth())
        self.assertEqual(status, 201)
        self.track(body)
        self.assertIn(symbol, body["symbols"])

        status, body = self.call("DELETE", "/api/watchlist/ZZTEST2")
        self.assertEqual(status, 200)
        self.assertTrue(body["removed"])
        self.assertNotIn("ZZTEST2", body["symbols"])
        self.added_symbols.remove("ZZTEST2")

        status, body = self.call("DELETE", "/api/watchlist/ZZTEST2")
        self.assertEqual(status, 200)
        self.assertFalse(body["removed"])

    def test_watchlist_add_needs_symbols(self):
        status, _ = self.call("POST", "/api/watchlist", {"symbols": []})
        self.assertEqual(status, 400)

    # -- notifications ------------------------------------------------

    def test_notify_is_skipped_without_a_webhook(self):
        status, body = self.call(
            "POST", "/api/suggestions", {"symbols": ["NABIL"], "notify": True}, self.auth()
        )
        self.assertEqual(status, 201)
        self.track(body)
        self.assertFalse(body["notified"])
        self.assertEqual(body["notification"]["status"], "skipped")

    def test_notify_sends_once_per_run(self):
        from nepse_trader import runs

        sent = {"sent": True, "status": "sent", "messages": 1, "reason": ""}
        with patch("nepse_trader.discord.send_run", return_value=sent) as sender:
            status, body = self.call(
                "POST", "/api/suggestions",
                {"symbols": ["NABIL"], "notify": True}, self.auth(),
            )
            self.assertEqual(status, 201)
            self.track(body)
            self.assertTrue(body["notified"])
            self.assertEqual(body["notification"]["status"], "sent")
            self.assertEqual(sender.call_count, 1)

            runs._notify(self.cfg, body["run_id"], body["suggestions"])
            self.assertEqual(sender.call_count, 1)  # already sent, no second post

        sent_rows = db.scalar(
            "select count(*) from notification_log where run_id = %s and status = 'sent'",
            (body["run_id"],),
        )
        self.assertEqual(sent_rows, 1)

    def test_discord_failure_does_not_break_the_response(self):
        with patch(
            "nepse_trader.discord.send_run", side_effect=RuntimeError("webhook down")
        ):
            status, body = self.call(
                "POST", "/api/suggestions",
                {"symbols": ["NABIL"], "notify": True}, self.auth(),
            )
        self.assertEqual(status, 201)
        self.track(body)
        self.assertFalse(body["notified"])
        self.assertEqual(body["notification"]["status"], "error")
        self.assertEqual(len(body["suggestions"]), 1)


if __name__ == "__main__":
    unittest.main()
