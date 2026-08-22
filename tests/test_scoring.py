from __future__ import annotations

from datetime import timedelta

from brief.models import SafetyReport
from brief.scoring import score_candidate
from tests.conftest import build_settings
from tests.test_tracks import NOW, _tape


def test_scoring_prefers_quality_over_raw_multiple(tmp_path):
    settings = build_settings(tmp_path / "scores")

    quality = _tape(
        "QUALITY",
        mcap=900_000,
        vol24=4_500_000,
        vol6=1_600_000,
        liq=180_000,
        trades6=5_000,
        buys6=2_750,
    )
    quality.token.txns_24h = quality.token.txns_6h
    quality.token.price_change_24h = 700
    quality.token.price_change_6h = 180
    quality.token.price_change_1h = 12
    quality.token.socials = [{"type": "twitter", "url": "https://x.com/quality"}]
    quality.safety = SafetyReport("m", holder_count=6_000, top10_pct=13.0, lp_locked_or_burned_pct=100.0)
    quality.enrichment.holder_change_24h = 2_000
    quality.run_multiple = 8.0
    quality.kol_wallets_scanned = 30
    quality.kol_buyers = ["Gasp", "theo", "Wugi"]
    quality.kol_holders = ["Gasp", "Wugi"]

    huge_but_bad = _tape(
        "BADX",
        mcap=1_400_000,
        vol24=320_000,
        vol6=30_000,
        liq=30_000,
        trades6=900,
        buys6=810,
        reuse=2,
    )
    huge_but_bad.token.txns_24h = huge_but_bad.token.txns_6h
    huge_but_bad.token.price_change_24h = 3900
    huge_but_bad.token.price_change_6h = 20
    huge_but_bad.token.price_change_1h = -28
    huge_but_bad.safety = SafetyReport("m", holder_count=900, top10_pct=38.0, lp_locked_or_burned_pct=40.0)
    huge_but_bad.run_multiple = 40.0

    score_candidate(quality, settings)
    score_candidate(huge_but_bad, settings)

    assert quality.scores["runner"] > huge_but_bad.scores["runner"]
    assert quality.scores["organic"] > huge_but_bad.scores["organic"]
    assert quality.scores["manipulation"] < huge_but_bad.scores["manipulation"]
    assert quality.classification in {"HIGH-CONVICTION ORGANIC RUNNER", "ORGANIC RUNNER"}


def test_payload_contains_runner_score_and_classification(tmp_path):
    from brief.models import Brief, Scorecard
    from brief.render.payload import build_payload

    settings = build_settings(tmp_path / "payload-score")
    candidate = _tape(
        "PAYLOAD",
        mcap=500_000,
        vol24=1_000_000,
        vol6=350_000,
        liq=80_000,
        trades6=1_500,
        buys6=800,
    )
    candidate.token.txns_24h = candidate.token.txns_6h
    candidate.safety = SafetyReport("m", holder_count=2_500, top10_pct=18.0, lp_locked_or_burned_pct=100.0)
    candidate.token.pair_created_at = NOW - timedelta(hours=12)
    candidate.run_multiple = 6.0
    score_candidate(candidate, settings)

    brief = Brief(
        generated_at=NOW,
        scorecard=Scorecard(),
        metas=[],
        new_and_moving=[],
        ctos=[],
        follow_ups=[],
        onchain=[],
        excluded=[],
        source_statuses=[],
        runners=[candidate],
    )
    row = build_payload(brief, settings)["runners"][0]

    assert row["scores"]["runner"] >= 0
    assert row["scoreComponents"]["organic"]
    assert row["classification"]


def test_gmgn_organic_lane_is_positive_corroboration_not_a_requirement(tmp_path):
    settings = build_settings(tmp_path / "gmgn-organic-score")
    ordinary = _tape(
        "ORDINARY", mcap=700_000, vol24=2_000_000, vol6=600_000,
        liq=120_000, trades6=2_000, buys6=1_100,
    )
    qualified = _tape(
        "QUALIFIED", mcap=700_000, vol24=2_000_000, vol6=600_000,
        liq=120_000, trades6=2_000, buys6=1_100,
    )
    for candidate in (ordinary, qualified):
        candidate.token.txns_24h = candidate.token.txns_6h
        candidate.safety = SafetyReport(
            candidate.token.mint, holder_count=3_000, top10_pct=18.0,
            lp_locked_or_burned_pct=100.0,
        )
        candidate.run_multiple = 5.0
    qualified.provider_evidence["gmgn"] = {"organicQualified": True}

    score_candidate(ordinary, settings)
    score_candidate(qualified, settings)

    assert ordinary.scores["runner"] > 0
    assert qualified.scores["runner"] > ordinary.scores["runner"]
    assert qualified.scores["organic"] > ordinary.scores["organic"]
    assert qualified.score_components["runner"]["gmgnOrganicLane"] == 100.0
