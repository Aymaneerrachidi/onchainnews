from __future__ import annotations

import httpx
import pytest

from brief.sources.http import CachedHttpClient, SourceError


class CacheLedger:
    def __init__(self, stale):
        self.stale = stale

    @staticmethod
    def cache_key(*_args):
        return "key"

    def cache_get(self, _key, ttl):
        return None if ttl is not None else self.stale

    def cache_put(self, *_args):
        raise AssertionError("a failed response must not enter cache")

    def archive_response(self, **_kwargs):
        pass


@pytest.mark.asyncio
async def test_permanent_http_error_never_masquerades_as_current_stale_data():
    transport = httpx.MockTransport(lambda _request: httpx.Response(400, json={"error": "bad request"}))
    client = CachedHttpClient(CacheLedger({"old": True}), transport=transport)
    try:
        with pytest.raises(SourceError, match="HTTP 400"):
            await client.get_json("https://example.test/rank", family="rank", limit=60, ttl=1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_temporary_http_failure_can_degrade_to_stale_data():
    transport = httpx.MockTransport(lambda _request: httpx.Response(503, json={"error": "down"}))
    client = CachedHttpClient(CacheLedger({"old": True}), transport=transport)
    try:
        result = await client.get_json("https://example.test/rank", family="rank", limit=60, ttl=1)
    finally:
        await client.close()

    assert result == {"old": True}


@pytest.mark.asyncio
async def test_rate_limit_stops_after_one_request_and_does_not_serve_stale():
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "slow down"})

    client = CachedHttpClient(CacheLedger({"old": True}), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SourceError, match="HTTP 429"):
            await client.get_json("https://example.test/rank", family="rank", limit=60, ttl=1)
    finally:
        await client.close()

    assert calls == 1


@pytest.mark.asyncio
async def test_provider_quota_reason_is_reported_without_copying_arbitrary_body():
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        400,
        json={"success": False, "message": "Compute units usage limit exceeded"},
    ))
    client = CachedHttpClient(CacheLedger(None), transport=transport)
    try:
        with pytest.raises(SourceError, match="compute-unit quota exceeded"):
            await client.get_json("https://example.test/rank", family="rank", limit=60, ttl=1)
    finally:
        await client.close()
