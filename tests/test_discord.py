"""Discord delivery tests. The webhook is always faked — nothing hits Discord."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from nepse_trader import config, discord


def suggestion(symbol="NABIL", action="BUY", quantity=10, price=431.02, confidence=0.72):
    return {
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "currentPrice": price,
        "confidence": confidence,
        "reasoning": [
            "SMA20 402.20 vs SMA50 417.20: trend score +0.31",
            "%s %d shares at Rs %.2f, stop Rs 405.00, target Rs 496.00"
            % (action, quantity, price),
        ],
        "signals": {"market": {"status": "closed (weekend)"}},
    }


def run(run_id="run-1"):
    return {"run_id": run_id, "generated_at": "2026-11-14T17:00:00Z",
            "market": {"status": "closed (weekend)"}}


class FakeResponse:
    status = 200

    def __init__(self, status=200):
        self.status = status

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Returns or raises the queued outcomes; records every request."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome)


def http_error(code):
    return urllib.error.HTTPError(
        "http://h/hook",
        code,
        "fake",
        {"Retry-After": "0.25"},
        io.BytesIO(b'{"retry_after": 0.25}'),
    )


class PayloadTests(unittest.TestCase):
    def test_summary_comes_first_with_counts_and_footer(self):
        payload = discord.build_payloads(
            run(), [suggestion(action="BUY"), suggestion("NICA", "SELL", 20), suggestion("NLIC", "HOLD", 0)]
        )[0]
        summary = payload["embeds"][0]
        self.assertEqual(summary["title"], "Suggestion run")
        self.assertIn("3 (1 BUY, 1 SELL, 1 HOLD)", summary["fields"][1]["value"])
        self.assertIn("Not financial advice", summary["footer"]["text"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_one_embed_per_suggestion_with_reason_price_quantity_confidence(self):
        payload = discord.build_payloads(run(), [suggestion()])[0]
        embed = payload["embeds"][1]
        self.assertEqual(embed["title"], "BUY NABIL")
        self.assertEqual(embed["color"], discord.COLORS["BUY"])
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["Price"], "Rs 431.02")
        self.assertEqual(fields["Quantity"], "10")
        self.assertEqual(fields["Confidence"], "72%")
        self.assertIn("BUY 10 shares", embed["description"])

    def test_colours_per_action(self):
        for action, color in (("BUY", 0x2ECC71), ("SELL", 0xE74C3C), ("HOLD", 0x95A5A6)):
            self.assertEqual(
                discord.suggestion_embed(suggestion(action=action))["color"], color
            )
        self.assertEqual(discord.suggestion_embed({"action": "?"})["color"], 0x95A5A6)

    def test_missing_price_and_confidence_are_safe(self):
        embed = discord.suggestion_embed({"symbol": "X", "action": "HOLD"})
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["Price"], "n/a")
        self.assertEqual(fields["Confidence"], "0%")
        self.assertEqual(embed["description"], "No reasoning recorded.")

    def test_chunks_at_ten_embeds(self):
        for count, expected in ((0, [1]), (9, [10]), (10, [10, 1]), (25, [10, 10, 6])):
            payloads = discord.build_payloads(run(), [suggestion("S%02d" % i) for i in range(count)])
            self.assertEqual([len(p["embeds"]) for p in payloads], expected, count)
            self.assertTrue(all(len(p["embeds"]) <= discord.MAX_EMBEDS for p in payloads))

    def test_chunks_on_total_embed_characters(self):
        long = suggestion()
        long["reasoning"] = ["x" * 4096]
        payloads = discord.build_payloads(run(), [long] * 5)
        for payload in payloads:
            self.assertLessEqual(
                sum(discord.embed_chars(embed) for embed in payload["embeds"]),
                discord.MAX_EMBED_CHARS,
            )

    def test_truncates_oversized_text(self):
        long = suggestion()
        long["reasoning"] = ["y" * 9000]
        embed = discord.suggestion_embed(long)
        self.assertLessEqual(len(embed["description"]), discord.MAX_EMBED_DESCRIPTION)
        payload = discord.build_payloads({"run_id": "r" * 5000}, [long])[0]
        self.assertLessEqual(len(payload["content"]), discord.MAX_CONTENT)

    def test_multi_message_content_is_numbered(self):
        payloads = discord.build_payloads(run(), [suggestion("S%02d" % i) for i in range(25)])
        self.assertTrue(payloads[0]["content"].endswith("(1/3)"))
        self.assertTrue(payloads[-1]["content"].endswith("(3/3)"))


class PostTests(unittest.TestCase):
    def test_posts_json_to_the_webhook(self):
        opener = FakeOpener([200])
        discord.post({"content": "hi"}, url="http://127.0.0.1:9/hook", opener=opener)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(json.loads(request.data), {"content": "hi"})
        self.assertEqual(request.get_method(), "POST")

    def test_retries_429_with_the_server_hint(self):
        opener = FakeOpener([http_error(429), 200])
        sleeps = []
        discord.post({"content": "x"}, url="http://h/hook", opener=opener, sleep=sleeps.append)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(sleeps, [0.25])

    def test_retries_5xx_then_passes(self):
        opener = FakeOpener([http_error(500), http_error(502), 200])
        sleeps = []
        discord.post({"content": "x"}, url="http://h/hook", opener=opener, sleep=sleeps.append)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(len(sleeps), 2)

    def test_gives_up_after_four_attempts(self):
        opener = FakeOpener([http_error(429)] * 4)
        with self.assertRaises(discord.DiscordError):
            discord.post(
                {"content": "x"}, url="http://h/hook", opener=opener, sleep=lambda _: None
            )
        self.assertEqual(len(opener.requests), 4)

    def test_non_retryable_error_fails_fast(self):
        opener = FakeOpener([http_error(401)])
        with self.assertRaises(discord.DiscordError):
            discord.post({"content": "x"}, url="http://h/hook", opener=opener, sleep=lambda _: None)
        self.assertEqual(len(opener.requests), 1)

    def test_network_error_is_retried(self):
        opener = FakeOpener([urllib.error.URLError("down"), 200])
        sleeps = []
        discord.post({"content": "x"}, url="http://h/hook", opener=opener, sleep=sleeps.append)
        self.assertEqual(len(opener.requests), 2)

    def test_errors_never_leak_the_webhook_url(self):
        url = "http://127.0.0.1:9/secret-token"
        opener = FakeOpener([http_error(403)])
        with self.assertRaises(discord.DiscordError) as caught:
            discord.post({"content": "x"}, url=url, opener=opener)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn(url, str(caught.exception))


class SendRunTests(unittest.TestCase):
    def test_missing_webhook_is_skipped_not_raised(self):
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": ""}):
            result = discord.send_run(config.DEFAULTS, "run-1", [suggestion()])
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["sent"])
        self.assertIn("DISCORD_WEBHOOK_URL", result["reason"])

    def test_sends_every_chunk(self):
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "http://h/hook"}), patch.object(
            discord, "send_payloads", return_value={"sent": True, "status": "sent", "messages": 2}
        ) as sender:
            result = discord.send_run(config.DEFAULTS, "run-1", [suggestion("S%02d" % i) for i in range(25)])
        self.assertTrue(result["sent"])
        self.assertEqual(sender.call_count, 1)
        payloads = sender.call_args[0][0]
        self.assertEqual(len(payloads), 3)

    def test_delivery_failure_is_returned_as_a_dict(self):
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "http://h/hook"}), patch.object(
            discord, "post", side_effect=discord.DiscordError("discord returned HTTP 500")
        ):
            result = discord.send_run(config.DEFAULTS, "run-1", [suggestion()])
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["sent"])
        self.assertNotIn("h/hook", result["reason"])

    def test_unexpected_exception_does_not_escape(self):
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "http://h/hook"}), patch.object(
            discord, "post", side_effect=RuntimeError("boom")
        ):
            result = discord.send_run(config.DEFAULTS, "run-1", [suggestion()])
        self.assertEqual(result["status"], "error")
        self.assertIn("RuntimeError", result["reason"])


if __name__ == "__main__":
    unittest.main()
