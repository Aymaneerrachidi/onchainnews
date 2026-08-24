from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit

import httpx


log = logging.getLogger("brief.http")


class SourceError(RuntimeError):
    pass


def _safe_http_error(exc: Exception) -> str:
    """Never leak query-string API keys through exception text or reports."""
    if isinstance(exc, httpx.HTTPStatusError):
        label = f"HTTP {exc.response.status_code}"
        # Surface a small allow-list of operational provider reasons without
        # ever copying arbitrary response text (which may echo credentials or
        # request parameters) into logs and public data-coverage notes.
        try:
            body = exc.response.json()
            message = str((body or {}).get("message") or (body or {}).get("error") or "").lower()
        except (ValueError, AttributeError):
            message = ""
        if "compute unit" in message and ("limit" in message or "quota" in message):
            return f"{label} (compute-unit quota exceeded)"
        if "rate limit" in message:
            return f"{label} (rate limit exceeded)"
        return label
    if isinstance(exc, httpx.RequestError):
        return exc.__class__.__name__
    return exc.__class__.__name__


@dataclass(slots=True)
class _Bucket:
    limit: int
    calls: deque[float]
    lock: asyncio.Lock
    last_call: float = 0.0


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    async def acquire(
        self,
        family: str,
        limit_per_minute: int,
        min_interval_seconds: float = 0.0,
    ) -> None:
        bucket = self._buckets.setdefault(
            family, _Bucket(limit_per_minute, deque(), asyncio.Lock())
        )
        async with bucket.lock:
            while True:
                now = time.monotonic()
                while bucket.calls and now - bucket.calls[0] >= 60:
                    bucket.calls.popleft()
                if len(bucket.calls) < bucket.limit:
                    interval_wait = max(
                        0.0,
                        float(min_interval_seconds) - (now - bucket.last_call),
                    )
                    if interval_wait > 0:
                        await asyncio.sleep(interval_wait)
                        continue
                    bucket.calls.append(now)
                    bucket.last_call = now
                    return
                await asyncio.sleep(max(0.01, 60 - (now - bucket.calls[0])))


class CachedHttpClient:
    def __init__(
        self,
        ledger: Any,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        fixture_path: str | Path | None = None,
        replay_date: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.limiter = RateLimiter()
        self.fixtures: dict[str, Any] | None = None
        self.replay_date = replay_date
        if fixture_path:
            self.fixtures = json.loads(Path(fixture_path).read_text(encoding="utf-8"))

    def _fixture(self, method: str, url: str) -> Any:
        if self.fixtures is None:
            return None
        path = urlsplit(url).path
        for key in (f"{method} {path}", path):
            if key in self.fixtures:
                return self.fixtures[key]
        raise SourceError(f"fixture has no response for {method} {path}")

    async def close(self) -> None:
        await self.client.aclose()

    async def get_json(
        self,
        url: str,
        *,
        family: str,
        limit: int,
        ttl: int,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        min_interval_seconds: float = 0.0,
    ) -> Any:
        if self.replay_date:
            replayed = self.ledger.replay_response(self.replay_date, "GET", url, params)
            if replayed is None:
                raise SourceError(f"archive has no response for GET {url} on {self.replay_date}")
            return replayed
        if self.fixtures is not None:
            return self._fixture("GET", url)
        cache_key = self.ledger.cache_key("GET", url, params, headers)
        cached = self.ledger.cache_get(cache_key, ttl)
        if cached is not None:
            return cached
        last_error = "unknown error"
        allow_stale = True
        for attempt in range(3):
            await self.limiter.acquire(family, limit, min_interval_seconds)
            started = time.perf_counter()
            try:
                response = await self.client.get(url, headers=headers, params=params)
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                log.info(
                    "api_call method=GET family=%s status=%s latency_ms=%s url=%s",
                    family,
                    response.status_code,
                    latency_ms,
                    url,
                )
                response_text = response.text
                self.ledger.archive_response(
                    method="GET", endpoint=url, request_params=params, request_body=None,
                    status=response.status_code, response_body=response_text,
                )
                if response.status_code == 429:
                    last_error = f"HTTP {response.status_code}"
                    # Repeated requests during a provider cooldown can extend
                    # the ban. Fail this lane immediately; its caller can open
                    # a circuit and mark the data unavailable.
                    allow_stale = False
                    break
                if response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                response.raise_for_status()
                payload = json.loads(response_text)
                self.ledger.cache_put(cache_key, payload)
                return payload
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = _safe_http_error(exc)
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                    and exc.response.status_code != 429
                ):
                    # A bad request, expired credential, or forbidden route is
                    # not a temporary outage. Returning an old successful page
                    # here made dead provider lanes look live and injected
                    # stale token rankings into the current recap.
                    allow_stale = False
                    break
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        stale = self.ledger.cache_get(cache_key, ttl=None) if allow_stale else None
        if stale is not None:
            log.warning("serving_stale_cache family=%s url=%s error=%s", family, url, last_error)
            return stale
        raise SourceError(f"{family} failed after 3 attempts: {last_error}")

    async def post_json(
        self,
        url: str,
        *,
        family: str,
        limit: int,
        ttl: int,
        json_body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if self.replay_date:
            replayed = self.ledger.replay_response(self.replay_date, "POST", url, params, json_body)
            if replayed is None:
                raise SourceError(f"archive has no response for POST {url} on {self.replay_date}")
            return replayed
        if self.fixtures is not None:
            return self._fixture("POST", url)
        cache_headers = {**(headers or {}), "body": json.dumps(json_body, sort_keys=True)}
        cache_key = self.ledger.cache_key("POST", url, params, cache_headers)
        cached = self.ledger.cache_get(cache_key, ttl)
        if cached is not None:
            return cached
        last_error = "unknown error"
        allow_stale = True
        for attempt in range(3):
            await self.limiter.acquire(family, limit)
            started = time.perf_counter()
            try:
                response = await self.client.post(
                    url, params=params, headers=headers, json=json_body
                )
                log.info(
                    "api_call method=POST family=%s status=%s latency_ms=%.1f url=%s",
                    family,
                    response.status_code,
                    (time.perf_counter() - started) * 1000,
                    url,
                )
                response_text = response.text
                self.ledger.archive_response(
                    method="POST", endpoint=url, request_params=params, request_body=json_body,
                    status=response.status_code, response_body=response_text,
                )
                if response.status_code == 429:
                    last_error = f"HTTP {response.status_code}"
                    allow_stale = False
                    break
                if response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                response.raise_for_status()
                payload = json.loads(response_text)
                if isinstance(payload, dict) and payload.get("error"):
                    error = payload.get("error") or {}
                    code = error.get("code")
                    last_error = f"RPC {code}: {error.get('message', 'error')}"
                    if code in {-32029, -32000} and attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    return payload
                self.ledger.cache_put(cache_key, payload)
                return payload
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = _safe_http_error(exc)
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                    and exc.response.status_code != 429
                ):
                    allow_stale = False
                    break
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        stale = self.ledger.cache_get(cache_key, ttl=None) if allow_stale else None
        if stale is not None:
            return stale
        raise SourceError(f"{family} failed after 3 attempts: {last_error}")
