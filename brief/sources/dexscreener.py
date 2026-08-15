from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable

from brief.models import CTORecord, MetaSnapshot, TokenSnapshot, TransactionWindow, integer, number
from brief.sources.http import CachedHttpClient


def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _dt_ms(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def parse_pair(pair: dict[str, Any]) -> TokenSnapshot | None:
    base = pair.get("baseToken") or {}
    mint = str(base.get("address") or pair.get("tokenAddress") or "")
    if not mint:
        return None
    txns = pair.get("txns") or {}

    def window(name: str) -> TransactionWindow:
        values = txns.get(name) or {}
        makers = values.get("makers") or values.get("uniqueMakers")
        return TransactionWindow(integer(values.get("buys")), integer(values.get("sells")), integer(makers) if makers is not None else None)

    info = pair.get("info") or {}
    return TokenSnapshot(
        mint=mint,
        symbol=str(base.get("symbol") or "?").upper(),
        name=str(base.get("name") or base.get("symbol") or mint[:8]),
        chain_id=str(pair.get("chainId") or ""),
        pair_address=str(pair.get("pairAddress") or ""),
        url=str(pair.get("url") or f"https://dexscreener.com/solana/{pair.get('pairAddress', '')}"),
        price_usd=number(pair.get("priceUsd")),
        # FDV is not accepted as a substitute: an unknown market cap cannot
        # prove that the operator's hard floor has been cleared.
        market_cap=number(pair.get("marketCap")),
        liquidity_usd=number((pair.get("liquidity") or {}).get("usd")),
        volume_24h=number((pair.get("volume") or {}).get("h24")),
        volume_6h=number((pair.get("volume") or {}).get("h6")),
        price_change_24h=number((pair.get("priceChange") or {}).get("h24")),
        price_change_6h=number((pair.get("priceChange") or {}).get("h6")),
        price_change_1h=number((pair.get("priceChange") or {}).get("h1")),
        price_change_5m=number((pair.get("priceChange") or {}).get("m5")),
        pair_created_at=_dt_ms(pair.get("pairCreatedAt")),
        dex_id=str(pair.get("dexId") or "unknown").lower(),
        volume_by_dex={str(pair.get("dexId") or "unknown").lower(): number((pair.get("volume") or {}).get("h24"))},
        txns_1h=window("h1"),
        txns_6h=window("h6"),
        txns_24h=window("h24"),
        volume_1h=number((pair.get("volume") or {}).get("h1")),
        socials=[s for s in info.get("socials", []) if isinstance(s, dict)],
        active_boosts=integer((pair.get("boosts") or {}).get("active")),
        raw=pair,
    )


def merge_token_snapshots(tokens: list[TokenSnapshot]) -> list[TokenSnapshot]:
    """Choose the deepest market per mint while preserving cross-feed context."""
    grouped: dict[str, list[TokenSnapshot]] = {}
    for token in tokens:
        grouped.setdefault(token.mint, []).append(token)
    best: dict[str, TokenSnapshot] = {}
    for mint, group in grouped.items():
        chosen = max(group, key=lambda token: token.liquidity_usd)
        chosen.meta_slugs = set().union(*(token.meta_slugs for token in group))
        chosen.from_profile = any(token.from_profile for token in group)
        volume_by_dex: dict[str, float] = {}
        seen_pairs: set[str] = set()
        for token in group:
            pair_key = token.pair_address or token.url
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            volume_by_dex[token.dex_id] = volume_by_dex.get(token.dex_id, 0) + token.volume_24h
        chosen.volume_by_dex = volume_by_dex
        best[mint] = chosen
    return list(best.values())


class DexscreenerSource:
    def __init__(
        self,
        http: CachedHttpClient,
        base_url: str,
        discovery_ttl: int,
        pairs_ttl: int,
        chains: tuple[str, ...] = ("solana",),
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.discovery_ttl = discovery_ttl
        self.pairs_ttl = pairs_ttl
        # Discovery feeds are multi-chain; the safety layer below is not.
        # Keeping this a list makes adding a chain a configuration change.
        self.chains = tuple(chain.lower() for chain in chains) or ("solana",)

    @property
    def primary_chain(self) -> str:
        return self.chains[0]

    async def trending_metas(self) -> list[MetaSnapshot]:
        payload = await self.http.get_json(
            f"{self.base_url}/metas/trending/v1", family="dex-discovery", limit=60, ttl=self.discovery_ttl
        )
        result: list[MetaSnapshot] = []
        for item in _items(payload, "metas", "data"):
            changes = item.get("marketCapChange") or item.get("marketCapDelta") or {}
            slug = str(item.get("slug") or item.get("id") or item.get("name") or "")
            if slug:
                result.append(MetaSnapshot(
                    slug=slug,
                    name=str(item.get("name") or slug.replace("-", " ").title()),
                    market_cap=number(item.get("marketCap")),
                    liquidity=number(item.get("liquidity")),
                    volume_24h=number(item.get("volume") or item.get("volume24h")),
                    token_count=integer(item.get("tokenCount")),
                    change_24h=number(changes.get("h24") if isinstance(changes, dict) else changes),
                    change_6h=number(changes.get("h6") if isinstance(changes, dict) else 0),
                ))
        return result

    async def meta_pairs(self, slug: str) -> list[TokenSnapshot]:
        payload = await self.http.get_json(
            f"{self.base_url}/metas/meta/v1/{slug}", family="dex-discovery", limit=60, ttl=self.discovery_ttl
        )
        pairs = _items(payload, "pairs", "data")
        tokens = [parsed for pair in pairs if (parsed := parse_pair(pair))]
        for token in tokens:
            token.meta_slugs.add(slug)
        return tokens

    async def ctos(self) -> list[CTORecord]:
        payload = await self.http.get_json(
            f"{self.base_url}/community-takeovers/latest/v1", family="dex-discovery", limit=60, ttl=self.discovery_ttl
        )
        result: list[CTORecord] = []
        for item in _items(payload, "takeovers", "data"):
            mint = str(item.get("tokenAddress") or item.get("address") or item.get("mint") or "")
            if mint:
                result.append(CTORecord(mint=mint, claim_date=_dt_ms(item.get("claimDate")), raw=item))
        return result

    def _in_scope(self, item: dict[str, Any]) -> bool:
        return str(item.get("chainId") or "").lower() in self.chains

    async def profiles(self) -> list[tuple[str, str]]:
        payload = await self.http.get_json(
            f"{self.base_url}/token-profiles/latest/v1", family="dex-discovery", limit=60, ttl=self.discovery_ttl
        )
        return [
            (str(item.get("chainId")).lower(), str(item.get("tokenAddress")))
            for item in _items(payload, "profiles", "data")
            if self._in_scope(item) and item.get("tokenAddress")
        ]

    async def top_boosts(self) -> set[str]:
        payload = await self.http.get_json(
            f"{self.base_url}/token-boosts/top/v1", family="dex-discovery", limit=60, ttl=self.discovery_ttl
        )
        return {
            str(item.get("tokenAddress"))
            for item in _items(payload, "boosts", "data")
            if self._in_scope(item) and item.get("tokenAddress")
        }

    async def token_pairs(
        self, addresses: Iterable[str] | Iterable[tuple[str, str]], chain: str | None = None
    ) -> list[TokenSnapshot]:
        by_chain: dict[str, list[str]] = {}
        for entry in addresses:
            if isinstance(entry, tuple):
                entry_chain, address = entry
            else:
                entry_chain, address = chain or self.primary_chain, entry
            if not address:
                continue
            bucket = by_chain.setdefault(str(entry_chain).lower(), [])
            if address not in bucket:
                bucket.append(address)

        async def fetch(chain_id: str, chunk: list[str]) -> list[TokenSnapshot]:
            payload = await self.http.get_json(
                f"{self.base_url}/tokens/v1/{chain_id}/{','.join(chunk)}",
                family="dex-pairs", limit=300, ttl=self.pairs_ttl,
            )
            return [parsed for pair in _items(payload, "pairs", "data") if (parsed := parse_pair(pair))]

        jobs = [
            fetch(chain_id, unique[index:index + 30])
            for chain_id, unique in by_chain.items()
            for index in range(0, len(unique), 30)
        ]
        nested = await asyncio.gather(*jobs) if jobs else []
        return [token for group in nested for token in group]

    async def universe(self) -> tuple[list[MetaSnapshot], list[TokenSnapshot], dict[str, CTORecord], list[str]]:
        names = ("trending metas", "CTO feed", "token profiles", "paid boosts")
        discovered = await asyncio.gather(
            self.trending_metas(), self.ctos(), self.profiles(), self.top_boosts(),
            return_exceptions=True,
        )
        degraded = [name for name, value in zip(names, discovered) if isinstance(value, Exception)]
        metas = [] if isinstance(discovered[0], Exception) else discovered[0]
        ctos = [] if isinstance(discovered[1], Exception) else discovered[1]
        profiles = [] if isinstance(discovered[2], Exception) else discovered[2]
        boosts = set() if isinstance(discovered[3], Exception) else discovered[3]
        meta_results = await asyncio.gather(*(self.meta_pairs(meta.slug) for meta in metas), return_exceptions=True)
        meta_tokens: list[TokenSnapshot] = []
        for meta, result in zip(metas, meta_results):
            if isinstance(result, Exception):
                degraded.append(f"meta:{meta.slug}")
                continue
            for token in result:
                token.meta_slugs.add(meta.slug)
                meta_tokens.append(token)
        extra_addresses: list[tuple[str, str]] = [*profiles]
        for cto in ctos:
            chain_id = str((cto.raw or {}).get("chainId") or self.primary_chain).lower()
            if chain_id in self.chains:
                extra_addresses.append((chain_id, cto.mint))
        try:
            extra_tokens = await self.token_pairs(extra_addresses)
        except Exception:
            degraded.append("token pair lookup")
            extra_tokens = []
        profile_set = {address for _, address in profiles}
        for token in extra_tokens:
            token.from_profile = token.mint in profile_set
        all_tokens = meta_tokens + extra_tokens
        for token in all_tokens:
            if token.mint in boosts:
                token.active_boosts = max(1, token.active_boosts)
        return metas, merge_token_snapshots(all_tokens), {record.mint: record for record in ctos}, degraded
