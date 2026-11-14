"""Discord delivery for suggestion runs.

The webhook URL comes only from ``DISCORD_WEBHOOK_URL``; it is never printed,
logged or written to the database. A delivery failure is reported back to the
caller as a dict — it never raises into the API response.

    python -m nepse_trader.discord --dry-run   # print the payloads, send nothing
    python -m nepse_trader.discord --test      # send a sample message

Message layout: one summary embed (run time, symbol count, BUY/SELL/HOLD
counts), then one embed per suggestion, colour-coded green / red / grey.
Messages are chunked to Discord's limits (10 embeds, 6000 characters of embed
content, 2000 characters of content) and 429 or 5xx responses are retried with
the server's ``retry_after`` and exponential backoff.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENV_VAR = "DISCORD_WEBHOOK_URL"
CHANNEL = "discord"

MAX_EMBEDS = 10
MAX_CONTENT = 2000
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_CHARS = 6000  # Discord's per-message combined embed limit
MAX_FIELD_VALUE = 1024
MAX_ATTEMPTS = 4
TIMEOUT = 15
USER_AGENT = "nepse-trader/1.0 (+suggestions)"

COLORS = {"BUY": 0x2ECC71, "SELL": 0xE74C3C, "HOLD": 0x95A5A6}
SUMMARY_COLOR = 0x3498DB
FOOTER = "Paper-trading suggestions from a simulated Time Machine. Not financial advice."

RETRY_STATUS = {429, 500, 502, 503, 504}


class DiscordError(RuntimeError):
    """Delivery failed. The message never contains the webhook URL."""


def webhook_url():
    return (os.environ.get(ENV_VAR) or "").strip() or None


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _now():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _reason(item):
    """The shortest useful explanation for a suggestion."""
    lines = [str(line) for line in (item.get("reasoning") or []) if str(line).strip()]
    action = str(item.get("action") or "HOLD").upper()
    picked = [line for line in lines if line.startswith(action + " ")]
    if not picked:
        picked = [line for line in lines if "score" in line.lower()] or lines[-1:]
    return _clip(" • ".join(picked[:2]), MAX_EMBED_DESCRIPTION)


def _price(value):
    if value is None:
        return "n/a"
    try:
        return "Rs %.2f" % float(value)
    except (TypeError, ValueError):
        return "n/a"


def _confidence(value):
    try:
        return "%.0f%%" % (100.0 * float(value))
    except (TypeError, ValueError):
        return "0%"


def suggestion_embed(item):
    action = str(item.get("action") or "HOLD").upper()
    if action not in COLORS:
        action = "HOLD"
    return {
        "title": _clip("%s %s" % (action, item.get("symbol") or "?"), 256),
        "color": COLORS[action],
        "description": _reason(item) or "No reasoning recorded.",
        "fields": [
            {"name": "Price", "value": _price(item.get("currentPrice")), "inline": True},
            {"name": "Quantity", "value": str(int(item.get("quantity") or 0)), "inline": True},
            {"name": "Confidence", "value": _confidence(item.get("confidence")), "inline": True},
        ],
    }


def counts(suggestions):
    tally = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for item in suggestions:
        action = str(item.get("action") or "HOLD").upper()
        tally[action if action in tally else "HOLD"] += 1
    return tally


def summary_embed(run, suggestions):
    tally = counts(suggestions)
    market = run.get("market") or {}
    return {
        "title": "Suggestion run",
        "color": SUMMARY_COLOR,
        "timestamp": run.get("generated_at") or _now(),
        "fields": [
            {
                "name": "Run",
                "value": _clip(run.get("run_id") or "unknown", MAX_FIELD_VALUE),
                "inline": False,
            },
            {
                "name": "Symbols",
                "value": "%d (%d BUY, %d SELL, %d HOLD)"
                % (len(suggestions), tally["BUY"], tally["SELL"], tally["HOLD"]),
                "inline": True,
            },
            {
                "name": "Market",
                "value": _clip(market.get("status") or "unknown", MAX_FIELD_VALUE),
                "inline": True,
            },
        ],
        "footer": {"text": FOOTER},
    }


def embed_chars(embed):
    total = len(embed.get("title") or "") + len(embed.get("description") or "")
    for field in embed.get("fields") or []:
        total += len(field.get("name") or "") + len(field.get("value") or "")
    total += len((embed.get("footer") or {}).get("text") or "")
    return total


def build_payloads(run, suggestions, max_embeds=MAX_EMBEDS, max_chars=MAX_EMBED_CHARS):
    """Summary first, then one embed per suggestion, chunked to Discord limits."""
    embeds = [summary_embed(run, suggestions)]
    embeds.extend(suggestion_embed(item) for item in suggestions)

    groups, current, used = [], [], 0
    for embed in embeds:
        size = embed_chars(embed)
        if current and (len(current) >= max_embeds or used + size > max_chars):
            groups.append(current)
            current, used = [], 0
        current.append(embed)
        used += size
    if current:
        groups.append(current)

    payloads = []
    for index, group in enumerate(groups, 1):
        suffix = "" if len(groups) == 1 else " (%d/%d)" % (index, len(groups))
        payloads.append(
            {
                "content": _clip("Suggestion run %s%s" % (run.get("run_id") or "", suffix), MAX_CONTENT),
                "embeds": group,
                "allowed_mentions": {"parse": []},
            }
        )
    return payloads


def _retry_after(exc, attempt):
    raw = None
    try:
        raw = exc.headers.get("Retry-After")
    except AttributeError:
        raw = None
    if raw is None:
        try:
            raw = json.loads(exc.read().decode("utf-8")).get("retry_after")
        except Exception:
            raw = None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = min(2.0 ** attempt, 8.0)
    return max(0.0, min(delay, 30.0))


def post(payload, url=None, opener=None, sleep=time.sleep, attempts=MAX_ATTEMPTS):
    """POST one payload, retrying 429/5xx with the server's backoff hint."""
    url = url or webhook_url()
    if not url:
        raise DiscordError("%s is not set" % ENV_VAR)
    opener = opener or urllib.request.urlopen
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with opener(request, timeout=TIMEOUT) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_STATUS and attempt < attempts:
                sleep(_retry_after(exc, attempt))
                continue
            raise DiscordError("discord returned HTTP %d" % exc.code)
        except OSError:
            # URLError, DNS failures, timeouts — all OSError subclasses.
            if attempt < attempts:
                sleep(min(2.0 ** attempt, 8.0))
                continue
            raise DiscordError("discord is unreachable")
    raise DiscordError("discord did not accept the message")


