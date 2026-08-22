from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from brief.intraday import load_pulse_passes
from tests.conftest import build_settings


def _entry(mint: str, taken_at: datetime, market_cap: float) -> dict[str, object]:
    return {
        "mint": mint,
        "symbol": mint,
        "takenAt": taken_at.isoformat(),
        "marketCap": market_cap,
        "volume24h": 500_000,
        "runMultiple": 3.0,
        "scores": {"runner": 50.0},
    }


def test_daily_recap_loads_only_alerted_runners_from_the_last_24_hours(tmp_path):
    settings = build_settings(tmp_path)
    settings.values.setdefault("journal", {})["pulse_recap_alerted_only"] = True
    settings.values["pulse"] = {"state_path": "pulse-state.json"}
    now = datetime(2026, 8, 22, 6, 45, tzinfo=timezone.utc)
    recent = now - timedelta(hours=5)
    old = now - timedelta(hours=25)
    state = {
        "passes": {
            "SENT": [_entry("SENT", recent, 300_000), _entry("SENT", recent, 800_000)],
            "NOT_SENT": [_entry("NOT_SENT", recent, 900_000)],
            "OLD": [_entry("OLD", old, 1_000_000)],
        },
        "posted": {
            "SENT": recent.isoformat(),
            "OLD": old.isoformat(),
        },
    }
    (tmp_path / "pulse-state.json").write_text(json.dumps(state), encoding="utf-8")

    loaded = load_pulse_passes(settings, now - timedelta(hours=24), now)

    assert set(loaded) == {"SENT"}
    assert loaded["SENT"]["marketCap"] == 800_000
