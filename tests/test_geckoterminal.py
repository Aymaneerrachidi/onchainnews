from __future__ import annotations

import pytest

from brief.sources.http import SourceError
from brief.sources.geckoterminal import GeckoTerminalSource


class FakeHttp:
    async def get_json(self, url, **kwargs):
        assert kwargs["min_interval_seconds"] > 0
        network = url.split("/networks/", 1)[1].split("/", 1)[0]
        page = int(kwargs["params"]["page"])
        if page > 1:
            return {"data": []}
        address = "A" * 32 if network == "solana" else "0x" + "a" * 40
        return {"data": [{
            "id": f"{network}_POOL",
            "attributes": {
                "address": "POOL",
                "pool_created_at": "2026-08-23T13:33:46Z",
                "fdv_usd": "400000",
                "base_token_price_usd": "0.0004",
                "reserve_in_usd": "58000",
                "volume_usd": {"h24": "4800000", "h6": "770000", "h1": "83000"},
                "price_change_percentage": {"h24": "800", "h6": "-38", "h1": "-7"},
                "transactions": {
                    "h24": {"buys": 54000, "sells": 31000, "buyers": 35000, "sellers": 18000},
                    "h6": {"buys": 12000, "sells": 6400},
                    "h1": {"buys": 1700, "sells": 900},
                },
            },
            "relationships": {
                "base_token": {"data": {"id": f"{network}_{address}"}},
                "dex": {"data": {"id": "pumpswap"}},
            },
        }], "included": [{
            "id": f"{network}_{address}",
            "type": "token",
            "attributes": {"symbol": "CYBERCAT", "name": "Cybercat"},
        }]}


@pytest.mark.asyncio
async def test_trending_discovery_preserves_chain_and_exact_contract():
    source = GeckoTerminalSource(FakeHttp(), request_interval_seconds=0.01)

    rows = await source.trending_addresses(["solana", "base", "robinhood"], pages=3)

    assert rows == [
        ("solana", "A" * 32),
        ("base", "0x" + "a" * 40),
    ]
    assert len(source.trending_snapshots) == 2
    snapshot = source.trending_snapshots[0]
    assert snapshot.symbol == "CYBERCAT"
    assert snapshot.market_cap == 400_000
    assert snapshot.liquidity_usd == 58_000
    assert snapshot.txns_24h.total == 85_000
    assert snapshot.raw["marketCapIsFdv"] is True


@pytest.mark.asyncio
async def test_trending_discovery_keeps_partial_contracts_after_rate_limit():
    class LimitedHttp:
        async def get_json(self, url, **_kwargs):
            if "/base/" in url:
                raise SourceError("geckoterminal request failed: HTTP 429")
            return {"data": [{
                "relationships": {
                    "base_token": {"data": {"id": f"solana_{'A' * 32}"}},
                },
            }]}

    source = GeckoTerminalSource(LimitedHttp(), request_interval_seconds=0)

    rows = await source.trending_addresses(["solana", "base"], pages=3)

    assert rows == [("solana", "A" * 32)]
    assert "HTTP 429" in (source.partial_error or "")
