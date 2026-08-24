from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from brief.models import TokenSnapshot, TransactionWindow, integer, number
from brief.sources.http import CachedHttpClient, SourceError


NETWORK_BY_CHAIN = {
    "solana": "solana",
    "ethereum": "eth",
    "bsc": "bsc",
    "base": "base",
}


class GeckoTerminalSource:
    """Keyless independent discovery and hourly peak verification."""

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
        self.partial_error: str | None = None
        self.trending_snapshots: list[TokenSnapshot] = []

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _snapshot(
        self,
        row: dict,
        *,
        chain: str,
        network: str,
        mint: str,
        token_meta: dict,
    ) -> TokenSnapshot | None:
        """Translate a ranked pool into a usable fallback market snapshot.

        Dexscreener can know the mint while missing the active migrated pool.
        In that case address-only discovery resolves to the dead bonding-curve
        pair and the live runner vanishes. Gecko's pool row is complete enough
        to enter the normal hard, safety and KOL gates directly.
        """
        attributes = row.get("attributes") or {}
        relationships = row.get("relationships") or {}
        pool_address = str(attributes.get("address") or "")
        if not pool_address:
            row_id = str(row.get("id") or "")
            prefix = f"{network}_"
            pool_address = row_id[len(prefix):] if row_id.startswith(prefix) else ""
        if not pool_address:
            return None
        transactions = attributes.get("transactions") or {}

        def tx_window(name: str) -> TransactionWindow:
            values = transactions.get(name) or {}
            return TransactionWindow(
                buys=integer(values.get("buys")),
                sells=integer(values.get("sells")),
                makers=integer(values.get("buyers")) + integer(values.get("sellers")) or None,
            )

        volume = attributes.get("volume_usd") or {}
        changes = attributes.get("price_change_percentage") or {}
        dex_id = str((((relationships.get("dex") or {}).get("data") or {}).get("id")) or "geckoterminal")
        market_cap = number(attributes.get("market_cap_usd"))
        fdv = number(attributes.get("fdv_usd"))
        return TokenSnapshot(
            mint=mint,
            symbol=str(token_meta.get("symbol") or "?").upper(),
            name=str(token_meta.get("name") or token_meta.get("symbol") or mint[:8]),
            chain_id=chain,
            pair_address=pool_address,
            url=f"https://www.geckoterminal.com/{network}/pools/{pool_address}",
            price_usd=number(attributes.get("base_token_price_usd")),
            # Gecko does not publish circulating market cap for every new
            # launch. FDV is retained as a clearly tagged fallback; KOL,
            # liquidity, holder and contract gates still decide publication.
            market_cap=market_cap or fdv,
            liquidity_usd=number(attributes.get("reserve_in_usd")),
            volume_24h=number(volume.get("h24")),
            volume_6h=number(volume.get("h6")),
            volume_1h=number(volume.get("h1")),
            price_change_24h=number(changes.get("h24")),
            price_change_6h=number(changes.get("h6")),
            price_change_1h=number(changes.get("h1")),
            price_change_5m=number(changes.get("m5")),
            pair_created_at=self._timestamp(attributes.get("pool_created_at")),
            dex_id=dex_id.lower(),
            txns_24h=tx_window("h24"),
            txns_6h=tx_window("h6"),
            txns_1h=tx_window("h1"),
            intraday_known=True,
            volume_by_dex={dex_id.lower(): number(volume.get("h24"))},
            raw={
                "geckoterminal": row,
                "marketCapIsFdv": not bool(market_cap) and bool(fdv),
            },
        )

    async def trending_addresses(
        self,
        chains: Iterable[str],
        *,
        pages: int = 3,
    ) -> list[tuple[str, str]]:
        """Return exact base-token addresses from independent trending pools.

        This lane caught CYBERLEEK when it had +80% / $34M volume but was below
        every sampled GMGN/Dexscreener discovery page. Addresses are hydrated by
        Dexscreener afterwards, so GeckoTerminal is discovery—not a safety pass.
        """
        found: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        seen_pools: set[tuple[str, str]] = set()
        supported = [
            (chain, NETWORK_BY_CHAIN[chain])
            for chain in dict.fromkeys(str(value).lower() for value in chains)
            if chain in NETWORK_BY_CHAIN
        ]
        self.partial_error = None
        self.trending_snapshots = []
        # Breadth before depth: page 1 from every chain is more valuable than
        # three Solana pages followed by a rate limit before BNB/Ethereum.
        for page in range(1, max(1, pages) + 1):
            for chain, network in supported:
                try:
                    payload = await self.http.get_json(
                        f"{self.base_url}/networks/{network}/trending_pools",
                        family="geckoterminal-trending",
                        limit=self.requests_per_minute,
                        ttl=min(self.ttl, 600),
                        headers={"User-Agent": "onchain-rundown/1.0"},
                        params={"page": page, "include": "base_token"},
                        min_interval_seconds=self.request_interval_seconds,
                    )
                except SourceError as exc:
                    self.partial_error = str(exc)
                    # Preserve every exact contract already found. Repeated
                    # calls after a 429 can extend the provider cooldown.
                    return found
                rows = (payload or {}).get("data") or []
                if not rows:
                    continue
                included = {
                    str(item.get("id") or ""): item.get("attributes") or {}
                    for item in ((payload or {}).get("included") or [])
                    if isinstance(item, dict) and item.get("type") == "token"
                }
                prefix = f"{network}_"
                for row in rows:
                    token_id = str(
                        (((row.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")
                        or ""
                    )
                    mint = token_id[len(prefix):] if token_id.startswith(prefix) else ""
                    key = (chain, mint)
                    if mint and key not in seen:
                        seen.add(key)
                        found.append(key)
                    snapshot = self._snapshot(
                        row,
                        chain=chain,
                        network=network,
                        mint=mint,
                        token_meta=included.get(token_id, {}),
                    ) if mint else None
                    pool_key = (chain, snapshot.pair_address) if snapshot else None
                    if snapshot and pool_key not in seen_pools:
                        seen_pools.add(pool_key)
                        self.trending_snapshots.append(snapshot)
        return found

    async def peak_market_cap(self, token: TokenSnapshot, hours: int = 30) -> float | None:
        if not token.pair_address or token.price_usd <= 0 or token.market_cap <= 0:
            return None
        network = NETWORK_BY_CHAIN.get(token.chain_id.lower())
        if not network:
            return None
        payload = await self.http.get_json(
            f"{self.base_url}/networks/{network}/pools/{token.pair_address}/ohlcv/hour",
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
            min_interval_seconds=self.request_interval_seconds,
        )
        candles = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        highs = [number(row[2]) for row in candles if isinstance(row, list) and len(row) >= 3]
        high = max(highs, default=0.0)
        if high <= 0:
            return None
        circulating_supply = token.market_cap / token.price_usd
        return high * circulating_supply
