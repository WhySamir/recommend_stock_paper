"""NEPSE bar loading: CSV files or your own JSON HTTP API.

Both sources return daily bars sorted oldest-first as dicts with
``date`` (YYYY-MM-DD) and float ``open/high/low/close/volume``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from . import nepse_api

BAR_FIELDS = ("date", "open", "high", "low", "close", "volume")
DEFAULT_USER_AGENT = "nepse-trader/1.0 (personal research tool)"


class DataError(RuntimeError):
    pass


def normalize_date(value):
    text = str(value).strip()
    if not text:
        raise DataError("empty date value")
    text = text.split("T")[0].split(" ")[0].replace("/", "-").replace(".", "-")
    parts = text.split("-")
    if len(parts) != 3:
        raise DataError("unparseable date: %r" % value)
    try:
        year, month, day = (int(p) for p in parts)
    except ValueError:
        raise DataError("unparseable date: %r" % value)
    if year < 1000:
        raise DataError("unparseable date (need YYYY-MM-DD): %r" % value)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise DataError("unparseable date: %r" % value)
    return "%04d-%02d-%02d" % (year, month, day)


def _as_float(value, field, symbol):
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        raise DataError("%s: bad %s value %r" % (symbol, field, value))


def _as_bar(record, columns, symbol):
    raw = {}
    for field in BAR_FIELDS:
        key = columns.get(field, field)
        if key not in record:
            raise DataError("%s: missing field %r in record" % (symbol, key))
        raw[field] = record[key]
    bar = {"date": normalize_date(raw["date"])}
    for field in ("open", "high", "low", "close", "volume"):
        bar[field] = _as_float(raw[field], field, symbol)
    if bar["close"] <= 0 or bar["high"] < bar["low"]:
        return None
    bar["high"] = max(bar["high"], bar["open"], bar["close"])
    bar["low"] = min(bar["low"], bar["open"], bar["close"])
    return bar


def _clean(bars):
    by_date = {}
    for bar in bars:
        if bar:
            by_date[bar["date"]] = bar
    return [by_date[key] for key in sorted(by_date)]


def read_csv(path, columns=None, symbol=None):
    path = Path(path)
    symbol = symbol or path.stem
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise DataError("%s: empty csv" % path)
        header = {name.strip().lower(): name for name in reader.fieldnames}
        cols = {}
        for field in BAR_FIELDS:
            wanted = (columns or {}).get(field, field).strip().lower()
            if wanted not in header:
                raise DataError(
                    "%s: no %r column (found: %s)"
                    % (path, wanted, ", ".join(reader.fieldnames))
                )
            cols[field] = header[wanted]
        bars = []
        for row in reader:
            bars.append(_as_bar(row, cols, symbol))
    bars = _clean(bars)
    if not bars:
        raise DataError("%s: no usable rows" % path)
    return bars


def write_csv(path, bars):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(BAR_FIELDS))
        writer.writeheader()
        for bar in bars:
            writer.writerow({field: bar[field] for field in BAR_FIELDS})


def _dig(payload, path):
    node = payload
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                raise DataError("records_path: no %r in list" % part)
        elif isinstance(node, dict):
            if part not in node:
                raise DataError("records_path: no %r in response" % part)
            node = node[part]
        else:
            raise DataError("records_path: cannot descend into %r" % part)
    return node


def _expand(value):
    def repl(match):
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            raise DataError("environment variable %s is not set" % name)
        return os.environ[name]

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, str(value))


class CsvSource:
    """Reads ``<SYMBOL>.csv`` files from a directory. Fully offline."""

    def __init__(self, cfg):
        data_cfg = cfg["data"]
        self.directory = Path(data_cfg["dir"])
        self.columns = data_cfg.get("columns") or {}

    def bars(self, symbol, refresh=False):
        path = self.directory / ("%s.csv" % symbol.upper())
        if not path.exists():
            raise DataError("no data for %s: %s does not exist" % (symbol, path))
        return read_csv(path, self.columns, symbol)

    def last(self, symbol):
        bars = self.bars(symbol)
        return bars[-1]["close"], bars[-1]["date"]


class HttpSource:
    """Reads daily bars from your NEPSE JSON API, cached to CSV."""

    def __init__(self, cfg):
        data_cfg = cfg["data"]
        self.api = data_cfg.get("api") or {}
        self.columns = data_cfg.get("columns") or {}
        cache_dir = data_cfg.get("cache_dir")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = float(self.api.get("timeout", 20))
        if not self.api.get("history_url"):
            raise DataError("data.api.history_url is empty")

    def _url(self, symbol):
        mapped = (self.api.get("symbol_map") or {}).get(symbol.upper(), symbol)
        return (
            _expand(self.api["history_url"])
            .replace("{symbol}", str(mapped))
            .replace("{symbol_lower}", str(mapped).lower())
        )

    def _get_json(self, url):
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
        for key, value in (self.api.get("headers") or {}).items():
            headers[key] = _expand(value)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except Exception as exc:  # network, HTTP, TLS, timeout
            raise DataError("request failed for %s: %s" % (url, exc))
        try:
            return json.loads(body)
        except ValueError:
            raise DataError("response from %s was not JSON" % url)

    def bars(self, symbol, refresh=False):
        cache = None
        if self.cache_dir is not None:
            cache = self.cache_dir / ("%s.csv" % symbol.upper())
            if cache.exists() and not refresh:
                return read_csv(cache, self.columns, symbol)
        payload = self._get_json(self._url(symbol))
        records = _dig(payload, self.api.get("records_path"))
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise DataError("API returned %s, expected a list" % type(records).__name__)
        bars = _clean([_as_bar(r, self.api.get("fields") or {}, symbol) for r in records])
        if not bars:
            raise DataError("%s: API returned no usable bars" % symbol)
        if cache is not None:
            write_csv(cache, bars)
        return bars

    def last(self, symbol):
        quote_url = self.api.get("quote_url")
        if not quote_url:
            bars = self.bars(symbol)
            return bars[-1]["close"], bars[-1]["date"]
        mapped = (self.api.get("symbol_map") or {}).get(symbol.upper(), symbol)
        payload = self._get_json(
            _expand(quote_url).replace("{symbol}", str(mapped)).replace(
                "{symbol_lower}", str(mapped).lower()
            )
        )
        fields = self.api.get("quote_fields") or {}
        price = _dig(payload, fields.get("last", "lastTradedPrice"))
        as_of = _dig(payload, fields.get("date", "date")) if fields.get("date") else ""
        return _as_float(price, "last", symbol), str(as_of or "")

    def session_open(self, symbol):
        """Opening price of the latest session, if ``quote_fields.open`` is set."""
        fields = self.api.get("quote_fields") or {}
        quote_url = self.api.get("quote_url")
        if not quote_url or not fields.get("open"):
            return None
        mapped = (self.api.get("symbol_map") or {}).get(symbol.upper(), symbol)
        payload = self._get_json(
            _expand(quote_url).replace("{symbol}", str(mapped)).replace(
                "{symbol_lower}", str(mapped).lower()
            )
        )
        try:
            price = _as_float(_dig(payload, fields["open"]), "open", symbol)
        except DataError:
            return None
        as_of = _dig(payload, fields.get("date", "date")) if fields.get("date") else ""
        if price <= 0 or not as_of:
            return None
        return price, str(as_of)


class NepseOfficialSource:
    """Daily bars and quotes from the official NEPSE JSON API.

    The API exposes roughly one year of daily history.  Every successful fetch
    is merged into ``data/cache/<SYMBOL>.csv``, so a local series accumulates
    beyond that window as the tool is used.  The official history has no open
    price; it is filled from the previous session's close, which is also the
    Time Machine's entry assumption.
    """

    def __init__(self, cfg):
        data_cfg = cfg["data"]
        official = data_cfg.get("official") or {}
        self.client = nepse_api.NepseClient(
            base_url=official.get("base_url") or nepse_api.BASE_URL,
            timeout=float(official.get("timeout", 30)),
            verify_tls=bool(official.get("verify_tls", True)),
            min_interval=float(official.get("min_interval", 0.25)),
            ttl=float(official.get("token_ttl_seconds", 35)),
        )
        self.history_days = int(official.get("history_days", 400))
        self.cache_ttl = float(data_cfg.get("cache_ttl_minutes", 60)) * 60.0
        self.map_ttl = float(official.get("security_map_ttl_hours", 24)) * 3600.0
        cache_dir = data_cfg.get("cache_dir")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._symbol_ids = None
        self._map_loaded_at = 0.0
        self._quotes = {}

    # -- security id map ----------------------------------------------
    def _map_path(self):
        return self.cache_dir / "security_map.json" if self.cache_dir else None

    def _read_map(self):
        path = self._map_path()
        if path is None or not path.exists():
            return {}, 0.0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("symbols") or {}, float(payload.get("fetched_at", 0))
        except (OSError, ValueError, TypeError):
            return {}, 0.0

    def _security_map(self):
        now = time.time()
        if self._symbol_ids is not None and (now - self._map_loaded_at) < self.map_ttl:
            return self._symbol_ids
        symbols, fetched_at = self._read_map()
        if symbols and (now - fetched_at) < self.map_ttl:
            self._symbol_ids = {str(k).upper(): v for k, v in symbols.items()}
            self._map_loaded_at = now
            return self._symbol_ids
        try:
            rows = self.client.security_list()
            mapping = {
                str(row["symbol"]).upper(): row["id"]
                for row in rows
                if row.get("symbol") and row.get("id") is not None
            }
            if not mapping:
                raise DataError("NEPSE returned an empty security list")
            self._symbol_ids = mapping
            self._map_loaded_at = now
            path = self._map_path()
            if path is not None:
                path.write_text(
                    json.dumps({"fetched_at": now, "symbols": mapping}, indent=2) + "\n",
                    encoding="utf-8",
                )
            return mapping
        except nepse_api.NepseError as exc:
            if symbols:
                self._symbol_ids = {str(k).upper(): v for k, v in symbols.items()}
                self._map_loaded_at = now
                return self._symbol_ids
            raise DataError("could not load the NEPSE security list: %s" % exc)

    def _symbol_id(self, symbol):
        key = symbol.upper()
        mapping = self._security_map()
        if key not in mapping:
            raise DataError("%s is not in the NEPSE security list" % key)
        return mapping[key]

    # -- bars ---------------------------------------------------------
    def _cache_path(self, symbol):
        return self.cache_dir / ("%s.csv" % symbol.upper()) if self.cache_dir else None

    def _cache_is_fresh(self, path):
        if path is None or self.cache_ttl <= 0:
            return False
        try:
            return (time.time() - path.stat().st_mtime) < self.cache_ttl
        except OSError:
            return False

    def _read_cache(self, path, symbol):
        if path is None or not path.exists():
            return []
        try:
            return read_csv(path, symbol=symbol)
        except DataError:
            return []

    def _fetch_bars(self, symbol, security_id):
        end = date.today()
        start = end - timedelta(days=max(30, self.history_days))
        try:
            payload = self.client.history(security_id, start, end)
        except nepse_api.NepseError as exc:
            raise DataError("%s: NEPSE history request failed: %s" % (symbol, exc))
        rows = payload.get("content") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise DataError("%s: unexpected NEPSE history response" % symbol)
        bars = []
        for row in rows:
            try:
                bar = {
                    "date": normalize_date(row["businessDate"]),
                    "open": None,
                    "high": _as_float(row.get("highPrice"), "highPrice", symbol),
                    "low": _as_float(row.get("lowPrice"), "lowPrice", symbol),
                    "close": _as_float(row.get("closePrice"), "closePrice", symbol),
                    "volume": max(
                        0.0,
                        _as_float(
                            row.get("totalTradedQuantity"),
                            "totalTradedQuantity",
                            symbol,
                        ),
                    ),
                }
            except (KeyError, TypeError, DataError):
                continue
            if (
                bar["close"] <= 0
                or bar["high"] <= 0
                or bar["low"] <= 0
                or bar["high"] < bar["low"]
            ):
                continue
            bars.append(bar)
        if not bars:
            raise DataError("%s: NEPSE returned no usable daily bars" % symbol)
        return _clean(bars)

    def bars(self, symbol, refresh=False):
        symbol = symbol.upper()
        cache = self._cache_path(symbol)
        cached = self._read_cache(cache, symbol)
        if cached:
            today = date.today().isoformat()
            cached = [bar for bar in cached if bar["date"] <= today]
        if cached and not refresh and self._cache_is_fresh(cache):
            return cached

        try:
            security_id = self._symbol_id(symbol)
            fresh = self._fetch_bars(symbol, security_id)
        except DataError:
            if cached:
                return cached
            raise

        merged = {bar["date"]: bar for bar in cached}
        merged.update({bar["date"]: bar for bar in fresh})
        bars = [merged[key] for key in sorted(merged)]

        previous_close = None
        for bar in bars:
            if bar.get("open") is None:
                bar["open"] = previous_close if previous_close is not None else bar["close"]
            previous_close = bar["close"]

        if cache is not None:
            write_csv(cache, bars)
        return bars

    # -- quote --------------------------------------------------------
    def _quote_data(self, symbol):
        """Latest ``securityMcsData`` dict, memoized for 20 seconds."""
        symbol = symbol.upper()
        cached = self._quotes.get(symbol)
        if cached and (time.monotonic() - cached[0]) < 20.0:
            return cached[1]
        security_id = self._symbol_id(symbol)
        try:
            payload = self.client.quote(security_id)
        except nepse_api.NepseError:
            payload = None
        if isinstance(payload, dict):
            mcs = payload.get("securityMcsData") or {}
            self._quotes[symbol] = (time.monotonic(), mcs)
            return mcs
        return None

    def last(self, symbol):
        symbol = symbol.upper()
        mcs = self._quote_data(symbol)
        if mcs:
            try:
                price = _as_float(mcs.get("lastTradedPrice"), "lastTradedPrice", symbol)
            except DataError:
                price = 0.0
            as_of = str(mcs.get("businessDate") or "")
            if price > 0:
                return price, as_of
        bars = self.bars(symbol)
        return bars[-1]["close"], bars[-1]["date"]

    def session_open(self, symbol):
        """Opening price and date of the latest session, or ``None``."""
        symbol = symbol.upper()
        mcs = self._quote_data(symbol)
        if not mcs:
            return None
        try:
            price = _as_float(mcs.get("openPrice"), "openPrice", symbol)
        except DataError:
            return None
        as_of = str(mcs.get("businessDate") or "")
        if price <= 0 or not as_of:
            return None
        return price, as_of


def open_source(cfg):
    kind = (cfg["data"].get("kind") or "csv").lower()
    if kind == "csv":
        return CsvSource(cfg)
    if kind == "http":
        return HttpSource(cfg)
    if kind in ("nepse", "official", "nepalstock"):
        return NepseOfficialSource(cfg)
    raise DataError(
        "unknown data.kind %r (use 'csv', 'http' or 'nepse')" % kind
    )


def universe(cfg, source, symbols=None, limit=None):
    names = []
    for item in symbols or cfg.get("universe") or []:
        names.append(str(item).strip().upper())
    if not names:
        directory = Path(cfg["data"]["dir"])
        if directory.exists():
            names = sorted(p.stem.upper() for p in directory.glob("*.csv"))
    names = sorted({n for n in names if n})
    if limit:
        names = names[:limit]
    return names
