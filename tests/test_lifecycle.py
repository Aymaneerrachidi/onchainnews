from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brief.ledger import Ledger
from brief.lifecycle import attach_lifecycle, build_structured_recap, tier_for_peak
from tests.test_tracks import _tape


NOW = datetime(2026, 8, 22, 6, 45, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("market_cap", "tier"),
    [
        (10_000_000, "S"),
        (1_000_000, "S"),
        (800_000, "A"),
        (500_000, "A"),
        (499_999, "B"),
        (250_000, "B"),
        (249_999, "BELOW"),
    ],
)
def test_runner_tiers_have_no_gap(market_cap, tier):
    assert tier_for_peak(market_cap) == tier


def test_lifecycle_reconstructs_peak_multiple_and_round_trip(tmp_path):
    ledger = Ledger(tmp_path / "brief.db")
    candidate = _tape(
        "ROUNDTRIP", mcap=100_000, vol24=2_000_000, vol6=500_000,
        liq=80_000, trades6=2_000, buys6=1_050,
    )
    candidate.token.pair_created_at = NOW - timedelta(hours=23)
    try:
        ledger.record_market_snapshot(candidate.token, NOW - timedelta(hours=23), provider="fixture")
        candidate.token.market_cap = 800_000
        ledger.record_market_snapshot(candidate.token, NOW - timedelta(hours=3), provider="fixture")
        candidate.token.market_cap = 200_000
        ledger.record_market_snapshot(candidate.token, NOW, provider="fixture")

        attach_lifecycle(candidate, ledger, NOW)

        assert candidate.start_market_cap == 100_000
        assert candidate.peak_market_cap == 800_000
        assert candidate.peak_multiple == pytest.approx(8.0)
        assert candidate.drawdown_from_peak_pct == pytest.approx(75.0)
        assert candidate.runner_tier == "A"
        assert candidate.round_trip is True
    finally:
        ledger.close()


def test_recap_uses_the_three_requested_peak_bands():
    ordinary = _tape(
        "ORDINARY", mcap=300_000, vol24=400_000, vol6=100_000,
        liq=50_000, trades6=600, buys6=310,
    )
    ordinary.runner_tier = "BELOW"
    ordinary.peak_market_cap = 249_000
    ordinary.scores = {"runner": 55, "organic": 55, "manipulation": 20}

    exceptional = _tape(
        "EXTRA", mcap=320_000, vol24=900_000, vol6=300_000,
        liq=70_000, trades6=1_200, buys6=650,
    )
    exceptional.runner_tier = "B"
    exceptional.peak_market_cap = 320_000
    exceptional.scores = {"runner": 75, "organic": 74, "manipulation": 20}

    recap = build_structured_recap([ordinary, exceptional], NOW)

    assert [row["symbol"] for row in recap["tiers"]["B"]] == ["EXTRA"]
    assert all(row["symbol"] != "ORDINARY" for row in recap["all"])


def test_large_manipulated_run_remains_visible_as_questionable():
    candidate = _tape(
        "VISIBLE", mcap=1_500_000, vol24=8_000_000, vol6=2_000_000,
        liq=100_000, trades6=5_000, buys6=4_700,
    )
    candidate.runner_tier = "S"
    candidate.peak_market_cap = 2_000_000
    candidate.round_trip = True
    candidate.scores = {"runner": 92, "organic": 30, "manipulation": 88}
    candidate.provider_evidence["editorial"] = {"published": False}

    recap = build_structured_recap([candidate], NOW)

    assert recap["tiers"]["S"] == []
    assert recap["all"] == []
    assert recap["runnerOfDay"] is None
    assert recap["questionable"][0]["symbol"] == "VISIBLE"
    assert recap["observedAll"][0]["symbol"] == "VISIBLE"
    assert recap["roundTrips"] == []
