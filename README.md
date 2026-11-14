# NEPSE trading engine

The trading logic of the [Day Trader skill](https://github.com/Jakeschincariol/day-trader-skill)
(scan → entry → exit → Time Machine → budget → risk → alerts), rebuilt for the
Nepal Stock Exchange and pointed at **NEPSE data** instead of Yahoo Finance.

It can read the **official nepalstock.com.np JSON API** directly
(`data.kind: "nepse"`), or any CSV / HTTP source you already have. No news feed,
no SEC, no congressional filings, no US broker connector. Python standard
library only, Python 3.8+ — no database and no extra packages.

## What NEPSE changed about the model

NEPSE is a delivery market, so this is a swing engine, not a day-trading bot:

| Day Trader skill (US) | Here (NEPSE) |
|---|---|
| 1–5 minute bars | daily bars |
| flat by the close | hold up to `max_hold_days` sessions |
| opening-range breakout | 20-day high breakout on volume |
| `$25k` PDT rules | lot size 10, T+2 delivery, no shorting |
| Alpaca order tickets | paper plan only — order entry stays with your broker's TMS |
| NY hours/holidays | 11:00–15:00 NPT, Mon–Fri, your holiday list |

Consequences baked in: you cannot sell a share bought today (T+2), you cannot
short, a single scrip is banded (15% since April 2026, tighter pre-open), and
every round trip pays broker + SEBON + DP charges. The engine prices those
charges into both the loss and the reward, which is why a 2.5R gross target is
the default: at ~1.2% round-trip cost, 2R gross nets under 1.5R.

## Run it

```bash
cp config.nepse.example.json config.json   # official NEPSE API; edit account/universe
# or: cp config.example.json config.json   # CSV / your own HTTP API
python -m nepse_trader --config config.json scan
python -m nepse_trader --config config.json entry NABIL
python -m nepse_trader --config config.json exit      # = plan; writes stops and targets
python -m nepse_trader --config config.json budget
python -m nepse_trader --config config.json risk
python -m nepse_trader --config config.json timemachine NABIL
python -m nepse_trader --config config.json alerts add --from-plan
python -m nepse_trader --config config.json alerts check
```

State lands in `trader/` (`scan.json`, `plans.json`, `orders.json`,
`timemachine.json`, `alerts.json`) and each command reads what the last wrote.

`data/NABIL.csv` is a **synthetic** sample (seeded random walk, not real NEPSE
prices) so the chain runs offline. With `data.kind: "nepse"` it is ignored.

## Data: the official NEPSE API

`config.nepse.example.json` is ready for `nepalstock.com.np`. The site's JSON
API needs a short-lived token, so the source mints one itself — no credentials,
no login, no API key:

1. `GET /api/authenticate/prove` returns `accessToken` plus `salt1..salt5`.
2. The token has five junk characters inserted. Their positions come from
   `salt2`; `nepse_trader/nepse_api.py` reimplements the site's `css.wasm`
   arithmetic in pure Python (verified against the WASM on 500 random salts).
3. Requests send `Authorization: Salter <token>`; a 401/403 mints a new token.

What you get:

* `GET /api/nots/market/history/security/{id}?size=500&startDate=…&endDate=…`
  — daily `high / low / close / volume`, about **one year** (234 bars for NABIL
  when asked for 3 or 10 years).
* `GET /api/nots/security/{id}` — the live quote used by `alerts check`.

Caching and history:

* Bars are merged into `data/cache/<SYMBOL>.csv`, not overwritten, so your
  local history grows past the API's ~1-year window as you keep using it.
* `cache_ttl_minutes` (default 60) avoids re-fetching within an hour;
  `--refresh` forces a fetch.
* `data/cache/security_map.json` stores symbol → NEPSE id for
  `security_map_ttl_hours` (default 24).

**One caveat: the official history has no open price.** The source fills each
open from the previous session's close. The scanner, ATR and RSI never read
`open`, so setups are unaffected. When the next session has already started,
`plan` uses the live quote's real `openPrice` as the entry; only the provisional
case falls back to the signal close. The Time Machine replays use the synthetic
bar open, so its historical fills are approximate — treat it as a sanity check,
not a tick-level backtest. This is documented in the output instead of hidden.

No database is required. The cache is one CSV per symbol plus a small JSON map.
Add SQLite (or Neon) only if you later want multi-machine sync, intraday bars,
or a queryable trade journal.

### CSV (simplest offline)

One file per symbol, `data/<SYMBOL>.csv`, header `date,open,high,low,close,volume`
(ISO dates). Column names are remappable in `data.columns`, so `Date,Open,High,
Low,Close,Volume` or `published_date` style headers also work. A fetched HTTP
response is cached to `data/cache/<SYMBOL>.csv`; `--refresh` bypasses it.

### Any other JSON HTTP API

```json
"data": {
  "kind": "http",
  "cache_dir": "data/cache",
  "api": {
    "history_url": "https://your-api/daily/{symbol}",
    "quote_url": "https://your-api/quote/{symbol}",
    "headers": { "Authorization": "Bearer ${NEPSE_API_TOKEN}" },
    "records_path": "data",
    "fields": { "date": "d", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "q" },
    "quote_fields": { "last": "lastTradedPrice", "date": "date" },
    "symbol_map": { "NABIL": "NABIL-131" }
  }
}
```

* `{symbol}` in the URLs is replaced by `symbol_map[SYMBOL]` when present,
  otherwise by the symbol itself (`{symbol_lower}` forces lowercase).
* `records_path` is a dotted path to the bar array; empty string means the
  response *is* the array.
* `fields` maps each required field to your JSON key — that is the whole adapter.
* `${ENV_VAR}` works in the URL and headers, so tokens stay out of the file.
* `quote_url` drives `alerts check`; without it the last cached close is used.

Python-keyword mapping is intentionally absent: if your client library returns
objects, dump them to CSV once, or write a 10-line `Source` class with
`bars(symbol)` and `last(symbol)` like `CsvSource` in `nepse_trader/data.py`.

## The commands

**scan** — evaluates the last session of every symbol in `universe` (or
`--symbols`, or every CSV in the data dir) and writes fired setups:

* `breakout` — close above the prior 20-day high on ≥1.2× average volume
* `flag` — tight 3-bar pause (≤1.4 ATR) after a ≥3% run, then a break of the pause
* `bounce` — RSI(2) < 15 followed by an up day, above the 50-day mean

**entry** — `TRIGGERED` / `WAITING` for one symbol, with trigger, stop reference
and today's band.

**exit / plan** — entry is the **actual next-session open** once that session has
started; before then the signal close is used and labelled *provisional*.
Re-run `plan` and `budget` after the open if it gaps. Stop = the tighter of the
setup's range midpoint and 2×ATR; target = `target_r` × risk; `max_hold_days`
session limit.

**budget** — whole board lots, capped by cash and by max loss *including*
charges, stepped down one lot at a time until the net loss fits.

**risk** — PASS/BLOCK per rule: per-trade loss, daily loss, cash, reward:risk
after charges, lot multiple, entry inside the band, long-only.

**timemachine** — two answers before you risk a rupee:
1. *history*: replays the setup on every past bar and counts target-first /
   stop-first / timeout, with expectancy in R.
2. *simulation*: bootstraps the symbol's own sessions into `iterations` replays
   (seeded, so it is reproducible).

A daily bar that touches both stop and target is scored as a **stop** — daily
bars cannot say which printed first, so the odds stay pessimistic rather than
flattering.

**alerts** — `add --from-plan` arms stop, target and a hold-limit alert;
`check` prints only what just hit, once.

## Config knobs worth setting honestly

| key | meaning |
|---|---|
| `account.cash`, `max_loss_per_trade`, `max_loss_per_day` | your real numbers; sizing derives from these |
| `charges.*` | your broker tier (0.36% is the small-trade tier), SEBON 0.015%, DP per trade |
| `market.lot_size` | 10 for the regular board; odd lots differ |
| `plan.target_r`, `atr_mult`, `max_hold_days` | your exit policy |
| `timemachine.iterations`, `seed` | replay volume and reproducibility |
| `universe` | symbols to scan |

## JSON API for a frontend

```bash
python -m nepse_trader.api --config config.json --host 127.0.0.1 --port 8000
```

Standard library only, threaded, with CORS restricted by default to
`http://localhost:3000` plus the origin in the `FRONTEND_CORS_ORIGIN`
environment variable. Configure additional local origins with
`api.cors_origins`. The environment value may include a trailing slash.
Every reply is `application/json`; errors are
`{"error": "...", "status": 404}` and never a traceback.

| endpoint | returns |
|---|---|
| `GET /api/health` | liveness plus the active `data.kind` |
| `GET /api/symbols` | configured universe and cached symbols |
| `GET /api/cache` | the one-year bar cache: symbol, file, count, first/last date, bytes |
| `GET /api/bars/{symbol}` | full daily OHLCV history (`?limit=`, `?refresh=1`) |
| `GET /api/scan?symbols=A,B` | fired setups for those symbols |
| `GET /api/analyze/{symbol}` | **everything**: bars, setups, plan, sizing, risk, timemachine, alerts, verdict |
| `GET /api/report?symbols=A,B` | `analyze` for each symbol (`?bars=1` to include bars) |
| `GET /api/alerts` | stored alerts |
| `POST /api/alerts/check` | evaluates alerts against the latest quote; returns what fired |
| `POST /api/simulations` | saves and runs a custom paper-trade forecast |
| `GET /api/simulations` | lists saved custom simulations |
| `GET /api/simulations/{id}` | returns one saved simulation |

### Custom trade simulation

Use a JSON `POST` to simulate a quantity and holding period without changing
`data/cache`. Each request is saved in its own folder under
`trader/simulations/{id}/result.json` (or the configured `state_dir`). For
example, to model buying 100 NTC shares today and exiting after four sessions:

```bash
curl -X POST http://127.0.0.1:8000/api/simulations \
  -H "Content-Type: application/json" \
  -d '{"symbol":"NTC","action":"buy","shares":100,"days":4}'
```

`entry_price` is optional and defaults to the latest cached close. `action` may
be `buy` or `sell`; sell assumes the shares are already owned because NEPSE is
long-only. The response includes expected, best, worst, and percentile net
profit/loss plus profit/loss probabilities. It bootstraps daily returns from
the cached history, so it is a range forecast rather than a promise about the
price four sessions from now. `iterations` and `seed` can be supplied for a
custom, reproducible forecast.

`/api/analyze` payload:

```json
{
  "symbol": "CREST",
  "generated_at": "2026-11-12T15:17:30Z",
  "as_of": "2026-11-06",
  "verdict": "PASS",
  "data": { "count": 234, "first_date": "2025-11-06", "last_date": "2026-11-06", "source": "nepse", "cache_dir": "data/cache" },
  "setups": [ { "setup": "breakout", "trigger": 1134.0, "setup_low": 1017.0, "volume_ratio": 3.83 } ],
  "plan": { "entry": 1192.0, "stop": 1104.5, "target": 1410.75, "reward_risk": 2.5, "entry_basis": "signal close" },
  "sizing": { "shares": 30, "net_loss_at_stop": 2933.36, "net_reward_risk": 2.12 },
  "risk": { "verdict": "PASS", "checks": [ { "rule": "...", "ok": true, "detail": "..." } ] },
  "timemachine": { "history": { "firings": 14 }, "simulation": { "target_first_pct": 13.3, "stop_first_pct": 49.0 } },
  "alerts": [ { "kind": "stop", "level": 1104.5, "triggered": false } ],
  "bars": [ { "date": "2026-11-06", "open": 1044.9, "high": 1192.0, "low": 1014.0, "close": 1192.0, "volume": 19655 } ]
}
```

`entry` / `stop` / `target` build a **manual plan for any scrip**, setup or
not: `?entry=549&stop=533` (target defaults to `target_r` × risk). The payload
then reports `plan_source: "manual"` instead of `"signal"`, and `verdict`
becomes a real PASS/BLOCK instead of `NO SETUP`. A stop at or above the entry
is rejected with 400, because NEPSE has no shorting.

Query flags: `refresh=1` bypasses the cache, `bars=0` drops the bar array
(6.7 KB instead of 25 KB for a year of CREST), `iterations=2000` caps the Time
Machine replays (max 50000). Required fields are never omitted: a symbol with no
setup returns `plan: null` and `verdict: "NO SETUP"` rather than an error.

NEPSE requests are serialized behind one lock because the official client keeps
one short-lived token, so a burst of parallel requests queues instead of
re-authenticating.

## Serving `data/cache` directly

The cache is plain CSV, one file per symbol (`data/cache/CREST.csv`), plus
`security_map.json` (symbol → NEPSE security id). Anything that can serve a
directory can hand it out:

- `GET /api/cache/all` — **every cached symbol in one JSON**, bars included
  (`?bars=0` for inventory only)
- `GET /api/cache/CREST.csv` — the raw CSV, `text/csv`, CORS enabled
- `python -m http.server 8080 --directory data/cache` — quick downloads, but no
  CORS headers, so a browser `fetch` from another origin will be blocked

nginx (not installed on this machine; this is the config to add):

```nginx
server {
    listen 8080;
    server_name localhost;

    # expose ONLY the cache directory, never the project root
    location /cache/ {
        alias E:/day-trader-skill/data/cache/;   # trailing slash required
        autoindex on;
        autoindex_format json;                    # file list a frontend can read
    # Configure this proxy with the same allowlist as api.cors_origins;
    # do not use a wildcard origin in production.
        add_header Cache-Control "no-store" always;
        types { text/csv csv; application/json json; }
        default_type text/plain;
    }

    # the id map is not market data; keep it off the wire
    location = /cache/security_map.json { deny all; }
}
```

What static serving does **not** give you: files exist only for symbols that
have already been fetched, the newest bar appears only after a command runs
(`scan` or `--refresh`), the `open` column is synthesised from the previous
close, and none of the analysis (plan / sizing / risk / Time Machine) is in the
CSV. Serve the directory for charts and history; use the API for verdicts.

## Neon Postgres state

The engine's state can live in Neon Postgres instead of the JSON files in
`trader/`. Set `NEON_CONN_URL` and the CLI and API write there; with the
variable unset they keep using the files, so offline CSV runs and the test
suite need no database.

```bash
pip install -r requirements.txt          # psycopg, the only dependency
export NEON_CONN_URL='postgresql://...'  # never commit this value
python -m nepse_trader.migrate --config config.json   # schema + import old state
python -m nepse_trader.db --check                     # connectivity + tables
python -m nepse_trader.db --smoke                     # CRUD end to end
```

`migrations/*.sql` are applied once each and tracked in `schema_migrations`.
The imported files are `trader/scan.json`, `plans.json`, `orders.json`,
`timemachine.json` (as `state_snapshots`), `trader/alerts.json` (`alerts`) and
`trader/simulations/*/result.json` (`simulations`). The import is idempotent:
existing rows are never overwritten.

Connection handling: one short-lived psycopg connection per operation with
`prepare_threshold=None`, which suits Neon's pooled endpoint behind a threaded
server. `NEON_CONN_URL` is read from the environment only; the migrate command
may also read `.env` / `.env.local` in the project root.

## Suggestion runs (BUY / SELL / HOLD)

`nepse_trader/suggestions.py` scores daily bars (SMA/EMA trend, MACD, RSI
extremes, volatility-adjusted momentum), replays the same score over each
symbol's past window with the Time Machine and scales confidence by the
realised win rate, expectancy and drawdown, then sizes every BUY from the paper
portfolio (risk per trade, concentration cap, whole board lots) and caps every
SELL at the shares held. One bad symbol never fails the batch.

Runs, suggestions, the watchlist and the notification log live in Neon, so the
endpoint needs `NEON_CONN_URL`. The cron endpoint also needs `CRON_SECRET`:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/suggestions \
  -H "Authorization: Bearer $CRON_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"symbols": ["NABIL", "NICA"], "notify": true}'

curl -sS http://127.0.0.1:8000/api/suggestions/latest
curl -sS http://127.0.0.1:8000/api/watchlist
curl -sS -X POST http://127.0.0.1:8000/api/watchlist \
  -H "Content-Type: application/json" -d '{"symbols": ["NABIL", "NTC"]}'
curl -sS -X DELETE http://127.0.0.1:8000/api/watchlist/NTC
```

* `symbols` is optional; without it (or with `[]`) the saved watchlist is used.
* Send `Idempotency-Key: <unique>` on a cron retry: the same key returns the
existing run (`200`) instead of generating and notifying again.
* `notify: true` posts the run to Discord (see below); the response always
carries `run_id`, the suggestions, their `market` / `portfolio` context, and a
`notification` summary (`sent`, `skipped` or `error`).
* Only `POST /api/suggestions` is behind `CRON_SECRET`; `GET
/api/suggestions/latest` and the watchlist routes are read/write public JSON
like the rest of the API.
* 401 for a missing/wrong secret, 400 for bad input, 503 when the server has no
`CRON_SECRET` or no Neon connection, 201 for a new run, 200 for an idempotent
replay.

Generate the secret with `python -c "import secrets;print(secrets.token_urlsafe(32))"`
and put it in the server's environment and the cron job's secret store.

Engine knobs (config `suggest`, overridable by env): `risk_per_trade_pct`
(`SUGGEST_RISK_PER_TRADE_PCT`), `max_position_pct`, `max_positions`,
`min_confidence` (`SUGGEST_MIN_CONFIDENCE`), `lookback_days`
(`SUGGEST_LOOKBACK_DAYS`), `buy_threshold`, `sell_threshold`, `atr_mult`,
`target_r`, `sell_fraction`, `min_history_trades`, `concurrency`
(`SUGGEST_CONCURRENCY`). Market data is fetched one symbol at a time because
the official NEPSE client shares one short-lived token; the analysis runs in a
bounded thread pool.

### Cron

crontab (server running the API, 15:15 NPT on weekdays):

```cron
15 15 * * 1-5 curl -fsS -X POST http://127.0.0.1:8000/api/suggestions -H "Authorization: Bearer $CRON_SECRET" -H "Content-Type: application/json" -H "Idempotency-Key: $(date +%F)" -d '{}' >/dev/null
```

GitHub Actions:

```yaml
on:
  schedule: [{cron: "15 9 * * 1-5"}]   # 09:15 UTC = 15:00 NPT
jobs:
  suggestions:
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -fsS -X POST "$API_URL/api/suggestions" \
            -H "Authorization: Bearer ${{ secrets.CRON_SECRET }}" \
            -H "Content-Type: application/json" \
            -H "Idempotency-Key: ${{ github.run_id }}" \
            -d '{"notify": true}'
        env:
          API_URL: ${{ secrets.API_URL }}
```

Vercel cron (only if this API is deployed to Vercel; Vercel sends
`Authorization: Bearer $CRON_SECRET` for you):

```json
{ "crons": [{ "path": "/api/suggestions", "schedule": "15 9 * * 1-5" }] }
```

### Discord notifications

Set `DISCORD_WEBHOOK_URL` in the API server's environment (never in a file that
gets committed) and call the endpoint with `"notify": true`. The message is one
summary embed (run time, symbol count, BUY/SELL/HOLD counts) followed by one
embed per suggestion with price, quantity, confidence and a short reason —
green BUY, red SELL, grey HOLD — and a paper-trading/"not financial advice"
footer.

Create the webhook in Discord: **Server Settings → Integrations → Webhooks →
New Webhook**, pick the channel, **Copy Webhook URL**. Treat the URL as a
password: anyone holding it can post to that channel.

Delivery rules:

* At most 10 embeds and 6000 characters of embed content per message; longer
  runs are chunked into `1/3`, `2/3`, … messages (2000 characters of content,
  4096 per embed description).
* HTTP 429 uses Discord's `retry_after`; 5xx and network errors back off
  exponentially, up to 4 attempts per message.
* Every attempt is recorded in `notification_log` (`sent`, `skipped`, `error`).
  The API response is unaffected by a Discord failure — the run still returns
  `201` with the suggestions, and `notification.status` says what happened.
* A run that already has a `sent` row for the channel is never sent again, so a
  retried cron call cannot double-post.

Test it without the API (it prints the payloads and sends nothing):

```bash
python -m nepse_trader.discord --dry-run
```

Send a sample message with the real webhook:

```bash
python -m nepse_trader.discord --test
```

The sample includes a BUY, a SELL and a HOLD so all three colours and the
footer show up. `--test` loads `.env` / `.env.local` if the variable is not
already in the environment.

## Tests

```bash
python -m unittest discover -s tests -v
```

Twenty-six offline tests: the engine's original twelve (CSV round-trip, API
field mapping and `symbol_map`, env expansion, breakout detection,
lot/limit/charge-aware sizing, BLOCK on a tiny account, deterministic simulation,
history accounting, alert firing once, the full CLI chain) plus fourteen for the
official source: token offsets against WASM samples, token parsing, OHLCV
mapping, synthetic opens, cache TTL, cache merging, stale-cache fallback,
future-date filtering, quote fallback, live session open, next-open plan entry,
entry overrides, unknown symbols, and `kind: "nepse"` recognition. None of them
touch the network.

## Not financial advice

Software that reads your data and does arithmetic. Most people who trade lose
money. The Time Machine is a simulation, not a prediction; a stop is a plan, not
a guaranteed fill, and NEPSE's bands and halts can gap straight through it.
