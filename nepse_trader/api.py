"""Read-only JSON API over the engine, for a frontend repo.

    python -m nepse_trader.api --config config.json --port 8000

Everything the CLI computes is returned as JSON: the year of cached daily bars,
fired setups, the plan, sizing, the risk verdict and the Time Machine odds.
NEPSE data access is serialized behind one lock because the official client
keeps a shared token.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import threading
import urllib.parse
import uuid
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import alerts as alerts_mod
from . import cli as cli_mod
from . import config as config_mod
from . import data as data_mod
from . import indicators as ind
from . import db as db_mod
from . import plan as plan_mod
from . import runs as runs_mod
from . import scan as scan_mod
from . import future_simulations as future_mod
from . import simulation as simulation_mod
from . import store
from . import timemachine as tm_mod

MAX_ITERATIONS = 50000


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _int(query, name):
    raw = (query.get(name) or [""])[0].strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ApiError("%s must be an integer" % name, 400)


def _float(query, name):
    raw = (query.get(name) or [""])[0].strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ApiError("%s must be a number" % name, 400)


def _flag(query, name, default=False):
    raw = (query.get(name) or [""])[0].strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.source = data_mod.open_source(cfg)
        self.lock = threading.Lock()

    # -- helpers ------------------------------------------------------
    def symbols(self, query):
        raw = (query.get("symbols") or [""])[0]
        if raw.strip():
            names = [part.strip().upper() for part in raw.split(",") if part.strip()]
            if names:
                return names
        names = data_mod.universe(self.cfg, self.source)
        if not names:
            raise ApiError("no symbols: pass ?symbols=NABIL,CREST or set universe", 400)
        return names

    def _config_for(self, iterations):
        cfg = self.cfg
        if iterations is None:
            return cfg
        if not (1 <= iterations <= MAX_ITERATIONS):
            raise ApiError("iterations must be between 1 and %d" % MAX_ITERATIONS, 400)
        cfg = json.loads(json.dumps(cfg))
        cfg["timemachine"]["iterations"] = iterations
        return cfg

    def _plan_for(self, candidates):
        """One position per symbol: strongest-volume setup sets the plan."""
        if not candidates:
            return None
        ordered = sorted(candidates, key=lambda c: c.get("volume_ratio", 0), reverse=True)
        candidate = dict(ordered[0])
        setups = []
        for item in ordered:
            name = item.get("setup")
            if name and name not in setups:
                setups.append(name)
        candidate["setups"] = setups
        actual = cli_mod._entry_for_plan(self.source, candidate)
        if actual:
            candidate["entry"], candidate["entry_basis"] = actual
        trade = plan_mod.build_plan(candidate, self.cfg)
        if trade:
            trade["setups"] = setups
        return trade

    def _manual_plan(self, symbol, bars, entry, stop, target=None):
        """Plan from levels the caller supplies, for any scrip, setup or not."""
        cfg = self.cfg
        if entry is None or stop is None:
            raise ApiError("entry and stop must be given together", 400)
        if entry <= 0 or stop <= 0:
            raise ApiError("entry and stop must be positive", 400)
        if stop >= entry:
            raise ApiError("stop must be below entry (NEPSE is long only)", 400)
        risk = entry - stop
        if target is None:
            target = entry + cfg["plan"]["target_r"] * risk
        if target <= entry:
            raise ApiError("target must be above entry", 400)
        previous_close = bars[-2]["close"] if len(bars) > 1 else entry
        band = cfg["market"]["price_band_pct"] / 100.0
        return {
            "symbol": symbol.upper(),
            "setup": "manual",
            "signal_date": bars[-1]["date"],
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "risk_per_share": round(risk, 2),
            "reward_per_share": round(target - entry, 2),
            "reward_risk": round((target - entry) / risk, 2),
            "stop_basis": "manual level",
            "setup_low": round(stop, 2),
            "atr": round(ind.atr_last(bars, 14) or 0.0, 2),
            "band_upper": round(previous_close * (1 + band), 2),
            "max_hold_days": cfg["plan"]["max_hold_days"],
            "entry_basis": "manual level",
            "note": "",
            "setups": ["manual"],
        }

    def _bar_summary(self, bars):
        return {
            "count": len(bars),
            "first_date": bars[0]["date"],
            "last_date": bars[-1]["date"],
            "last_close": bars[-1]["close"],
            "last_volume": bars[-1]["volume"],
            "source": (self.cfg["data"].get("kind") or "csv"),
            "cache_dir": self.cfg["data"].get("cache_dir") or "",
        }

    # -- analysis -----------------------------------------------------
    def analyze(self, symbol, include_bars=True, refresh=False, iterations=None, levels=None):
        cfg = self._config_for(iterations)
        with self.lock:
            bars = self.source.bars(symbol, refresh=refresh)
            candidates = scan_mod.evaluate(symbol, bars, cfg)
            if levels:
                trade = self._manual_plan(symbol, bars, **levels)
                plan_source = "manual"
            else:
                trade = self._plan_for(candidates)
                plan_source = "signal"
            sizing = plan_mod.size_position(trade, cfg) if trade else None
            risk = plan_mod.risk_check(trade, sizing, cfg) if trade and sizing else None
            machine = tm_mod.run(symbol, bars, trade, cfg) if trade else None
            payload = {
                "symbol": symbol.upper(),
                "generated_at": _now(),
                "as_of": bars[-1]["date"],
                "data": self._bar_summary(bars),
                "setups": candidates,
                "plan": trade,
                "plan_source": plan_source if trade else "none",
                "sizing": sizing,
                "risk": risk,
                "timemachine": machine,
                "alerts": [
                    a
                    for a in alerts_mod.load(alerts_mod.path_for(cfg))
                    if a.get("symbol") == symbol.upper()
                ],
                "verdict": (risk or {}).get("verdict") or "NO SETUP",
                "disclaimer": (
                    "Software that reads market data and does arithmetic. Not "
                    "financial advice; paper analysis only."
                ),
            }
            if include_bars:
                payload["bars"] = bars
            return payload

    def scan(self, symbols, refresh=False):
        candidates, errors = [], []
        with self.lock:
            for symbol in symbols:
                try:
                    bars = self.source.bars(symbol, refresh=refresh)
                    candidates.extend(scan_mod.evaluate(symbol, bars, self.cfg))
                except data_mod.DataError as exc:
                    errors.append({"symbol": symbol, "error": str(exc)})
        candidates.sort(key=lambda c: (-c.get("volume_ratio", 0), c["symbol"]))
        return {
            "generated_at": _now(),
            "scanned": len(symbols),
            "as_of": candidates[0]["date"] if candidates else "",
            "candidates": candidates,
            "errors": errors,
        }

    def report(self, symbols, include_bars=False, refresh=False, iterations=None):
        results, errors = [], []
        for symbol in symbols:
            try:
                results.append(self.analyze(symbol, include_bars, refresh, iterations))
            except data_mod.DataError as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
        return {"generated_at": _now(), "results": results, "errors": errors}

    def cache_all(self, include_bars=True):
        """Every cached symbol in one payload, optionally with its bars."""
        inventory = self.cache_inventory()
        entries = []
        for row in inventory["symbols"]:
            try:
                bars = data_mod.read_csv(row["file"], symbol=row["symbol"])
            except data_mod.DataError:
                continue
            item = dict(row)
            if include_bars:
                item["bars"] = bars
            entries.append(item)
        return {
            "generated_at": _now(),
            "cache_dir": inventory["cache_dir"],
            "symbols_cached": len(entries),
            "symbols": entries,
        }

    def cache_inventory(self):
        directory = Path(self.cfg["data"].get("cache_dir") or "")
        rows = []
        if directory.exists():
            for path in sorted(directory.glob("*.csv")):
                try:
                    bars = data_mod.read_csv(path, symbol=path.stem)
                except data_mod.DataError:
                    continue
                rows.append(
                    {
                        "symbol": path.stem.upper(),
                        "file": str(path),
                        "count": len(bars),
                        "first_date": bars[0]["date"],
                        "last_date": bars[-1]["date"],
                        "last_close": bars[-1]["close"],
                        "bytes": path.stat().st_size,
                    }
                )
        return {
            "cache_dir": str(directory),
            "as_of": _now(),
            "symbols_cached": len(rows),
            "symbols": rows,
        }

    def _simulation_dir(self):
        directory = Path(self.cfg.get("state_dir") or "trader") / "simulations"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def create_simulation(self, request):
        symbol = str(request.get("symbol") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9&_.-]{1,24}", symbol):
            raise ApiError("symbol is required and must contain only letters, numbers, &, _, ., or -", 400)
        action = str(request.get("action") or "buy").strip().lower()
        if action not in ("buy", "sell"):
            raise ApiError("action must be buy or sell", 400)
        try:
            shares = int(request.get("shares"))
            days = int(request.get("days"))
        except (TypeError, ValueError):
            raise ApiError("shares and days must be integers", 400)
        if not (1 <= shares <= 10000000):
            raise ApiError("shares must be between 1 and 10000000", 400)
        if not (1 <= days <= 252):
            raise ApiError("days must be between 1 and 252 sessions", 400)
        try:
            iterations = int(request.get("iterations", 10000))
            seed = int(request.get("seed", self.cfg["timemachine"]["seed"]))
        except (TypeError, ValueError):
            raise ApiError("iterations and seed must be integers", 400)
        if not (1 <= iterations <= MAX_ITERATIONS):
            raise ApiError("iterations must be between 1 and %d" % MAX_ITERATIONS, 400)
        entry = request.get("entry_price")
        if entry is not None:
            try:
                entry = float(entry)
            except (TypeError, ValueError):
                raise ApiError("entry_price must be a number", 400)
            if entry <= 0:
                raise ApiError("entry_price must be positive", 400)
        with self.lock:
            bars = self.source.bars(symbol, refresh=bool(request.get("refresh", False)))
            if entry is None:
                entry = bars[-1]["close"]
            result = simulation_mod.run(symbol, bars, action, shares, days, entry, iterations, seed, self.cfg)
        simulation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        payload = dict(result)
        payload["id"] = simulation_id
        payload["cache_dir"] = self.cfg["data"].get("cache_dir") or ""
        if store.backend() == "postgres":
            payload["simulation_dir"] = ""
            return store.save_simulation(payload)
        payload["simulation_dir"] = str(self._simulation_dir() / simulation_id)
        folder = self._simulation_dir() / simulation_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def get_simulation(self, simulation_id):
        if not re.fullmatch(r"[A-Za-z0-9T_-]{1,80}", simulation_id):
            raise ApiError("bad simulation id", 400)
        if store.backend() == "postgres":
            payload = store.get_simulation(simulation_id)
            if payload is None:
                raise ApiError("simulation not found", 404)
            return payload
        path = self._simulation_dir() / simulation_id / "result.json"
        if not path.is_file():
            raise ApiError("simulation not found", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def simulations(self):
        if store.backend() == "postgres":
            return {"simulations": store.list_simulations(), "directory": ""}
        rows = []
        for path in sorted(self._simulation_dir().glob("*/result.json"), reverse=True):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                rows.append({"id": item.get("id"), "symbol": item.get("symbol"), "action": item.get("action"), "shares": item.get("shares"), "days": item.get("days"), "generated_at": item.get("generated_at")})
            except (OSError, ValueError):
                continue
        return {"simulations": rows, "directory": str(self._simulation_dir())}

    def _future_dir(self):
        directory = Path(self.cfg.get("state_dir") or "trader") / "future_simulations"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _future_path(self, simulation_id):
        return self._future_dir() / (simulation_id + ".json")

    def create_future_simulation(self, request):
        symbol = str(request.get("symbol") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9&_.-]{1,24}", symbol):
            raise ApiError("symbol is required and must contain only letters, numbers, &, _, ., or -", 400)
        action = str(request.get("action") or "buy").strip().lower()
        if action not in ("buy", "sell"):
            raise ApiError("action must be buy or sell", 400)
        shares = request.get("shares")
        if isinstance(shares, bool):
            raise ApiError("shares must be an integer", 400)
        try:
            shares = int(shares)
        except (TypeError, ValueError):
            raise ApiError("shares must be an integer", 400)
        if not (1 <= shares <= 10000000):
            raise ApiError("shares must be between 1 and 10000000", 400)
        price = request.get("price")
        if price is None:
            price = request.get("entry_price")
        if price is None:
            price = request.get("sell_price")
        if isinstance(price, bool):
            raise ApiError("price must be a number", 400)
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise ApiError("price must be a number", 400)
        if price <= 0:
            raise ApiError("price must be positive", 400)
        try:
            days = future_mod.normalise_days(request.get("days", [4]))
            iterations = int(request.get("iterations", 1000))
            seed = request.get("seed")
            seed = None if seed is None else int(seed)
        except ValueError as exc:
            raise ApiError(str(exc), 400)
        if not (1 <= iterations <= MAX_ITERATIONS):
            raise ApiError("iterations must be between 1 and %d" % MAX_ITERATIONS, 400)
        with self.lock:
            bars = self.source.bars(symbol, refresh=bool(request.get("refresh", False)))
            payload = future_mod.new_record(
                symbol, action, shares, price, days, self.cfg, bars,
                iterations=iterations, seed=seed,
            )
        simulation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        payload["id"] = simulation_id
        payload["cache_dir"] = self.cfg["data"].get("cache_dir") or ""
        if store.backend() == "postgres":
            payload["simulation_dir"] = ""
            return store.save_future_simulation(payload)
        payload["simulation_dir"] = str(self._future_dir())
        self._future_path(simulation_id).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        return payload

    def get_future_simulation(self, simulation_id, refresh=False):
        if not re.fullmatch(r"[A-Za-z0-9T_-]{1,80}", simulation_id):
            raise ApiError("bad simulation id", 400)
        if store.backend() == "postgres":
            payload = store.get_future_simulation(simulation_id)
            if payload is None:
                raise ApiError("simulation not found", 404)
        else:
            path = self._future_path(simulation_id)
            if not path.is_file():
                raise ApiError("simulation not found", 404)
            payload = json.loads(path.read_text(encoding="utf-8"))
        with self.lock:
            bars = self.source.bars(payload["symbol"], refresh=refresh)
            updated = future_mod.settle(payload, bars)
        if store.backend() == "postgres":
            return store.save_future_simulation(updated)
        self._future_path(simulation_id).write_text(
            json.dumps(updated, indent=2), encoding="utf-8"
        )
        return updated

    def future_simulations(self):
        if store.backend() == "postgres":
            return {"simulations": store.list_future_simulations(), "directory": ""}
        rows = []
        for path in sorted(self._future_dir().glob("*.json"), reverse=True):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                rows.append(
                    {
                        "id": item.get("id"),
                        "symbol": item.get("symbol"),
                        "action": item.get("action"),
                        "shares": item.get("shares"),
                        "price": item.get("price"),
                        "created_date": item.get("created_date"),
                        "status": item.get("status"),
                        "generated_at": item.get("generated_at"),
                    }
                )
            except (OSError, ValueError):
                continue
        return {"simulations": rows, "directory": str(self._future_dir())}


class Handler(BaseHTTPRequestHandler):
    server_version = "nepse-trader-api/1.0"
    app = None
    cors_origins = ()

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))

    # -- response plumbing -------------------------------------------
    def _cors(self):
        origin = self.headers.get("Origin")
        if origin in self.cors_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization, Idempotency-Key"
        )

    def _json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ApiError("request body is too large", 400)
        try:
            # utf-8-sig so a client that prefixes a BOM (PowerShell, .NET) works
            payload = json.loads(self.rfile.read(length).decode("utf-8-sig") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError("request body must be valid JSON", 400)
        if not isinstance(payload, dict):
            raise ApiError("request body must be a JSON object", 400)
        return payload

    def _require_cron(self):
        """Constant-time Bearer check against the CRON_SECRET env var."""
        secret = (os.environ.get("CRON_SECRET") or "").strip()
        if not secret:
            raise ApiError("CRON_SECRET is not configured on the server", 503)
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            raise ApiError("unauthorized: send Authorization: Bearer <CRON_SECRET>", 401)
        if not hmac.compare_digest(header[len("Bearer ") :].strip(), secret):
            raise ApiError("unauthorized", 401)

    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status, no_store=True)

    def _send_bytes(self, body, content_type, status=200, no_store=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status):
        self._send({"error": message, "status": status}, status)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) != 3 or parts[0] != "api" or parts[1] != "watchlist":
            return self._error("unknown route %s" % self.path, 404)
        symbol = parts[2].upper()
        if not re.fullmatch(r"[A-Z0-9&_.-]{1,24}", symbol):
            return self._error("bad symbol %r" % parts[2], 400)
        try:
            removed = runs_mod.remove_symbol(symbol)
            return self._send(
                {"removed": removed, "symbols": runs_mod.watchlist()}
            )
        except runs_mod.RunError as exc:
            return self._error(str(exc), 400)
        except db_mod.DbError as exc:
            return self._error(str(exc), 503)
        except Exception as exc:
            return self._error("watchlist update failed: %s" % exc, 500)

    def do_POST(self):
        route = self.path.split("?")[0].rstrip("/")
        if route == "/api/suggestions":
            try:
                self._require_cron()
                body = self._json_body()
                notify = body.get("notify", False)
                if not isinstance(notify, bool):
                    raise ApiError("notify must be true or false", 400)
                payload = runs_mod.execute(
                    self.app.cfg,
                    body.get("symbols"),
                    notify=notify,
                    idempotency_key=(self.headers.get("Idempotency-Key") or "").strip()
                    or None,
                )
                return self._send(payload, 200 if payload.get("idempotent") else 201)
            except ApiError as exc:
                return self._error(str(exc), exc.status)
            except runs_mod.RunError as exc:
                return self._error(str(exc), 400)
            except db_mod.DbError as exc:
                return self._error(str(exc), 503)
            except Exception as exc:  # never leak a traceback to the cron job
                return self._error("suggestion run failed: %s" % exc, 500)
        if route == "/api/watchlist":
            try:
                body = self._json_body()
                return self._send({"symbols": runs_mod.add_symbols(body.get("symbols"))})
            except ApiError as exc:
                return self._error(str(exc), exc.status)
            except runs_mod.RunError as exc:
                return self._error(str(exc), 400)
            except db_mod.DbError as exc:
                return self._error(str(exc), 503)
            except Exception as exc:
                return self._error("watchlist update failed: %s" % exc, 500)
        if route == "/api/future-simulations":
            try:
                request = self._json_body()
                return self._send(self.app.create_future_simulation(request), 201)
            except ApiError as exc:
                return self._error(str(exc), exc.status)
            except data_mod.DataError as exc:
                return self._error(str(exc), 404)
            except Exception as exc:
                return self._error("future simulation failed: %s" % exc, 500)
        if route == "/api/simulations":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1024 * 1024:
                    raise ApiError("request body is too large", 400)
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(request, dict):
                    raise ApiError("request body must be a JSON object", 400)
                return self._send(self.app.create_simulation(request), 201)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return self._error("request body must be valid JSON", 400)
            except ApiError as exc:
                return self._error(str(exc), exc.status)
            except data_mod.DataError as exc:
                return self._error(str(exc), 404)
            except Exception as exc:
                return self._error("simulation failed: %s" % exc, 500)
        if route != "/api/alerts/check":
            return self._error(
                "POST is only supported on /api/suggestions, /api/watchlist, "
                "/api/alerts/check, /api/simulations and /api/future-simulations",
                405,
            )
        try:
            app = self.app
            path = alerts_mod.path_for(app.cfg)
            current = alerts_mod.load(path)
            with app.lock:
                fired = alerts_mod.check(current, app.source, bars_for=app.source.bars)
            alerts_mod.save(path, current)
            self._send(
                {
                    "generated_at": _now(),
                    "fired": fired,
                    "armed": sum(1 for a in current if not a["triggered"]),
                    "alerts": current,
                }
            )
        except data_mod.DataError as exc:
            self._error(str(exc), 502)
        except Exception as exc:  # never leak a traceback to the frontend
            self._error("alerts check failed: %s" % exc, 500)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        app = self.app
        try:
            if path in ("/", "/api"):
                return self._send(
                    {
                        "service": "nepse-trader",
                        "endpoints": {
                            "GET /api/health": "liveness",
                            "GET /api/symbols": "configured universe and cached symbols",
                            "GET /api/cache": "one-year bar cache inventory",
                            "GET /api/bars/{symbol}": "daily OHLCV history (?limit=&refresh=1)",
                            "GET /api/scan?symbols=A,B": "fired setups",
                            "GET /api/analyze/{symbol}": "setups, plan, sizing, risk, timemachine, bars",
                            "GET /api/report?symbols=A,B": "analyze every symbol (?bars=1)",
                            "GET /api/alerts": "stored alerts",
                            "POST /api/simulations": "custom paper-trade forecast (JSON body)",
                            "GET /api/simulations": "saved custom simulations",
                            "GET /api/simulations/{id}": "saved custom simulation result",
                            "POST /api/future-simulations": "log a paper trade with future horizons",
                            "GET /api/future-simulations": "saved future simulations",
                            "GET /api/future-simulations/{id}": "settled future simulation",
                            "POST /api/alerts/check": "evaluate alerts against the latest quote",
                            "POST /api/suggestions": "BUY/SELL/HOLD run (Bearer CRON_SECRET; JSON body)",
                            "GET /api/suggestions/latest": "latest persisted suggestion run",
                            "GET /api/watchlist": "watchlist symbols used when a run omits symbols",
                            "POST /api/watchlist": "add symbols to the watchlist",
                            "DELETE /api/watchlist/{symbol}": "remove a symbol from the watchlist",
                        },
                    }
                )
            if path == "/api/health":
                return self._send(
                    {"ok": True, "as_of": _now(), "data_kind": app.cfg["data"].get("kind")}
                )
            if path == "/api/symbols":
                return self._send(
                    {
                        "universe": data_mod.universe(app.cfg, app.source),
                        "cached": [row["symbol"] for row in app.cache_inventory()["symbols"]],
                    }
                )
            if path == "/api/cache":
                return self._send(app.cache_inventory())
            if path == "/api/cache/all":
                return self._send(app.cache_all(include_bars=_flag(query, "bars", True)))
            if path == "/api/alerts":
                return self._send({"alerts": alerts_mod.load(alerts_mod.path_for(app.cfg))})
            if path == "/api/simulations":
                return self._send(app.simulations())
            if path == "/api/future-simulations":
                return self._send(app.future_simulations())
            if path == "/api/watchlist":
                return self._send(
                    {"name": store.DEFAULT_WATCHLIST, "symbols": runs_mod.watchlist()}
                )
            if path == "/api/suggestions/latest":
                payload = runs_mod.latest()
                if payload is None:
                    raise ApiError("no suggestion runs yet", 404)
                return self._send(payload)
            if path == "/api/scan":
                symbols = app.symbols(query)
                if len(symbols) > 200:
                    raise ApiError("too many symbols (max 200 per request)", 400)
                return self._send(app.scan(symbols, refresh=_flag(query, "refresh")))
            if path == "/api/report":
                symbols = app.symbols(query)
                if len(symbols) > 50:
                    raise ApiError("too many symbols for a report (max 50)", 400)
                return self._send(
                    app.report(
                        symbols,
                        include_bars=_flag(query, "bars"),
                        refresh=_flag(query, "refresh"),
                        iterations=_int(query, "iterations"),
                    )
                )

            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "simulations":
                return self._send(app.get_simulation(parts[2]))
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "future-simulations":
                return self._send(
                    app.get_future_simulation(parts[2], refresh=_flag(query, "refresh"))
                )
            if (
                len(parts) == 3
                and parts[0] == "api"
                and parts[1] == "cache"
                and parts[2].lower().endswith(".csv")
            ):
                symbol = parts[2][:-4].upper()
                if not re.fullmatch(r"[A-Z0-9&_.-]{1,24}", symbol):
                    raise ApiError("bad symbol %r" % parts[2], 400)
                directory = Path(app.cfg["data"].get("cache_dir") or "")
                file = directory / (symbol + ".csv")
                if not file.is_file():
                    raise ApiError("no cached csv for %s" % symbol, 404)
                return self._send_bytes(file.read_bytes(), "text/csv; charset=utf-8")

            if len(parts) == 3 and parts[0] == "api" and parts[1] == "bars":
                symbol = parts[2].upper()
                with app.lock:
                    bars = app.source.bars(symbol, refresh=_flag(query, "refresh"))
                limit = _int(query, "limit")
                if limit is not None:
                    bars = bars[-max(1, limit) :]
                return self._send(
                    {
                        "symbol": symbol,
                        "generated_at": _now(),
                        "data": app._bar_summary(bars),
                        "bars": bars,
                    }
                )

            if len(parts) == 3 and parts[0] == "api" and parts[1] == "analyze":
                levels = None
                if query.get("entry") or query.get("stop"):
                    levels = {
                        "entry": _float(query, "entry"),
                        "stop": _float(query, "stop"),
                        "target": _float(query, "target"),
                    }
                payload = app.analyze(
                    parts[2].upper(),
                    include_bars=_flag(query, "bars", True),
                    refresh=_flag(query, "refresh"),
                    iterations=_int(query, "iterations"),
                    levels=levels,
                )
                return self._send(payload)

            return self._error("unknown route %s" % path, 404)
        except ApiError as exc:
            self._error(str(exc), exc.status)
        except db_mod.DbError as exc:
            self._error(str(exc), 503)
        except data_mod.DataError as exc:
            message = str(exc)
            status = 404 if "not in the NEPSE security list" in message or "no data for" in message else 502
            self._error(message, status)
        except Exception as exc:  # pragma: no cover - defensive
            self._error("analysis failed: %s" % exc, 500)


def build_server(cfg, host="127.0.0.1", port=8000):
    handler = type("BoundHandler", (Handler,), {"app": App(cfg)})
    api_cfg = cfg.get("api") or {}
    origins = api_cfg.get("cors_origins", [])
    if isinstance(origins, str):
        origins = [origins]
    else:
        origins = list(origins)
    frontend_origin = os.environ.get("FRONTEND_CORS_ORIGIN", "").strip()
    if frontend_origin:
        origins.append(frontend_origin)
    handler.cors_origins = frozenset(
        origin.rstrip("/") for origin in origins if isinstance(origin, str)
    )
    return ThreadingHTTPServer((host, int(port)), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nepse-trader-api", description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    cfg = config_mod.load(args.config)
    server = build_server(cfg, args.host, args.port)
    print(
        "nepse-trader API on http://%s:%d  (config %s, data %s)"
        % (args.host, args.port, args.config or "defaults", cfg["data"].get("kind"))
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
