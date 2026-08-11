from __future__ import annotations

import copy
import json
from datetime import timedelta
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from brief.config import Settings
from brief.engine import _cluster_history_line, build_brief
from brief.intelligence import detect_lp_removal
from brief.ledger import Ledger, iso
from brief.render.markdown import render_markdown
from brief.watcher import deliver_alerts


NOW = datetime(2026, 8, 6, 6, 45, tzinfo=ZoneInfo("Europe/Paris"))


def test_global_cluster_registry_surfaces_prior_launch_history(tmp_path):
    ledger = Ledger(tmp_path / "clusters.db")
    try:
        ledger.register_cluster("FUNDER", ["W1", "W2", "W3"], "OLD_MINT", NOW - timedelta(days=10))
        row = ledger.db.execute("SELECT id FROM observations WHERE mint='OLD_MINT'").fetchone()
        if row is None:
            ledger.record_feature("OLD_MINT", "OLD", 100, 1, NOW - timedelta(days=10))
            row = ledger.db.execute("SELECT id FROM observations WHERE mint='OLD_MINT'").fetchone()
        ledger.db.execute(
            "INSERT INTO forward_returns(observation_id,horizon_hours,measured_at,market_cap,return_pct) VALUES(?,?,?,?,?)",
            (row["id"], 168, iso(NOW - timedelta(days=3)), 12, -88),
        )
        ledger.db.commit()
        ledger.sync_cluster_outcomes()
        history = ledger.cluster_prior_history("FUNDER", ["W1", "W2", "NEW"], "NEW_MINT")
        line = _cluster_history_line(history)
        assert line is not None
        assert "prior token" in line
        assert "-88% at 7d" in line
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_thirty_percent_lp_removal_pushes_in_same_poll_cycle():
    sent: list[list[str]] = []

    async def fake_sender(messages: list[str]):
        sent.append(messages)

    removal = detect_lp_removal(100, 70, 10)
    assert removal == pytest.approx(30)
    await deliver_alerts([f"$TEST pool balance proxy fell {removal:.1f}%"], fake_sender)
    assert sent == [["SOLANA WATCHER\n$TEST pool balance proxy fell 30.0%"]]


def _archive_fixture(ledger: Ledger, settings: Settings) -> None:
    fixture_path = settings.path("run", "fixture_path")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    dex = settings.get("sources", "dexscreener_base_url")
    rug = settings.get("sources", "rugcheck_base_url")
    for path, payload in fixture.items():
        base = rug if path.startswith("/v1/tokens/") else dex
        ledger.archive_response(
            method="GET", endpoint=f"{base}{path}", request_params={}, request_body=None,
            status=200, response_body=json.dumps(payload), captured_at=NOW,
        )
    ledger.archive_response(
        method="GET",
        endpoint="https://cdn.syndication.twimg.com/widgets/followbutton/info.json",
        request_params={"screen_names": "a"}, request_body=None,
        status=200, response_body="[]", captured_at=NOW,
    )
    ledger.archive_response(
        method="GET", endpoint="https://lite-api.jup.ag/price/v3",
        request_params={"ids": "So11111111111111111111111111111111111111112"}, request_body=None,
        status=200, response_body="{}", captured_at=NOW,
    )


@pytest.mark.asyncio
async def test_archive_replay_is_reproducible_and_changes_with_thresholds(settings, tmp_path):
    ledger = Ledger(tmp_path / "replay.db")
    try:
        _archive_fixture(ledger, settings)
        baseline_values = copy.deepcopy(settings.values)
        baseline_values["run"].pop("fixture_path", None)
        baseline = Settings(settings.root, baseline_values)
        first = render_markdown(await build_brief(baseline, ledger, commit=False, now=NOW, replay_date="2026-08-06"))

        modified_values = copy.deepcopy(baseline_values)
        modified_values["thresholds"]["min_market_cap"] = 450_000.0
        modified = Settings(settings.root, modified_values)
        second = render_markdown(await build_brief(modified, ledger, commit=False, now=NOW, replay_date="2026-08-06"))
        third = render_markdown(await build_brief(modified, ledger, commit=False, now=NOW, replay_date="2026-08-06"))
        assert first != second
        assert second == third
    finally:
        ledger.close()


def test_scorecard_includes_quartiles_and_crash_rate(tmp_path):
    ledger = Ledger(tmp_path / "distribution.db")
    returns = [-95, -90, -20, 0, 10, 20, 30, 40]
    try:
        for index, value in enumerate(returns):
            ledger.record_feature(f"M{index}", f"T{index}", 100, index + 1, NOW - timedelta(days=4, minutes=-index))
        rows = ledger.db.execute("SELECT id FROM observations ORDER BY id").fetchall()
        for row, value in zip(rows, returns):
            ledger.db.execute(
                "INSERT INTO forward_returns VALUES (?,72,?,?,?)",
                (row["id"], iso(NOW), 100 * (1 + value / 100), value),
            )
        ledger.db.commit()
        score = ledger.scorecard(NOW)
        assert score.featured_q1_72h == pytest.approx(-37.5)
        assert score.featured_q3_72h == pytest.approx(22.5)
        assert score.featured_crash_pct_72h == 12.5
    finally:
        ledger.close()
