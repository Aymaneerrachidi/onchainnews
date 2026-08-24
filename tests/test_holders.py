from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brief.holders import analyze_changes, build_snapshot, collapse_clusters, gini
from brief.ledger import Ledger
from brief.models import HolderBalance, TokenSnapshot, WalletTrace
from brief.sources.helius import HeliusSource, parse_acquisition, parse_wallet_trace
from brief.sources.http import SourceError


UTC = timezone.utc
NOW = datetime(2026, 8, 6, 6, 45, tzinfo=UTC)


class FakeHttp:
    async def post_json(self, url, **kwargs):
        body = kwargs["json_body"]
        assert body["method"] == "getTokenAccounts"
        cursor = body["params"].get("cursor")
        if not cursor:
            return {"result": {"cursor": "page2", "token_accounts": [
                {"address": "A1", "owner": "OWNER1", "amount": 10},
                {"address": "LP", "owner": "POOL", "amount": 1000},
            ]}}
        return {"result": {"token_accounts": [
            {"address": "A2", "owner": "OWNER1", "amount": 5},
            {"address": "A3", "owner": "OWNER2", "amount": 7},
        ]}}


@pytest.mark.asyncio
async def test_helius_holder_pagination_aggregation_and_exclusions():
    source = HeliusSource(
        FakeHttp(), "https://helius.test", "key", 60,
        holder_page_limit=2, max_holder_pages=5,
    )
    balances, excluded = await source.token_holders(
        "MINT", excluded_accounts={"LP"}, excluded_owners={"CEX"}
    )
    assert [(item.owner, item.amount) for item in balances] == [("OWNER1", 15), ("OWNER2", 7)]
    assert excluded == 1


@pytest.mark.asyncio
async def test_helius_opens_run_circuit_after_first_rate_limit():
    class LimitedHttp:
        def __init__(self):
            self.calls = 0

        async def post_json(self, *_args, **_kwargs):
            self.calls += 1
            raise SourceError("helius failed after 1 attempt: HTTP 429")

    http = LimitedHttp()
    source = HeliusSource(http, "https://helius.test", "key", 60)
    with pytest.raises(SourceError, match="HTTP 429"):
        await source.enrich("FIRST")
    with pytest.raises(SourceError, match="circuit open"):
        await source.enrich("SECOND")
    assert http.calls == 1


def test_gini_and_snapshot_concentration():
    assert gini([1, 1, 1]) == pytest.approx(0)
    snapshot = build_snapshot("MINT", NOW, [HolderBalance(f"W{i}", 1) for i in range(100)])
    assert snapshot.holder_count == 100
    assert snapshot.top10_pct == pytest.approx(10)
    assert snapshot.top50_pct == pytest.approx(50)


def test_wallet_funder_and_first_acquisition_parsing():
    transaction = {
        "blockTime": int(NOW.timestamp()),
        "transaction": {"message": {"accountKeys": ["FUNDER", "WALLET"]}},
        "meta": {
            "preBalances": [2_000_000, 0],
            "postBalances": [990_000, 1_000_000],
            "preTokenBalances": [{"mint": "MINT", "owner": "WALLET", "uiTokenAmount": {"amount": "0"}}],
            "postTokenBalances": [{"mint": "MINT", "owner": "WALLET", "uiTokenAmount": {"amount": "500"}}],
        },
    }
    wallet = parse_wallet_trace("WALLET", [transaction])
    acquisition = parse_acquisition("MINT", "WALLET", [transaction])
    assert wallet.first_funder == "FUNDER"
    assert wallet.wallet_created_at == NOW
    assert acquisition.initial_amount == 500
    assert acquisition.first_acquired_at == NOW


def _token() -> TokenSnapshot:
    return TokenSnapshot(
        mint="MINT", symbol="TEST", name="Test", chain_id="solana", pair_address="PAIR",
        url="https://dexscreener.test/PAIR", price_usd=1.2, market_cap=500_000,
        liquidity_usd=100_000, volume_24h=200_000, volume_6h=50_000,
        price_change_24h=20, price_change_6h=5, pair_created_at=NOW - timedelta(days=2),
    )


def test_two_day_report_leads_with_divergence_and_collapses_clusters(tmp_path, settings):
    ledger = Ledger(tmp_path / "holders.db")
    try:
        previous_balances = [HolderBalance(f"W{i}", 1) for i in range(110)]
        previous = build_snapshot("MINT", NOW - timedelta(days=1), previous_balances)
        ledger.record_holder_snapshot(
            previous, price_usd=1.0, market_cap=400_000,
            pair_created_at=NOW - timedelta(days=2),
        )
        current_balances = [HolderBalance(f"W{i}", 1) for i in range(100)]
        current = build_snapshot("MINT", NOW, current_balances)
        ledger.record_holder_snapshot(
            current, price_usd=1.2, market_cap=500_000,
            pair_created_at=NOW - timedelta(days=2),
        )
        traces = {
            f"W{i}": WalletTrace(
                f"W{i}", "SHARED" if i < 40 else f"FUNDER{i}",
                NOW - timedelta(days=3), NOW - timedelta(days=3),
            )
            for i in range(100)
        }
        cluster = collapse_clusters(current_balances, traces, 100)
        ledger.record_cluster_snapshot("MINT", NOW, cluster.effective_top10_pct, cluster.cluster_count, cluster.coverage)
        finding = analyze_changes(_token(), current, ledger, traces, {}, 100, settings, NOW)
        assert finding is not None
        assert finding.priority == 0
        assert "distribution into strength" in finding.details
        assert any("effective top10 49.0%" in detail for detail in finding.details)
        assert any("91% of prior-day holders" in detail for detail in finding.details)
        assert finding.bubblemap_url.endswith("/MINT")
    finally:
        ledger.close()


def test_unchanged_token_does_not_appear(tmp_path, settings):
    ledger = Ledger(tmp_path / "unchanged.db")
    try:
        balances = [HolderBalance(f"W{i}", 1) for i in range(100)]
        previous = build_snapshot("MINT", NOW - timedelta(days=1), balances)
        ledger.record_holder_snapshot(
            previous, price_usd=1.2, market_cap=500_000,
            pair_created_at=NOW - timedelta(days=2),
        )
        current = build_snapshot("MINT", NOW, balances)
        ledger.record_holder_snapshot(
            current, price_usd=1.2, market_cap=500_000,
            pair_created_at=NOW - timedelta(days=2),
        )
        assert analyze_changes(_token(), current, ledger, {}, {}, 0, settings, NOW) is None
    finally:
        ledger.close()
