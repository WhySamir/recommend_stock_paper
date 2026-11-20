# Future Simulations — design

Date: 2026-11-20
Status: proposed

## Intent

Let a user log a paper trade today ("buy 10 NABIL at 574") with one or more
holding horizons ("after 4 days", "after 10 days"), then come back later and see
what actually happened on each due date, net of charges. The result belongs to
the due date, not to the day the user happens to check.

## Goals

- New `POST /api/future-simulations` — save a trade with `days` as an integer
  or a list of integers. One record can hold several horizons.
- New `GET /api/future-simulations/{id}` — return the record; settle each
  horizon against the close on its own due date and freeze that result.
- New `GET /api/future-simulations` — list saved records.
- Weekends and dates in `config.json` `holidays` push a due date forward.
- Charges: broker 0.36% + SEBON 0.015% + DP charge from `config.json`.
- Include a forecast at creation time so "what might happen after 4 days" is
  answered immediately, while the later settlement remains the real check.

## Non-goals

- No broker orders, no live trading, no intraday data, no automatic cron job.
- No change to the existing `/api/simulations` contract.
- No recomputation of a settled horizon when newer prices arrive.

## Anti-goals

- Showing the latest price as if it were the 4-day result.
- Silently changing a settled result because a historical bar was revised.

## Constraints

- Standard library only; Python 3.8+ syntax.
- Same file/Neon persistence split as the rest of the API: JSON under
  `state_dir` by default, Neon when `NEON_CONN_URL` is set.
- Nepal time is UTC+5:45, no DST, matching `suggestions.market_state`.
- Market data comes from the configured source; no new data provider.

## Approaches

1. **Future-simulation ledger (chosen).** A saved record with several
   checkpoints, each settled once against its due-date close. Matches the
   "log today, check after 4/10 days" workflow and keeps one trade together.
2. **Forecast-only.** Reuse `simulation.run()` and show expected/best/worst
   P&L now, with no later comparison. Cheaper, but misses the main request.
3. **Extend `/api/simulations`.** Adds horizon settlement to the current
   endpoint. Reuses code but changes an existing contract and mixes two jobs.
   Rejected because the user asked for a separate endpoint.

## Decisions

### Request body

```json
{
  "symbol": "NABIL",
  "action": "buy",
  "shares": 10,
  "price": 574,
  "days": [4, 10],
  "iterations": 1000,
  "seed": 19,
  "refresh": false
}
```

- `symbol` required; same symbol validation as `/api/simulations`.
- `action` is `buy` or `sell`, default `buy`.
- `price` required and positive. `entry_price` and `sell_price` are accepted
  as aliases and stored as `price`, so both wordings in the request make sense.
- `days` may be an integer or an array of integers, default `[4]`. Each value
  1–252; deduplicate, sort, max 10 horizons per record.
- `iterations` optional, default 1000, max 50000; `seed` optional, defaulting
  to config `timemachine.seed`.
- For `sell`, `price` means "sold today at this price"; the due-date close is
  the buyback cost. Shares are assumed already held (long-only).

### Due dates

Start date is today in NPT. For each `n` in `days`, the naive due date is
`start + n calendar days` (so `2026-11-20 + 4 = 2026-11-24`). While that date
is Saturday, Sunday, or listed in `config.json` `holidays`, add one day. The
result is stored on the record at creation and never recomputed.

### Settlement

On `GET /api/future-simulations/{id}?refresh=1`, for each unsettled horizon:

- If the due date is in the future: `status: "pending"`, reason
  `"scheduled"`.
- Otherwise ask the configured source for bars. If no bar has `date ==
  due_date`: stay `pending`, report the last cached bar date. A holiday added
  after creation is handled by the date check; a missing bar is not invented.
- If the bar exists: `exit_price = bar["close"]`; compute gross P&L, both-leg
  fees via `simulation._fees`, and net P&L; set `result` to `profit`, `loss`,
  or `break_even`; write `exit_date`, `settled_at`, and `status: "settled"`
  into the record and persist it.
- Settled horizons are returned unchanged on every later GET. The record's
  overall status is `pending`, `partial`, or `settled`.

### Responses

`POST` returns `201` with the full record: `id`, `symbol`, `action`, `shares`,
`price`, `created_date`, `status`, `horizons[]` (each with `days`, `due_date`,
`status`, and a slim `forecast`: `expected_net_pnl`, `best_net_pnl`,
`worst_net_pnl`, `profit_probability_pct`), `generated_at`, and `caveat`.

`GET /api/future-simulations/{id}` returns the same shape with settled fields
filled in. `GET /api/future-simulations` returns `{"simulations": [...]}` with
id, symbol, action, shares, price, created_date, status, and generated_at.
Errors stay JSON: 400 for bad input, 404 for unknown symbol/id, 500 otherwise.

### Storage and files

- `nepse_trader/future_simulations.py` — date math, request/response shaping,
  settlement calculation; imports `simulation._fees` and `simulation.run`.
- `nepse_trader/store.py` — `save_future_simulation`, `get_future_simulation`,
  `list_future_simulations`, mirroring the existing simulation helpers.
- `nepse_trader/api.py` — three routes plus endpoint listing and POST routing.
- `migrations/0005_future_simulations.sql` — `future_simulations` table with
  `id text primary key`, `symbol`, `action`, `shares`, `price`, `created_date`,
  `payload jsonb`, `created_at`.
- Files backend: `trader/future_simulations/{id}.json`.
- Tests in `tests/test_future_simulations.py` (date rolling, fee math, freeze
  behaviour, multiple horizons) and new cases in `tests/test_api.py`.
- README section with curl examples and the 11/20 → 11/24 → 11/30 dates.

## Left to builder

- Exact validation messages, rounding, and handler plumbing.
- Whether to expose a DELETE route (not in scope; omit for now).
- Run the existing unittest suite before declaring done.
