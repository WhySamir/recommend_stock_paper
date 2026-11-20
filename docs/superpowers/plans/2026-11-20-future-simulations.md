# Future Simulations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/api/future-simulations` ledger that logs a paper trade with several holding horizons and settles each one once against its own due-date close.

**Architecture:** A new `future_simulations` module owns date math, fee-aware outcome math, record creation, and settlement. `api.py` handles validation, persistence (JSON files by default, Neon via `store`), and three routes. Reuse `simulation._fees` and `simulation.run` rather than duplicating charge or forecast logic.

**Tech Stack:** Python 3.8+ standard library, existing `unittest` suite, psycopg only for the optional Neon path.

**Spec:** `docs/superpowers/specs/2026-11-20-future-simulations-design.md`

## Global Constraints

- Standard library only; no new dependencies.
- Timestamps use UTC and `Z` suffix; trading dates use fixed UTC+5:45 (`NPT`).
- Charges come from `cfg["charges"]`; broker is 0.36%, SEBON 0.015%, DP charge 25 in the shipped config.
- `days` accepts an integer or a list; default `[4]`, each value 1–252, max 10 horizons, deduplicated and sorted.
- File state lives under `state_dir/future_simulations/`; Neon state uses a `future_simulations` table.
- Never invent a missing bar; a horizon without its due-date bar stays `pending`.
- Never recompute a settled horizon.

## Review Focus

- A due date landing on Saturday/Sunday or a configured holiday must roll forward — pinned by `test_due_date_rolls_weekends_and_holidays` (Task 1).
- Returning long after the due date must still show the original frozen result — pinned by `test_settle_freezes_result` (Task 2).
- `sell` must invert the price direction while charging fees on both legs — pinned by `test_outcome_sell_charges_both_legs` (Task 1).
- A missing due-date bar must remain pending with the last cached date, never a guessed price — pinned by `test_settle_pending_without_due_bar` (Task 2).
- Horizons `[4, 10]` settle independently, so one settled and one pending yields overall `partial` — pinned by `test_settle_partial_multiple_horizons` (Task 2).

---

### Task 1: Date and outcome primitives

**Files:**
- Create: `nepse_trader/future_simulations.py`
- Test: `tests/test_future_simulations.py`

**Interfaces:**
- Consumes: `simulation._fees(cfg, turnover) -> float`.
- Produces: `NPT`, `today_npt(now=None) -> date`, `normalise_days(raw) -> list[int]`, `due_date(start: date, days: int, holidays) -> date`, `outcome(action: str, shares: int, price: float, exit_price: float, cfg: dict) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
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
        self.assertEqual(fs.due_date(date(2026, 11, 20), 4, {"2026-11-24"}), date(2026, 11, 25))
        self.assertEqual(fs.due_date(date(2026, 11, 21), 1, set()), date(2026, 11, 23))

    def test_outcome_buy_and_sell_charge_both_legs(self):
        cfg = config.DEFAULTS
        buy = fs.outcome("buy", 10, 574.0, 600.0, cfg)
        sell = fs.outcome("sell", 10, 600.0, 574.0, cfg)
        self.assertAlmostEqual(buy["fees"], 94.03, places=1)
        self.assertAlmostEqual(sell["fees"], 94.03, places=1)
        self.assertEqual((buy["gross_pnl"], sell["gross_pnl"]), (260.0, 260.0))
        self.assertEqual((buy["result"], sell["result"]), ("profit", "profit"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_future_simulations.DateAndOutcomeTests -v`
Expected: FAIL with `ModuleNotFoundError: nepse_trader.future_simulations`.

- [ ] **Step 3: Implement the primitives in `nepse_trader/future_simulations.py`**

