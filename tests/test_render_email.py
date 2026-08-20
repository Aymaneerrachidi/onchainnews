from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from brief.engine import build_brief
from brief.ledger import Ledger
from brief.render.email import email_subject, render_email
from brief.render.html import report_picks
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
        for pick in report_picks(brief):
            assert pick.read in body, "every pick's read must appear verbatim"
        assert brief.generated_at.strftime("%d %b %Y") in body
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_email_carries_the_same_picks_as_the_site(tmp_path):
    """One shared picks definition means the email can never disagree with the site."""
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        body = render_email(brief, settings)
        picks = report_picks(brief)
        assert picks, "fixture must produce picks for this contract test"
        for pick in picks:
            # A pick that is also the biggest run appears once, in the hero,
            # so this asserts a live link rather than a particular section.
            assert pick.token.symbol in body
            assert f'href="{pick.token.url}"' in body
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
        if brief.runners:
            top = max(brief.runners, key=lambda c: c.run_multiple)
            assert f"${top.token.symbol}" in subject
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
async def test_runners_and_blocked_sections_are_rendered(tmp_path):
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        body = render_email(brief, settings)
        assert "Runners of the day" in body or "Biggest run" in body
        if brief.blocked_runners:
            assert "Ran, but disqualified" in body
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
    assert "Nothing else cleared the bar" in body
    assert "Nothing cleared the floors today" in body
