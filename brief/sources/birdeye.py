from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from brief.models import number
from brief.sources.http import CachedHttpClient


log = logging.getLogger("brief.birdeye")

# Quote assets and majors are always at the top of a volume ranking and can
# never be a pick. Skipping them keeps the expensive safety path for names that
# could actually reach the brief.
MAJORS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",    # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",   # jitoSOL
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",   # JLP
    # EVM quote assets and wrapped natives, which top every volume ranking.
    "0xdac17f958d2ee523a2206206994597c13d831ec7",     # USDT  ethereum
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",     # USDC  ethereum
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",     # WETH  ethereum
    "0x55d398326f99059ff775485246999027b3197955",     # USDT  bsc
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",     # WBNB  bsc
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",     # USDC  base
    "0x4200000000000000000000000000000000000006",     # WETH  base
}


class BirdeyeSource:
    """Ranked discovery across the whole Solana token universe.

    Dexscreener's public feeds surface only trending metas, takeovers, profiles
    and paid boosts — roughly four hundred tokens. That is a keyhole view when
    the question is "what is strongest today". Birdeye ranks every token with
    real liquidity by 24h volume, which is the pool the movers track needs.

    Only `sort_by=volume_24h_usd` is used. Sorting by market cap returns tokens with
    fabricated supply (observed at $108 trillion), and `v24hChangePercent` is a
    change in volume rather than price.
    """

    def __init__(
        self,
        http: CachedHttpClient,
        base_url: str,
        api_key: str | None,
        ttl: int,
        *,
        page_size: int = 50,
        requests_per_minute: int = 50,
        request_interval: float = 1.1,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.ttl = ttl
        self.page_size = max(1, min(100, page_size))
        self.chain = "solana"
        self.requests_per_minute = requests_per_minute
        self.request_interval = max(0.0, request_interval)
        # One source instance is reused while discovery walks every configured
        # chain.  Keep the pacing state on that instance so the first page of a
        # new chain cannot burst immediately after the last page of the prior
        # chain and exhaust Birdeye's free-tier lane.
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        # The key travels as a header, so it never reaches the archived request
        # parameters or the request log line.
        return {"X-API-KEY": str(self.api_key), "x-chain": self.chain, "accept": "application/json"}

    async def _wait_for_request_slot(self) -> None:
        """Space all Birdeye requests, including transitions between chains."""
        async with self._request_lock:
            if self.request_interval and self._last_request_at:
                elapsed = time.monotonic() - self._last_request_at
                remaining = self.request_interval - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _tokens(payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            for key in ("tokens", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def top_by_volume(
        self, max_tokens: int, min_liquidity: float, min_market_cap: float
    ) -> list[str]:
        """Addresses of the highest-volume tokens that clear the report's floors.

        Market cap and liquidity come back with the ranking, so both floors are
        applied here rather than after a round of pair lookups.
        """
        if not self.configured:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for offset in range(0, max(0, max_tokens), self.page_size):
            # The per-minute limiter permits short bursts. Explicit spacing is
            # still required, and must cover chain boundaries as well as pages.
            await self._wait_for_request_slot()
            try:
                payload = await self.http.get_json(
                    f"{self.base_url}/defi/v3/token/list",
                    family="birdeye",
                    limit=self.requests_per_minute,
                    ttl=self.ttl,
                    headers=self._headers(),
                    params={
                        "sort_by": "volume_24h_usd",
                        "sort_type": "desc",
                        "offset": offset,
                        "limit": self.page_size,
                        "min_liquidity": int(min_liquidity),
                        "min_market_cap": int(min_market_cap),
                    },
                )
            except Exception as exc:
                # Keep the pages already collected rather than losing discovery.
                log.warning("birdeye_page_failed offset=%s error=%s", offset, exc)
                break
            page = self._tokens(payload)
            if not page:
                break
            seen_before = len(seen)
            for item in page:
                address = str(item.get("address") or "")
                if not address or address in seen or address.lower() in MAJORS or address in MAJORS:
                    continue
                seen.add(address)
                if number(item.get("liquidity")) < min_liquidity:
                    continue
                if number(item.get("market_cap") if item.get("market_cap") is not None else item.get("mc")) < min_market_cap:
                    continue
                found.append(address)
            # When the provider rejects an offset the cache can legally return
            # a stale page. Do not walk the same page through every remaining
            # offset and pretend it expanded discovery.
            if len(seen) == seen_before:
                break
            if len(page) < self.page_size:
                break
        log.info("birdeye_discovery chain=%s addresses=%s scanned=%s", self.chain, len(found), len(seen))
        return found