Define `NPT = timezone(timedelta(hours=5, minutes=45))`. `today_npt(now)` converts an aware UTC datetime to NPT; with `now=None` it uses `datetime.now(NPT)`. `normalise_days` raises `ValueError` for non-ints, values outside 1–252, an empty result, or more than 10 values. `due_date` adds `timedelta(days=days)` to `start`, then advances one day at a time while `date.weekday() >= 5` or the ISO date is in `holidays`. `outcome` computes `gross = shares * (exit_price - price)` for `buy` and its negation for `sell`, `fees = _fees(cfg, shares * price) + _fees(cfg, shares * exit_price)`, `net = gross - fees`, rounds all three to 2 decimals, and labels `profit` / `loss` / `break_even` from `net`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_future_simulations.DateAndOutcomeTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add nepse_trader/future_simulations.py tests/test_future_simulations.py
git commit -m "feat: add future simulation date and outcome helpers"
```

### Task 2: Record creation and frozen settlement

**Files:**
- Modify: `nepse_trader/future_simulations.py`
- Test: `tests/test_future_simulations.py`

**Interfaces:**
- Consumes: `today_npt`, `normalise_days`, `due_date`, `outcome` (Task 1), `simulation.run(symbol, bars, action, shares, days, entry_price, iterations, seed, cfg) -> dict`.
- Produces: `new_record(symbol, action, shares, price, days, cfg, bars, iterations=1000, seed=None, now=None) -> dict`, `settle(record: dict, bars: list[dict], now=None) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
class RecordTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(config.DEFAULTS))
        self.cfg["timemachine"]["iterations"] = 100
        self.bars = [
            {"date": "2026-11-20", "close": 574.0, "high": 575.0, "low": 573.0, "open": 574.0, "volume": 1},
            {"date": "2026-11-24", "close": 600.0, "high": 601.0, "low": 599.0, "open": 600.0, "volume": 1},
            {"date": "2026-11-30", "close": 620.0, "high": 621.0, "low": 619.0, "open": 620.0, "volume": 1},
        ]
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    def test_new_record_has_due_dates_and_slim_forecast(self):
        record = fs.new_record("NABIL", "buy", 10, 574.0, [10, 4], self.cfg, self.bars,
                               iterations=50, seed=7, now=self.now)
        self.assertEqual([h["days"] for h in record["horizons"]], [4, 10])
        self.assertEqual([h["due_date"] for h in record["horizons"]], ["2026-11-24", "2026-11-30"])
        self.assertEqual(record["status"], "pending")
        self.assertIn("expected_net_pnl", record["horizons"][0]["forecast"])
        self.assertNotIn("sample_outcomes", record["horizons"][0]["forecast"])

    def test_settle_uses_due_bar_and_freezes_result(self):
        record = fs.new_record("NABIL", "buy", 10, 574.0, 4, self.cfg, self.bars, now=self.now)
        later = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)
        settled = fs.settle(record, self.bars, now=later)
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(settled["horizons"][0]["exit_price"], 600.0)
        self.assertEqual(settled["horizons"][0]["result"], "profit")
        changed = [dict(bar, close=999.0) for bar in self.bars]
        again = fs.settle(settled, changed, now=later)
        self.assertEqual(again["horizons"][0]["net_pnl"], settled["horizons"][0]["net_pnl"])

    def test_settle_pending_without_due_bar(self):
        record = fs.new_record("NABIL", "buy", 10, 574.0, 4, self.cfg, self.bars[:1], now=self.now)
        settled = fs.settle(record, self.bars[:1], now=datetime(2026, 11, 25, tzinfo=timezone.utc))
        self.assertEqual(settled["status"], "pending")
        self.assertEqual(settled["horizons"][0]["reason"], "due-date bar not available")
        self.assertEqual(settled["horizons"][0]["last_bar_date"], "2026-11-20")

    def test_settle_partial_multiple_horizons(self):
        record = fs.new_record("NABIL", "sell", 10, 600.0, [4, 10], self.cfg, self.bars, now=self.now)
        settled = fs.settle(record, self.bars[:2], now=datetime(2026, 11, 26, tzinfo=timezone.utc))
        self.assertEqual(settled["status"], "partial")
        self.assertEqual([h["status"] for h in settled["horizons"]], ["settled", "pending"])
        self.assertEqual(settled["horizons"][0]["result"], "loss")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_future_simulations.RecordTests -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'new_record'`.

- [ ] **Step 3: Implement `new_record` and `settle` in `nepse_trader/future_simulations.py`**

`new_record` normalises `days`, resolves `seed` from `cfg["timemachine"]["seed"]`, calls `simulation.run` once per horizon, keeps only `expected_net_pnl`, `best_net_pnl`, `worst_net_pnl`, and `profit_probability_pct` from its `forecast`, and returns `symbol`, `action`, `shares`, `price`, `created_date`, `horizons`, `status`, `generated_at`, and a caveat string. `settle` iterates horizons, skips any with `status == "settled"`, marks future due dates `pending` with reason `"scheduled"`, finds a bar whose `date` equals the horizon's `due_date`, and otherwise calls `outcome` and writes the settled fields. Set the record's `status` to `settled` when all horizons are settled, `partial` when at least one is, else `pending`. Mutate and return the same record object.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_future_simulations -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add nepse_trader/future_simulations.py tests/test_future_simulations.py
git commit -m "feat: settle future simulations against due-date closes"
```

### Task 3: Neon persistence and migration

**Files:**
- Modify: `nepse_trader/store.py:83-131`
- Create: `migrations/0005_future_simulations.sql`

**Interfaces:**
- Consumes: `db.execute`, `db.fetch_all`, `db.jsonb`.
- Produces: `save_future_simulation(payload) -> payload`, `get_future_simulation(simulation_id) -> dict | None`, `list_future_simulations() -> list[dict]`.

- [ ] **Step 1: Write the migration**

```sql
create table if not exists future_simulations (
    id text primary key,
    symbol text not null,
    action text not null,
    shares integer not null,
    price double precision not null,
    created_date text not null,
    payload jsonb not null,
    created_at timestamptz not null default now()
);
create index if not exists future_simulations_created_at_idx
    on future_simulations (created_at desc);
```

- [ ] **Step 2: Add the three store functions**

