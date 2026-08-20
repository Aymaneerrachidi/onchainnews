from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from brief.pulse import record_runner_passes


def runner(mint: str = "MINTA", symbol: str = "ALPHA"):
    token = SimpleNamespace(
        mint=mint,
        symbol=symbol,
        name="Alpha",
        chain_id="solana",
        url="https://dexscreener.com/solana/alpha",
        market_cap=1_000_000,
        liquidity_usd=100_000,
        volume_24h=2_000_000,
        price_change_24h=300,
        price_change_1h=20,
    )
    safety = SimpleNamespace(holder_count=1200)
    return SimpleNamespace(
        token=token,
        safety=safety,
        run_multiple=4.0,
        kol_buyers=[],
        risk_labels=[],
        read="$ALPHA ran on real volume.",
    )


def test_pulse_triggers_on_third_pass_inside_window():
    state = {"version": 1, "passes": {}, "posted": {}}
    now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    candidate = runner()

    first = record_runner_passes(
        state, [candidate], now,
        window_hours=12, required_passes=3, repost_after_hours=72, min_gap_minutes=45,
    )
    second = record_runner_passes(
        state, [candidate], now + timedelta(hours=1),
        window_hours=12, required_passes=3, repost_after_hours=72, min_gap_minutes=45,
    )
    third = record_runner_passes(
        state, [candidate], now + timedelta(hours=2),
        window_hours=12, required_passes=3, repost_after_hours=72, min_gap_minutes=45,
    )

    assert first == []
    assert second == []
    assert len(third) == 1
    assert third[0][0].token.symbol == "ALPHA"
    assert len(third[0][1]) == 3


def test_pulse_does_not_repost_inside_cooldown():
    now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    state = {
        "version": 1,
        "passes": {
            "MINTA": [
                {"takenAt": (now - timedelta(hours=2)).isoformat()},
                {"takenAt": (now - timedelta(hours=1)).isoformat()},
            ]
        },
        "posted": {"MINTA": (now - timedelta(hours=1)).isoformat()},
    }

    triggered = record_runner_passes(
        state, [runner()], now,
        window_hours=12, required_passes=3, repost_after_hours=72, min_gap_minutes=45,
    )

    assert triggered == []


def test_pulse_prunes_old_passes():
    now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    state = {
        "version": 1,
        "passes": {"MINTA": [{"takenAt": (now - timedelta(hours=13)).isoformat()}]},
        "posted": {},
    }

    record_runner_passes(
        state, [runner()], now,
        window_hours=12, required_passes=3, repost_after_hours=72, min_gap_minutes=45,
    )

    assert len(state["passes"]["MINTA"]) == 1
    assert state["passes"]["MINTA"][0]["symbol"] == "ALPHA"
