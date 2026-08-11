from __future__ import annotations

import asyncio
import logging
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
}


class BirdeyeSource:
    """Ranked discovery across the whole Solana token universe.

    Dexscreener's public feeds surface only trending metas, takeovers, profiles
    and paid boosts — roughly four hundred tokens. That is a keyhole view when
    the question is "what is strongest today". Birdeye ranks every token with
    real liquidity by 24h volume, which is the pool the movers track needs.

    Only `sort_by=v24hUSD` is used. Sorting by market cap returns tokens with
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
        self.page_size = max(1, min(50, page_size))
        self.requests_per_minute = requests_per_minute
        self.request_interval = max(0.0, request_interval)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        # The key travels as a header, so it never reaches the archived request
        # parameters or the request log line.
        return {"X-API-KEY": str(self.api_key), "x-chain": "solana", "accept": "application/json"}

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
        for index, offset in enumerate(range(0, max(0, max_tokens), self.page_size)):
            if index and self.request_interval:
                # The free tier allows roughly one request per second. The
                # per-minute limiter permits a burst inside the minute, which
                # earns a run of 429s, so pages are spaced explicitly.
                await asyncio.sleep(self.request_interval)
            try:
                payload = await self.http.get_json(
                    f"{self.base_url}/defi/tokenlist",
                    family="birdeye",
                    limit=self.requests_per_minute,
                    ttl=self.ttl,
                    headers=self._headers(),
                    params={
                        "sort_by": "v24hUSD",
                        "sort_type": "desc",
                        "offset": offset,
                        "limit": self.page_size,
                        "min_liquidity": int(min_liquidity),
                    },
                )
            except Exception as exc:
                # Keep the pages already collected rather than losing discovery.
                log.warning("birdeye_page_failed offset=%s error=%s", offset, exc)
                break
            page = self._tokens(payload)
            if not page:
                break
            for item in page:
                address = str(item.get("address") or "")
                if not address or address in seen or address in MAJORS:
                    continue
                seen.add(address)
                if number(item.get("liquidity")) < min_liquidity:
                    continue
                if number(item.get("mc")) < min_market_cap:
                    continue
                found.append(address)
            if len(page) < self.page_size:
                break
        log.info("birdeye_discovery addresses=%s scanned=%s", len(found), len(seen))
        return found
