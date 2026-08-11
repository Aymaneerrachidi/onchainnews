from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from brief.engine import build_brief
from brief.ledger import Ledger, iso
from brief.render.html import render_html
from brief.render.markdown import render_markdown
from brief.render.telegram import render_telegram


NOW = datetime(2026, 8, 6, 6, 45, tzinfo=ZoneInfo("Europe/Paris"))


@pytest.mark.asyncio
async def test_same_day_rerun_preserves_daily_shortlist_without_duplicate_feature(settings):
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        first = await build_brief(settings, ledger, now=NOW)
        second = await build_brief(settings, ledger, now=NOW + timedelta(minutes=1))
        assert [c.token.mint for c in first.new_and_moving] == ["MINTA"]
        assert [c.token.mint for c in second.new_and_moving] == ["MINTA"]
        assert not second.follow_ups
        assert ledger.feature_state("MINTA")["times_featured"] == 1
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_invalid_helius_does_not_stop_run(settings, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "invalid")
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        assert brief.new_and_moving
        status = next(s for s in brief.source_statuses if s.name == "Holder snapshots")
        assert not status.available
        assert brief.onchain
        assert all(item.status == "unavailable" for item in brief.onchain)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_tripled_market_cap_returns_as_follow_up(settings):
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        ledger.record_feature("MINTA", "ALPHA", 100000, 1, NOW - timedelta(days=1))
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        assert [c.token.mint for c in brief.follow_ups] == ["MINTA"]
        assert brief.follow_ups[0].follow_up_multiple == 4
        html = render_html(brief)
        shortlist = html.split('<section id="shortlist"', 1)[1].split('</section>', 1)[0]
        assert "$ALPHA" not in shortlist
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_fixture_scoring_is_deterministic(settings, tmp_path):
    first_ledger = Ledger(tmp_path / "first.db")
    second_ledger = Ledger(tmp_path / "second.db")
    try:
        first = await build_brief(settings, first_ledger, commit=False, now=NOW)
        second = await build_brief(settings, second_ledger, commit=False, now=NOW)
        first_values = [(c.token.mint, c.signals.turnover, c.signals.acceleration) for c in first.new_and_moving]
        second_values = [(c.token.mint, c.signals.turnover, c.signals.acceleration) for c in second.new_and_moving]
        assert first_values == second_values == [("MINTA", 2.0, 21.0)]
    finally:
        first_ledger.close()
        second_ledger.close()


@pytest.mark.asyncio
async def test_below_minimum_only_appears_in_filtered_appendix(settings):
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        prominent = brief.new_and_moving + brief.ctos + brief.follow_ups
        assert all(c.token.market_cap >= 150000 for c in prominent)
        dust = next(item for item in brief.excluded if item.token.mint == "MINTB")
        assert "market cap below" in dust.reasons[0]
        control = next(item for item in brief.excluded if item.token.mint == "MINTC")
        assert "top10 47%" in control.reasons
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_morning_window_lists_all_discovered_new_pairs_with_screening_state(settings):
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        assert brief.window_start == NOW - timedelta(hours=24)
        launches = {launch.token.mint: launch for launch in brief.launches_last_24h}
        assert set(launches) == {"MINTA", "MINTB"}
        assert launches["MINTA"].status == "SHORTLIST"
        assert launches["MINTB"].status == "FILTERED"
        assert any("market cap below" in reason for reason in launches["MINTB"].reasons)
        pick = brief.new_and_moving[0]
        assert len(pick.strength_reasons) >= 3
        assert len(pick.interest_reasons) >= 2
        html = render_html(brief)
        assert "Why the rest did not make the brief" in html
        assert "on-chain launch collector has not started" in html
        assert "Why it reads strong" in html
        assert "Why it is interesting today" in html
        assert "$DUST" not in html
    finally:
        ledger.close()


def test_scorecard_against_ten_hand_checked_returns(tmp_path):
    ledger = Ledger(tmp_path / "scorecard.db")
    returns = [-50, -20, -10, 0, 2, 4, 10, 20, 30, 100]
    try:
        observed = NOW - timedelta(days=4)
        for index, value in enumerate(returns):
            ledger.record_feature(f"M{index}", f"T{index}", 100, index + 1, observed + timedelta(minutes=index))
        rows = ledger.db.execute("SELECT id FROM observations ORDER BY id").fetchall()
        for row, value in zip(rows, returns):
            ledger.db.execute(
                "INSERT INTO forward_returns VALUES (?, 72, ?, ?, ?)",
                (row["id"], iso(NOW - timedelta(days=1)), 100 * (1 + value / 100), value),
            )
        ledger.db.commit()
        score = ledger.scorecard(NOW)
        assert score.featured_count == 10
        assert score.featured_median_72h == 3
        assert score.featured_up_pct_72h == 60
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_all_renderers_include_disclaimer(settings):
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        markdown = render_markdown(brief)
        assert "Data, not advice" in markdown
        markdown.encode("cp1252")
        html = render_html(brief)
        assert "Data, not advice" in html
        assert 'href="https://app.bubblemaps.io/sol/token/' in html
        assert all(len(chunk) <= 3900 for chunk in render_telegram(brief))
    finally:
        ledger.close()
