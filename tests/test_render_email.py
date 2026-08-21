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
