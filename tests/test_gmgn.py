from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brief.sources.gmgn import GmgnSource, evidence_from_rank, parse_rank_item


MINT = "A" * 32
EVM_MINT = "0x" + "a" * 40


def test_gmgn_rank_parser_preserves_market_and_risk_provenance():
    row = {
        "address": MINT,
        "symbol": "RUN",
        "name": "Runner",
        "market_cap": "800000",
        "liquidity": "120000",
        "volume": "4000000",
        "buys": 4000,
        "sells": 3600,
        "creation_timestamp": 1_776_000_000,
        "history_highest_market_cap": "1400000",
        "top_10_holder_rate": "0.17",
        "is_wash_trading": False,
        "bundler_rate": "0.04",
        "dev_team_hold_rate": "0.07",
        "creator_token_status": "creator_close",
        "entrapment_ratio": "0.02",
        "top70_sniper_hold_rate": "0.01",
        "bot_degen_rate": "0.08",
        "bluechip_owner_percentage": "0.12",
        "burn_status": "burn",
    }

    token = parse_rank_item(row, origin="trending-24h")
    evidence = evidence_from_rank(row, "trending-24h")

    assert token is not None and token.market_cap == 800_000
    assert token.txns_24h.total == 7_600
    assert token.pair_created_at == datetime.fromtimestamp(1_776_000_000, tz=timezone.utc)
    assert evidence["athMarketCap"] == 1_400_000
    assert evidence["top10Pct"] == 17
    assert evidence["washTrading"] is False
    assert evidence["bundlerRate"] == 0.04
    assert evidence["devTeamHoldRate"] == 0.07
    assert evidence["creatorTokenStatus"] == "creator_close"
    assert evidence["entrapmentRatio"] == 0.02
    assert evidence["top70SniperHoldRate"] == 0.01
    assert evidence["botDegenRate"] == 0.08
    assert evidence["bluechipOwnerPct"] == 12
    assert evidence["burnStatus"] == "burn"


def test_hot_search_blocks_are_flattened_to_token_rows():
    payload = {
        "data": [
            {"name": "hot", "tokens": [{"address": MINT, "market_cap": 500_000}]},
            {"name": "empty", "tokens": []},
        ]
    }

    rows = GmgnSource._rank_rows(payload)

    assert rows[0]["address"] == MINT
    assert rows[1]["name"] == "empty"


def test_gmgn_renowned_trader_tape_preserves_outcomes_and_filters_wash_labels():
    payload = {"list": [
        {
            "address": "wallet-one", "name": "Alpha", "balance": 10,
            "amount_percentage": 0.012, "buy_volume_cur": 1000,
            "sell_volume_cur": 1800, "profit": 750,
            "realized_profit": 700, "unrealized_profit": 50,
            "profit_change": 0.75, "tags": ["kol"],
            "maker_token_tags": [], "start_holding_at": 1_776_000_000,
        },
        {
            "address": "wallet-two", "name": "Washer", "balance": 0,
            "profit": 9000, "realized_profit": 9000,
            "tags": ["kol", "wash_trader"], "maker_token_tags": ["bundler"],
        },
    ]}

    evidence = GmgnSource.trader_evidence(payload)

    assert evidence["renownedTraderCount"] == 2
    assert evidence["renownedTrustedCount"] == 1
    assert evidence["renownedProfitableCount"] == 1
    assert evidence["renownedHoldingCount"] == 1
    assert evidence["renownedRealizedProfitUsd"] == 700
    assert evidence["renownedTraders"][0]["holdingPct"] == 1.2
    assert evidence["renownedTraders"][1]["suspicious"] is True


def test_exact_token_info_recovers_wallet_counts_missing_from_discovery():
    evidence = GmgnSource.wallet_count_evidence({
        "holder_count": 55_421,
        "wallet_tags_stat": {"smart_wallets": 267, "renowned_wallets": 33},
        "stat": {"top_10_holder_rate": "0.1222"},
    })

    assert evidence["holders"] == 55_421
    assert evidence["kolCount"] == 33
    assert evidence["smartMoneyCount"] == 267
    assert evidence["top10Pct"] == pytest.approx(12.22)
    assert evidence["exactWalletCountsChecked"] is True


async def test_exact_wallet_fallback_updates_zero_kol_before_trader_gate(monkeypatch):
    from tests.test_tracks import _tape

    source = GmgnSource(chains=("solana",))
    source.executable = "gmgn-cli"
    source.api_key_present = True
    coin = _tape(
        "CYBERLEEK", mcap=16_000_000, vol24=35_000_000, vol6=8_000_000,
        liq=1_700_000, trades6=40_000, buys6=22_000,
    )
    evidence = {coin.token.mint: {"kolCount": 0, "smartMoneyCount": 0}}

    async def fake_safe(label: str, *args: str):
        return label, {
            "holder_count": 55_421,
            "wallet_tags_stat": {"smart_wallets": 267, "renowned_wallets": 33},
            "stat": {"top_10_holder_rate": "0.1222"},
        }, None

    monkeypatch.setattr(source, "_safe", fake_safe)
    status = await source.enrich_missing_wallet_counts([coin], evidence, limit=10)

    assert status.available is True
    assert evidence[coin.token.mint]["kolCount"] == 33
    assert evidence[coin.token.mint]["smartMoneyCount"] == 267