def send_payloads(payloads, url=None, opener=None, sleep=time.sleep):
    url = url or webhook_url()
    sent = 0
    for payload in payloads:
        post(payload, url=url, opener=opener, sleep=sleep)
        sent += 1
    return {"sent": bool(payloads), "status": "sent", "messages": sent, "reason": ""}


def send_run(cfg, run_id, suggestions_list):
    """Build and deliver a run. Returns a dict, never raises."""
    url = webhook_url()
    if not url:
        return {
            "sent": False,
            "status": "skipped",
            "reason": "%s is not set" % ENV_VAR,
            "messages": 0,
        }
    market = None
    for item in suggestions_list:
        market = (item.get("signals") or {}).get("market")
        if market:
            break
    run = {"run_id": run_id, "generated_at": _now(), "market": market or {}}
    payloads = build_payloads(run, suggestions_list)
    try:
        result = send_payloads(payloads, url=url)
    except DiscordError as exc:
        return {
            "sent": False,
            "status": "error",
            "reason": str(exc)[:300],
            "messages": 0,
        }
    except Exception as exc:  # never, ever break the caller
        return {
            "sent": False,
            "status": "error",
            "reason": "discord delivery failed (%s)" % exc.__class__.__name__,
            "messages": 0,
        }
    result["reason"] = ""
    return result


def _sample_suggestions():
    return [
        {
            "symbol": "NABIL",
            "action": "BUY",
            "quantity": 10,
            "currentPrice": 431.02,
            "confidence": 0.72,
            "reasoning": [
                "SMA20 402.20 vs SMA50 417.20: trend score +0.31",
                "BUY 10 shares (1 lot) at Rs 431.02, stop Rs 405.00, target Rs 496.00",
            ],
            "signals": {"market": {"status": "closed (weekend)"}},
        },
        {
            "symbol": "NICA",
            "action": "SELL",
            "quantity": 20,
            "currentPrice": 812.5,
            "confidence": 0.64,
            "reasoning": ["SELL 20 of 20 held shares at Rs 812.50 (never more than held)"],
            "signals": {"market": {"status": "closed (weekend)"}},
        },
        {
            "symbol": "NLIC",
            "action": "HOLD",
            "quantity": 0,
            "currentPrice": 1040.0,
            "confidence": 0.41,
            "reasoning": ["HOLD: score +0.12 is inside the +/-0.25 action band"],
            "signals": {"market": {"status": "closed (weekend)"}},
        },
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nepse-trader-discord", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="send a sample message")
    group.add_argument(
        "--dry-run", action="store_true", help="print the payloads, send nothing"
    )
    parser.add_argument(
        "--env-file", default=None, help="load env vars from this file first"
    )
    args = parser.parse_args(argv)

    from . import config as config_mod
    from . import db

    if args.env_file:
        db.load_env_files(args.env_file)
    else:
        db.load_env_files(".env", ".env.local")

    run = {"run_id": "sample-" + _now(), "generated_at": _now(), "market": {"status": "closed (weekend)"}}
    suggestions = _sample_suggestions()

    if args.dry_run:
        payloads = build_payloads(run, suggestions)
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
        print(
            "dry run: %d message(s), %d embed(s), webhook %s"
            % (
                len(payloads),
                sum(len(payload["embeds"]) for payload in payloads),
                "configured" if webhook_url() else "not configured",
            ),
            file=sys.stderr,
        )
        return 0

    if not webhook_url():
        print("%s is not set" % ENV_VAR, file=sys.stderr)
        return 2
    result = send_run(config_mod.DEFAULTS, run["run_id"], suggestions)
    print(json.dumps(result, indent=2))
    return 0 if result.get("sent") else 1


if __name__ == "__main__":
    raise SystemExit(main())
