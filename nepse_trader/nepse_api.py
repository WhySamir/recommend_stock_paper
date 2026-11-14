"""Official NEPSE JSON API client.

The public site (nepalstock.com.np) protects ``/api/nots`` with a short-lived
token sent as ``Authorization: Salter <token>``. The token is returned by
``/api/authenticate/prove`` and obfuscated by inserting five characters. The
positions of those characters are derived from ``salt2``; the arithmetic below
is a pure-Python port of the site's ``css.wasm`` helper, checked against the
binary for the full 00000-99999 salt range.

Only GET requests are needed: daily bars, one quote, and the security list.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

BASE_URL = "https://www.nepalstock.com.np"
DEFAULT_USER_AGENT = "nepse-trader/1.0 (personal research tool)"

# lookup table embedded in NEPSE's css.wasm
_LOOKUP = (
    5, 8, 4, 7, 9, 4, 6, 9, 5, 5,
    6, 5, 3, 5, 4, 4, 9, 6, 6, 8,
    8, 6, 8, 6, 5, 8, 4, 9, 5, 9,
    8, 5, 3, 4, 7, 7, 4, 7, 3, 9,
)


class NepseError(RuntimeError):
    """Raised when the official API cannot be reached or returns bad data."""


def _digits(value):
    value = abs(int(value))
    return value % 10, (value // 10) % 10, (value // 100) % 10


def token_offsets(salt2):
    """Return the five cut positions used to de-obfuscate the access token."""
    ones, tens, hundreds = _digits(salt2)
    c = _LOOKUP[ones + tens + hundreds] + 22
    pair = tens + hundreds
    r = pair + _LOOKUP[pair + ones] + 32
    b = pair + _LOOKUP[pair + ones] + 60
    n = tens + _LOOKUP[tens + ones + hundreds] + 88
    m = hundreds + _LOOKUP[hundreds + tens + ones] + 110
    return c, r, b, n, m


def parse_token(prove):
    """Turn a ``/api/authenticate/prove`` payload into a usable Salter token."""
    token = prove.get("accessToken")
    if not token:
        raise NepseError("prove response has no accessToken")
    try:
        salt2 = int(prove["salt2"])
    except (KeyError, TypeError, ValueError):
        raise NepseError("prove response has no usable salt2")
    c, r, b, n, m = token_offsets(salt2)
    if not (0 <= c < r < b < n < m <= len(token)):
        raise NepseError("token offsets are out of range for this response")
    return (
        token[:c]
        + token[c + 1:r]
        + token[r + 1:b]
        + token[b + 1:n]
        + token[n + 1:m]
        + token[m + 1:]
    )


class NepseClient:
    """Small authenticated JSON client for the official NEPSE endpoints."""

    def __init__(
        self,
        base_url=BASE_URL,
        timeout=30.0,
        verify_tls=True,
        min_interval=0.25,
        ttl=35.0,
        user_agent=DEFAULT_USER_AGENT,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.min_interval = max(0.0, float(min_interval))
        self.ttl = max(1.0, float(ttl))
        self.user_agent = user_agent
        self.context = ssl.create_default_context()
        if not verify_tls:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self._token = None
        self._minted_at = 0.0
        self._last_request = 0.0

    # -- auth ---------------------------------------------------------
    def token(self, force=False):
        if not force and self._token and (time.monotonic() - self._minted_at) < self.ttl:
            return self._token
        payload = self._request("/api/authenticate/prove", auth=False, retries=1)
        self._token = parse_token(payload)
        self._minted_at = time.monotonic()
        return self._token

    def invalidate(self):
        self._token = None
        self._minted_at = 0.0

    # -- transport ----------------------------------------------------
    def _throttle(self):
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _headers(self, auth):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "close",
            "Referer": self.base_url + "/",
            "Origin": self.base_url,
        }
        if auth:
            headers["Authorization"] = "Salter " + self.token()
        return headers

    def _request(self, path, params=None, auth=True, retries=2):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last_error = None
        for attempt in range(retries + 1):
            self._throttle()
            request = urllib.request.Request(url, headers=self._headers(auth))
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=self.context
                ) as response:
                    body = response.read().decode("utf-8")
                if not body.strip():
                    raise NepseError("empty response from %s" % path)
                try:
                    return json.loads(body)
                except ValueError:
                    raise NepseError(
                        "non-JSON response from %s: %s" % (path, body[:160].strip())
                    )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200].strip()
                last_error = NepseError(
                    "HTTP %s from %s%s"
                    % (exc.code, path, (": " + detail) if detail else "")
                )
                if auth and exc.code in (401, 403) and attempt < retries:
                    self.invalidate()
                    continue
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise last_error
            except NepseError as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise
            except Exception as exc:  # network, TLS, timeout, socket
                last_error = NepseError("request failed for %s: %s" % (path, exc))
                if attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise last_error
        raise last_error or NepseError("request failed for %s" % path)

    # -- endpoints ----------------------------------------------------
    def security_list(self):
        """All active securities as ``[{id, symbol, securityName, ...}]``."""
        rows = self._request("/api/nots/security", params={"nonDelisted": "true"})
        if isinstance(rows, dict):
            rows = rows.get("content") or rows.get("data") or []
        if not isinstance(rows, list):
            raise NepseError("unexpected security-list response")
        return rows

    def history(self, security_id, start, end, size=500):
        """Daily bars for one security id. ``start``/``end`` are date objects."""
        if not isinstance(start, date) or not isinstance(end, date):
            raise NepseError("history needs date objects for start and end")
        return self._request(
            "/api/nots/market/history/security/%s" % security_id,
            params={
                "size": int(size),
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            },
        )

    def quote(self, security_id):
        """Latest quote payload, including ``securityMcsData``."""
        return self._request("/api/nots/security/%s" % security_id)