def test_gmgn_hourly_candles_reconstruct_the_trailing_day_peak():
    token = parse_rank_item({
        "address": MINT,
        "symbol": "RUN",
        "market_cap": 400_000,
        "price": 0.40,
    }, origin="test")
    assert token is not None
    payload = {"list": [
        {"time": 1_776_000_000_000, "open": "0.20", "close": "0.30", "high": "0.35", "low": "0.18", "volume": "100000"},
        {"time": 1_776_003_600_000, "open": "0.30", "close": "0.40", "high": "0.50", "low": "0.28", "volume": "150000"},
    ]}

    evidence = GmgnSource.kline_evidence(payload, token)

    assert evidence["kline24hChangePct"] == 100
    assert evidence["kline24hPeakFromOpenPct"] == 150
    assert evidence["kline24hHighLowMultiple"] == pytest.approx(0.50 / 0.18)
    assert evidence["kline24hPeakMarketCap"] == 500_000
    assert evidence["kline24hVolumeUsd"] == 250_000
    assert evidence["kline24hPeakAt"].startswith("2026-")


async def test_recovered_kline_pass_queries_only_exact_mint_recoveries(monkeypatch):
    recovered = parse_rank_item({
        "address": MINT, "symbol": "RECOVERED", "market_cap": 400_000, "price": 0.40,
    }, origin="test")
    broad = parse_rank_item({
        "address": "B" * 32, "symbol": "BROAD", "market_cap": 500_000, "price": 0.50,
    }, origin="test")
    assert recovered is not None and broad is not None
    evidence = {
        recovered.mint: {"kolCount": 12, "exactWalletCountsChecked": True},
        broad.mint: {"kolCount": 20},
    }
    source = GmgnSource(chains=("solana",))
    source.executable = "gmgn-cli"
    source.api_key_present = True
    called: list[str] = []

    async def fake_safe(label: str, *args: str):
        called.append(label)
        return label, {"list": [
            {"time": 1_776_000_000_000, "open": "0.20", "close": "0.40",
             "high": "0.50", "low": "0.18", "volume": "250000"},
        ]}, None

    monkeypatch.setattr(source, "_safe", fake_safe)
    status = await source.enrich_runner_klines(
        [recovered, broad], evidence, now=datetime.now(timezone.utc), exact_only=True,
    )

    assert status.available is True
    assert called == [f"kline:sol:{MINT}"]
    assert evidence[recovered.mint]["kline24hPeakMarketCap"] == 500_000
    assert "kline24hPeakMarketCap" not in evidence[broad.mint]


def test_gmgn_rank_parser_accepts_supported_evm_chains():
    token = parse_rank_item(
        {
            "address": EVM_MINT,
            "symbol": "BASECAT",
            "name": "Base Cat",
            "market_cap": 2_000_000,
            "history_highest_market_cap": 40_000_000,
        },
        origin="gmgn:trending-ath:base",
        chain="base",
    )

    assert token is not None
    assert token.chain_id == "base"
    assert token.url == f"https://dexscreener.com/base/{EVM_MINT}"


async def test_unconfigured_gmgn_degrades_without_dropping_run(monkeypatch):
    source = GmgnSource()
    monkeypatch.setattr(source, "executable", None)

    result = await source.discover(datetime.now(timezone.utc))

    assert result.tokens == []
    assert result.statuses and result.statuses[0].available is False
    assert "not installed" in result.statuses[0].detail


async def test_gmgn_discovery_has_a_server_filtered_organic_backbone(monkeypatch):
    source = GmgnSource(chains=("solana",))
    source.executable = "gmgn-cli"
    source.api_key_present = True
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_safe(label: str, *args: str):
        calls.append((label, args))
        return label, {"data": {"rank": []}}, None

    monkeypatch.setattr(source, "_safe", fake_safe)
    await source.discover(datetime.now(timezone.utc))

    organic = next(args for label, args in calls if label == "trending-organic:sol")
    kol_lane = next(args for label, args in calls if label == "trending-kol:sol")
    smart_lane = next(args for label, args in calls if label == "trending-smartmoney:sol")
    ath_lane = next(args for label, args in calls if label == "trending-ath:sol")
    joined = " ".join(organic)
    assert "--min-volume 250000" in joined
    assert "--min-liquidity 40000" in joined
    assert "--min-holder-count 1000" in joined
    assert "--min-swaps 1000" in joined
    assert "--max-top10-holder-rate 0.25" in joined
    assert "--filter not_wash_trading" in joined
    assert "--order-by renowned_count" in " ".join(kol_lane)
    assert "--min-renowned-count 1" in " ".join(kol_lane)
    assert "--order-by smart_degen_count" in " ".join(smart_lane)
    assert "--min-smart-degen-count 1" in " ".join(smart_lane)
    assert "--max-created 30h" in " ".join(ath_lane)
