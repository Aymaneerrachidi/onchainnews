from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brief.models import Brief, Candidate, Enrichment, SafetyReport, Scorecard, Signals, TokenSnapshot
from brief.preflight import DeliveryPreflightError, audit_brief, audit_candidates, delivery_candidates


def candidate(symbol: str = "SAFE") -> Candidate:
    token = TokenSnapshot(
        mint=f"{symbol}mint1111111111111111111111111111111",
        symbol=symbol,
        name=symbol,
        chain_id="solana",
        pair_address=f"{symbol}pair111111111111111111111111111111",
        url="https://dexscreener.com/solana/pair",
        price_usd=0.001,
        market_cap=500_000,
        liquidity_usd=100_000,
        volume_24h=1_000_000,
        volume_6h=300_000,
        price_change_24h=200,
        price_change_6h=30,
        pair_created_at=datetime.now(timezone.utc),
    )
    result = Candidate(
        token=token,
        signals=Signals(2.0, 0.0, 0.55, 0.55, 0.2, None, None, 1.0),
        safety=SafetyReport(
            token.mint,
            mint_authority_renounced=True,
            freeze_authority_disabled=True,
            lp_locked_or_burned_pct=100.0,
            top10_pct=15.0,
            holder_count=2_000,
            source="rugcheck",
        ),
        enrichment=Enrichment(
            holder_count=2_000,
            mint_authority_renounced=True,
            freeze_authority_disabled=True,
            source="helius",
        ),
    )
    result.provider_evidence["gmgn"] = {
        "renownedTraders": [{
            "address": "kol-wallet-1",
            "name": "KOL One",
            "buyUsd": 5_000,
            "sellUsd": 1_000,
            "suspicious": False,
        }],
    }
    result.scores = {"runner": 70.0, "organic": 70.0, "manipulation": 10.0}
    return result


def test_complete_exact_contract_audit_passes(settings):
    proof = audit_candidates([candidate()], settings)
    assert proof.candidate_count == 1
    assert len(proof.mint_digest) == 64


def test_missing_exact_kol_history_blocks_the_entire_delivery(settings):
    first = candidate("GOOD")
    second = candidate("NOKOL")
    second.provider_evidence["gmgn"].pop("renownedTraders")

    with pytest.raises(DeliveryPreflightError, match="exact-mint KOL trader history unavailable"):
        audit_candidates([first, second], settings)


def test_suspicious_kol_does_not_satisfy_confirmation(settings):
    item = candidate()
    item.provider_evidence["gmgn"]["renownedTraders"][0]["suspicious"] = True

    with pytest.raises(DeliveryPreflightError, match="0/1 confirmed non-suspicious"):
        audit_candidates([item], settings)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("top10_pct", "top-10 concentration unavailable"),
        ("lp_locked_or_burned_pct", "LP lock/burn status unavailable or zero"),
        ("mint_authority_renounced", "mint authority/contract mintability not confirmed disabled"),
        ("freeze_authority_disabled", "freeze/pause/blacklist powers not confirmed disabled"),
    ],
)
def test_missing_security_measurement_fails_closed(settings, field, message):
    item = candidate()
    setattr(item.safety, field, None)
    if field == "mint_authority_renounced":
        item.enrichment.mint_authority_renounced = None
    if field == "freeze_authority_disabled":
        item.enrichment.freeze_authority_disabled = None

    with pytest.raises(DeliveryPreflightError, match=message):
        audit_candidates([item], settings)


def test_any_security_flag_blocks_delivery(settings):
    item = candidate()
    item.safety.risk_flags = ["contract source is not published"]

    with pytest.raises(DeliveryPreflightError, match="security flag"):
        audit_candidates([item], settings)


def test_hidden_discord_filter_universe_is_part_of_delivery_preflight():
    visible = candidate("VISIBLE")
    filtered = candidate("FILTERED")
    brief = Brief(
        generated_at=datetime.now(timezone.utc),
        scorecard=Scorecard(),
        metas=[],
        new_and_moving=[],
        ctos=[],
        follow_ups=[],
        onchain=[],
        excluded=[],
        source_statuses=[],
        runners=[visible],
        runner_universe=[visible, filtered],
    )

    assert [item.token.symbol for item in delivery_candidates(brief)] == [
        "VISIBLE", "FILTERED",
    ]


def test_filter_universe_allows_unknown_authorities_but_requires_holder_structure(settings):
    filtered = candidate("FILTERED")
    filtered.safety.mint_authority_renounced = None
    filtered.safety.freeze_authority_disabled = None
    filtered.enrichment.mint_authority_renounced = None
    filtered.enrichment.freeze_authority_disabled = None
    brief = Brief(
        generated_at=datetime.now(timezone.utc),
        scorecard=Scorecard(),
        metas=[],
        new_and_moving=[],
        ctos=[],
        follow_ups=[],
        onchain=[],
        excluded=[],
        source_statuses=[],
        runners=[],
        runner_universe=[filtered],
    )

    assert audit_brief(brief, settings).candidate_count == 1

    filtered.safety.holder_count = None
    filtered.enrichment.holder_count = None
    with pytest.raises(DeliveryPreflightError, match="holder count unavailable or zero"):
        audit_brief(brief, settings)


def test_filter_universe_rejects_low_runner_quality(settings):
    settings.values.setdefault("journal", {})["runner_universe_min_runner_score"] = 40.0
    filtered = candidate("LOWSCORE")
    filtered.scores["runner"] = 39.9
    brief = Brief(
        generated_at=datetime.now(timezone.utc),
        scorecard=Scorecard(),
        metas=[],
        new_and_moving=[],
        ctos=[],
        follow_ups=[],
        onchain=[],
        excluded=[],
        source_statuses=[],
        runners=[],
        runner_universe=[filtered],
    )

    with pytest.raises(DeliveryPreflightError, match="runner quality score 39.9"):
        audit_brief(brief, settings)
