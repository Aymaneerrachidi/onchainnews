from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from brief.engine import build_brief
from brief.ledger import Ledger
from brief.render.email import email_subject, render_email
from tests.conftest import build_settings

NOW = datetime(2026, 8, 6, 6, 45, tzinfo=ZoneInfo("Europe/Paris"))


@pytest.mark.asyncio
async def test_email_is_flat_and_inline_styled(tmp_path):
    """Email clients cannot open the interactive report; the email must expand it."""
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        body = render_email(brief, settings)
        assert "<details" not in body and "</details>" not in body
        assert "<style" not in body and "class=" not in body
        assert "fomo" in body, "the email carries the brand it belongs to"
        # Gmail drops web fonts and SVG, so neither may be relied on.
        assert "fonts.googleapis" not in body and "<svg" not in body
        assert brief.generated_at.strftime("%d %b %Y") in body
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_email_renders_runner_recap_lines(tmp_path):
    """The email is a trader recap: coins, hit market cap, and context."""
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        body = render_email(brief, settings)
        assert "Daily Memecoin Recap" in body
        assert "Runners of the day" in body
        assert "-&gt; hit" in body
        for candidate in brief.runners:
            assert candidate.token.symbol in body
            assert f'href="{candidate.token.url}"' in body
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_subject_prefix_and_date(tmp_path):
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        subject = email_subject(brief, settings)
        # The subject is read before anything is opened, so it leads with the
        # day's headline while avoiding characters that rendered badly in Gmail.
        assert subject.startswith("Fomo Onchain | ")
        assert brief.generated_at.strftime("%d %b") in subject
        recap = [
            candidate for candidate in [*brief.runners, *brief.blocked_runners]
            if candidate.scores.get("runner", 0) >= 25
        ]
        if recap:
            assert any(f"${candidate.token.symbol}" in subject for candidate in recap)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_email_escapes_user_content(tmp_path):
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        body = render_email(brief, settings)
        assert "<script" not in body
        assert "<iframe" not in body
        assert "<object" not in body
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_removed_dashboard_sections_stay_out_of_email(tmp_path):
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        body = render_email(brief, settings)
        assert "Runners of the day" in body
        assert "Ran, but disqualified" not in body
        assert "Worth a closer look" not in body
        assert "Did 5x+" in body
    finally:
        ledger.close()


def test_brief_older_than_a_week_still_renders_an_empty_email(tmp_path):
    """An empty brief is a result, not an outage -- the email must still say so."""
    from brief.models import Brief, Scorecard

    brief = Brief(
        generated_at=NOW - timedelta(days=400),
        scorecard=Scorecard(),
        metas=[],
        new_and_moving=[],
        ctos=[],
        follow_ups=[],
        onchain=[],
        excluded=[],
        source_statuses=[],
        runners=[],
    )
    settings = build_settings(tmp_path)
    body = render_email(brief, settings)
    assert "Nothing cleared the floors today" in body


def test_email_recaps_observed_movers_with_caveats(tmp_path):
    """The newsletter is a recap, not only the clean gate output."""
    from brief.models import Brief, Enrichment, SafetyReport, Scorecard
    from brief.scoring import score_candidate
    from tests.test_tracks import _tape

    settings = build_settings(tmp_path)
    settings.values.setdefault("delivery", {})["newsletter_min_observed_runner_score"] = 0
    settings.values["delivery"]["newsletter_excluded_risk_terms"] = [
        "move on only 0.",
        "move with no linked social context",
    ]
    clean = _tape("CLEAN", mcap=900_000, vol24=2_000_000, vol6=700_000, liq=120_000, trades6=2_000, buys6=1_050)
    clean.token.txns_24h = clean.token.txns_6h
    clean.token.socials = [{"type": "twitter", "url": "https://x.com/clean"}]
    clean.safety = SafetyReport("clean", holder_count=4_000, top10_pct=12.0, lp_locked_or_burned_pct=100.0)
    clean.run_multiple = 4.0
    score_candidate(clean, settings)

    watched = _tape("WATCH", mcap=700_000, vol24=1_500_000, vol6=400_000, liq=90_000, trades6=1_800, buys6=930)
    watched.token.txns_24h = watched.token.txns_6h
    watched.safety = SafetyReport("watch", holder_count=1_600, top10_pct=22.0, lp_locked_or_burned_pct=100.0)
    watched.enrichment = Enrichment()
    watched.run_multiple = 8.0
    watched.risk_labels = ["no linked social context"]
    score_candidate(watched, settings)

    junk = _tape("JUNK", mcap=1_700_000, vol24=300_000, vol6=75_000, liq=100_000, trades6=1_800, buys6=900)
    junk.token.txns_24h = junk.token.txns_6h
    junk.safety = SafetyReport("junk", holder_count=1_500, top10_pct=5.0, lp_locked_or_burned_pct=100.0)
    junk.run_multiple = 40.0
    junk.risk_labels = [
        "40x move on only 0.18x turnover",
        "40x move with no linked social context",
    ]
    score_candidate(junk, settings)
    junk.scores["runner"] = max(junk.scores.get("runner", 0.0), 45.0)

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
        runners=[clean],
        blocked_runners=[watched, junk],
    )

    body = render_email(brief, settings)

    assert "$CLEAN" in body
    assert "$WATCH" in body
    assert "$JUNK" not in body
    assert "no linked social context" in body
    assert "Ran, but disqualified" not in body


def test_email_leads_with_verified_peak_not_faded_cutoff_cap(tmp_path):
    from brief.models import Brief, Scorecard
    from brief.render.email import render_email
    from tests.test_tracks import _tape

    settings = build_settings(tmp_path)
    faded = _tape("FADED", mcap=4_000, vol24=1_500_000, vol6=100_000,
                  liq=4_500, trades6=800, buys6=410)
    faded.observed_peak_market_cap = 260_000.0
    faded.read = (
        "$FADED hit a candle-verified $260,000 intraday market cap before "
        "fading to $4,000 by the cutoff."
    )
    brief = Brief(
        generated_at=NOW,
        scorecard=Scorecard(), metas=[], new_and_moving=[], ctos=[], follow_ups=[],
        onchain=[], excluded=[], source_statuses=[], runners=[faded], blocked_runners=[],
    )

    body = render_email(brief, settings)

    assert "Hit $260k" in body
    assert "Hit $4k" not in body
    assert "candle-verified $260,000" in body
