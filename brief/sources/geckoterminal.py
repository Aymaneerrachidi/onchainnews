from __future__ import annotations

import asyncio
import time

from brief.models import TokenSnapshot, number
from brief.sources.http import CachedHttpClient


class GeckoTerminalSource:
    """Keyless hourly candles used only to verify a faded token's real peak."""

    def __init__(
        self,
        http: CachedHttpClient,
        base_url: str = "https://api.geckoterminal.com/api/v2",
        *,
        ttl: int = 3600,
        requests_per_minute: int = 25,
        request_interval_seconds: float = 2.5,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl
        self.requests_per_minute = requests_per_minute
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self._pace_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def _pace(self) -> None:
        async with self._pace_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.request_interval_seconds:
                await asyncio.sleep(self.request_interval_seconds - elapsed)
            self._last_request_at = time.monotonic()

    async def peak_market_cap(self, token: TokenSnapshot, hours: int = 30) -> float | None:
        if not token.pair_address or token.price_usd <= 0 or token.market_cap <= 0:
            return None
        await self._pace()
        payload = await self.http.get_json(
            f"{self.base_url}/networks/solana/pools/{token.pair_address}/ohlcv/hour",
            family="geckoterminal-ohlcv",
            limit=self.requests_per_minute,
            ttl=self.ttl,
            headers={"User-Agent": "onchain-rundown/1.0"},
            params={
                "aggregate": 1,
                "limit": max(1, min(hours, 48)),
                "currency": "usd",
                "token": "base",
            },
        )
        candles = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        highs = [number(row[2]) for row in candles if isinstance(row, list) and len(row) >= 3]
        high = max(highs, default=0.0)
        if high <= 0:
            return None
        circulating_supply = token.market_cap / token.price_usd
        return high * circulating_supply