Mirror `save_simulation` / `get_simulation` / `list_simulations`, using `on conflict (id) do update set payload = excluded.payload` and selecting `payload` by id. `list_future_simulations` returns rows of `id`, `symbol`, `action`, `shares`, `price`, `created_date`, `status`, and `generated_at` from the stored payload, ordered by `created_at desc`.

- [ ] **Step 3: Verify offline tests stay green and the SQL parses**

Run: `python -m unittest tests.test_offline tests.test_api -q`
Expected: PASS, unchanged; no Neon connection needed.

- [ ] **Step 4: Commit**

```bash
git add nepse_trader/store.py migrations/0005_future_simulations.sql
git commit -m "feat: persist future simulations in Neon"
```

### Task 4: API routes

**Files:**
- Modify: `nepse_trader/api.py:288-365`, `nepse_trader/api.py:490-512`, `nepse_trader/api.py:539-562`, `nepse_trader/api.py:581-612`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `future_simulations.new_record`, `future_simulations.settle`, `store.backend/save_future_simulation/get_future_simulation/list_future_simulations`.
- Produces: `Api.create_future_simulation(request) -> dict`, `Api.get_future_simulation(id, refresh=False) -> dict`, `Api.future_simulations() -> dict`.

- [ ] **Step 1: Write the integration tests in `tests/test_api.py`**

Add a `FutureSimulationApiTests` class with its own `TemporaryDirectory`, CSV bars dated `2026-11-20` through `2026-11-30`, `cfg["data"]["kind"] = "csv"`, and `cfg["state_dir"]` pointing at the temp dir. Patch `nepse_trader.future_simulations.today_npt` with `side_effect=lambda *a, **k: cls.today` and set `cls.today = date(2026, 11, 20)` before the POST. Set `cls.today = date(2026, 11, 30)` before the GET so the due dates are reachable. Assert:

```python
status, body, _ = self.request("/api/future-simulations", method="POST", body={
    "symbol": "NABIL", "action": "buy", "shares": 10,
    "entry_price": 574, "days": [4, 10], "iterations": 50, "seed": 7,
})
self.assertEqual(status, 201)
self.assertEqual([h["due_date"] for h in body["horizons"]], ["2026-11-24", "2026-11-30"])
_path = Path(body["simulation_dir"], "result.json")
self.assertTrue(_path.is_file())

self.__class__.today = date(2026, 11, 30)
status, settled, _ = self.request("/api/future-simulations/" + body["id"])
self.assertEqual(status, 200)
self.assertEqual([h["status"] for h in settled["horizons"]], ["settled", "settled"])
self.assertEqual(settled["status"], "settled")

status, listing, _ = self.request("/api/future-simulations")
self.assertEqual(status, 200)
self.assertTrue(any(row["id"] == body["id"] for row in listing["simulations"]))

status, error, _ = self.request("/api/future-simulations", method="POST",
                                body={"symbol": "NABIL", "shares": 1, "price": 10, "days": [0]})
self.assertEqual(status, 400)
self.assertIn("error", error)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_api.FutureSimulationApiTests -v`
Expected: FAIL with 404 for `/api/future-simulations`.

- [ ] **Step 3: Implement the API methods and routes**

In `Api`, add `_future_dir()` creating `state_dir/future_simulations`. `create_future_simulation` validates symbol with the existing regex, `action` in `buy`/`sell`, `shares` 1–10,000,000, `price` from `price`/`entry_price`/`sell_price` as a positive float, `days` through `future_simulations.normalise_days`, `iterations` 1–50000, and integer `seed`. Fetch bars under `self.lock` with `refresh=bool(request.get("refresh"))`, call `new_record`, attach `id`, `cache_dir`, `simulation_dir`, and `generated_at` already produced by `new_record`, then persist with `store.save_future_simulation` on Postgres or write `{id}.json` on files. `get_future_simulation` validates the id with the existing regex, loads the record, calls `settle` with `refresh` from the query, persists a changed record, and returns it. `future_simulations` lists summaries from either backend. Add the three routes to `do_POST` / `do_GET` and the three entries to the endpoint index.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_api -v`
Expected: PASS (all API tests including the new class).

- [ ] **Step 5: Commit**

```bash
git add nepse_trader/api.py tests/test_api.py
git commit -m "feat: add future simulations API endpoints"
```

### Task 5: Documentation and full verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the three routes from Task 4.

- [ ] **Step 1: Document the endpoint**

Add a README subsection after the custom trade simulation section with a curl `POST` using `{"symbol":"NABIL","action":"buy","shares":10,"entry_price":574,"days":[4,10]}` and a `GET /api/future-simulations/{id}` example. State that `11/20 + 4` settles on `11/24`, that results are frozen once settled, and that fees are broker 0.36% + SEBON 0.015% + DP charge.

- [ ] **Step 2: Run the full offline suite and syntax check**

Run: `python -m unittest discover -s tests -q` and `python -m compileall -q nepse_trader`
Expected: PASS; no Neon required.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document future simulations API"
```
