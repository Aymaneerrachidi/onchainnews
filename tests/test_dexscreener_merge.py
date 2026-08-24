from datetime import datetime, timezone

from brief.models import TokenSnapshot, TransactionWindow
from brief.sources.dexscreener import merge_token_snapshots


def snapshot(**overrides):
    values = {
        "mint": "MINT",
        "symbol": "RUN",
        "name": "Runner",
        "chain_id": "solana",
        "pair_address": "PAIR",
        "url": "https://example.test",
        "price_usd": 1.0,
        "market_cap": 1_000_000,
        "liquidity_usd": 100_000,
        "volume_24h": 500_000,
        "volume_6h": 0,
        "price_change_24h": 0,
        "price_change_6h": 0,
        "pair_created_at": None,
    }
    values.update(overrides)
    return TokenSnapshot(**values)


def test_merge_prefers_exact_pair_identity_when_rank_row_has_deeper_liquidity():
    ranked = snapshot(
        liquidity_usd=200_000,
        intraday_known=False,
        pair_address="RANKED",
        pair_created_at=None,
        raw={"gmgn": {"address": "MINT"}},
    )
    created = datetime(2026, 8, 23, 20, 27, tzinfo=timezone.utc)
    exact = snapshot(
        liquidity_usd=150_000,
        pair_address="EXACT",
        pair_created_at=created,
        volume_6h=90_000,
        volume_1h=20_000,
        price_change_24h=80,
        price_change_6h=12,
        price_change_1h=3,
        intraday_known=True,
        txns_6h=TransactionWindow(700, 500),
        txns_24h=TransactionWindow(2_000, 1_500),
        socials=[{"type": "twitter", "url": "https://x.com/runner"}],
        raw={"pairAddress": "EXACT", "baseToken": {"address": "MINT"}},
    )

    merged = merge_token_snapshots([ranked, exact])[0]

    assert merged is exact
    assert merged.pair_address == "EXACT"
    assert merged.pair_created_at == created
    assert merged.price_change_24h == 80
    assert merged.volume_6h == 90_000
    assert merged.txns_6h.total == 1_200
    assert merged.intraday_known is True
    assert merged.socials == exact.socials
