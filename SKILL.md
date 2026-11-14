---
name: nepse-trader
description: Scan NEPSE daily charts for setups, set entry/stop/target, size positions in board lots against account limits, replay outcomes with a seeded Time Machine, and manage stop/target/hold alerts — using the official nepalstock.com.np JSON API or the user's own CSV/API, never US market data.
---

# NEPSE Trader

NEPSE-adapted trading engine. Daily bars only, because NEPSE is a T+2 delivery
market: a share bought today cannot be sold for two sessions, short selling is
not allowed, and a single scrip is banded at 15% per session.

Never fetch Yahoo Finance, US symbols, forex, crypto or global news for this
skill. All data comes from the **official NEPSE API** (`data.kind: "nepse"`),
the user's CSV files, or the user's own API.

## Run the chain

Work from the project root. Use the user's config (default `config.json`).

```bash
python -m nepse_trader --config config.json scan
python -m nepse_trader --config config.json entry <SYMBOL>
python -m nepse_trader --config config.json exit
python -m nepse_trader --config config.json budget
python -m nepse_trader --config config.json risk
python -m nepse_trader --config config.json timemachine <SYMBOL>
python -m nepse_trader --config config.json alerts add --from-plan
python -m nepse_trader --config config.json alerts check
```

Each command reads the previous one's JSON from `trader/`. If a file is
missing, run the earlier command instead of inventing numbers.

## Rules

1. Report the scanner output as-is; do not add candidates it did not find.
2. Show the plan's entry basis, stop, target, reward:risk **after charges** and
   the session hold limit. Entry is the next session's open: if the plan says
   *provisional*, report the signal close as an estimate and re-run `plan` and
   `budget` after the open instead of pretending the close was the fill. No
   intraday framing: this is a multi-session delivery trade.
3. A `BLOCK` from `risk` is final. Do not resize to force a pass, and never
   quietly raise `max_loss_per_trade`.
4. The Time Machine is a simulation. Quote its target-first / stop-first /
   timeout split and expectancy in R, and keep the caveat that daily bars
   cannot resolve intrabar order.
5. Paper trading only. This skill cannot place a NEPSE order; order entry stays
   with the user's broker TMS.

## Data

Preferred: official NEPSE (`data.kind: "nepse"`, template in
`config.nepse.example.json`). No credentials or env vars are needed — the source
mints its own short-lived token via `/api/authenticate/prove`. Bars are cached
and merged in `data/cache/<SYMBOL>.csv`; `cache_ttl_minutes` defaults to 60 and
`--refresh` forces a fetch. `data/cache/security_map.json` holds symbol → id.

The official API returns about one year of daily bars and **no open price**;
bars fill open from the previous close. The scanner/ATR/RSI do not use open, so
setups are unaffected. `plan` uses the live quote's real `openPrice` as the
entry once the next session has started; otherwise it labels the signal close
*provisional*. The Time Machine's historical next-open fills still use the
synthetic bar open, so they are approximate — say so when quoting them. Never
claim they are tick-accurate.

No database is needed. Do not ask for Neon/SQLite unless the user wants
multi-machine sync or a queryable journal; the CSV cache is enough.

Alternatives: `data/<SYMBOL>.csv` with
`date,open,high,low,close,volume`, or `data.kind: "http"` with
`data.api.history_url` and `data.api.fields` mapped to the user's API keys. If
the API shape is unknown, ask for one sample response rather than guessing field
names. If the user's API has no daily OHLC (snapshot only), say so plainly: the
scanner and Time Machine need a bar history.

If a live fetch fails (network, NEPSE maintenance, expired mapping), fall back
to the cached CSVs and report the data's last date instead of inventing bars.
