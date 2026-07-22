"""Shared HTTP layer: token-bucket rate limiting, retry with backoff, no hard deps."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

USER_AGENT = "edge-engine/0.1"


class RateLimiter:
    """Token bucket. Polymarket's positions endpoint caps at 150 req/10s."""

    def __init__(self, rate_per_sec: float, burst: Optional[float] = None):
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(rate_per_sec, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, cost: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = deficit / self.rate if self.rate > 0 else 0.05
            time.sleep(min(wait, 2.0))


class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


def request_json(
    url: str,
    params: Optional[dict[str, Any]] = None,
    limiter: Optional[RateLimiter] = None,
    timeout: float = 25.0,
    retries: int = 4,
) -> Any:
    """GET JSON with exponential backoff and jitter.

    Retries 429 and 5xx. Raises ApiError on non-retryable status so schema or
    auth problems surface loudly rather than being read as an empty result -
    an empty market list must never be mistaken for "no opportunities today".
    """
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(clean)}"

    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        if limiter:
            limiter.acquire()
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT, "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                sleep = (2 ** attempt) + random.uniform(0, 0.5)
                log.warning("HTTP %s on %s, retry in %.1fs", e.code, url, sleep)
                time.sleep(sleep)
                last_exc = e
                continue
            raise ApiError(e.code, url, body) from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_exc = e
            if attempt < retries - 1:
                sleep = (2 ** attempt) + random.uniform(0, 0.5)
                log.warning("%s on %s, retry in %.1fs", type(e).__name__, url, sleep)
                time.sleep(sleep)
                continue
            raise
    raise last_exc if last_exc else RuntimeError(f"request failed: {url}")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json_field(value: Any) -> list:
    """Polymarket returns `outcomes` and `outcomePrices` as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []
