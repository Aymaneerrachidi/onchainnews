from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from brief.engine import build_brief
from brief.ledger import Ledger
from brief.render.email import email_subject, pulse_email_subject, render_email, render_pulse_email
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
        # The peak sits right-aligned opposite the ticker, so the arrow that
        # used to join them is no longer drawn. The loop below already checks
        # every runner's ticker reaches the page.
        for candidate in brief.runners:
            assert candidate.token.symbol in body
            assert f'href="https://fomo.family/tokens/solana/{candidate.token.mint}"' in body
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
        assert "Recap coins" not in body
        assert "Lead read" not in body
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


def test_hourly_email_contains_only_confirmed_runner_alert_content(tmp_path):
    from tests.test_tracks import _tape

    settings = build_settings(tmp_path)
    settings.values["pulse"] = {"window_hours": 12.0}
    candidate = _tape(
        "PULSE", mcap=850_000, vol24=1_900_000, vol6=700_000,
        liq=110_000, trades6=2_200, buys6=1_200,
    )
    candidate.run_multiple = 4.2
    candidate.read = "$PULSE kept volume and liquidity through three hourly checks."
    items = [(candidate, 1)]

    subject = pulse_email_subject(items, settings)
    body = render_pulse_email(items, settings, NOW)

    assert subject == "Fomo Onchain runner alert | $PULSE"
    assert "Confirmed runner alert" in body
    assert "1 scan" in body
    assert "$PULSE" in body
    assert "CA:" in body and candidate.token.mint in body
    assert "No email is sent on an empty hour" in body


def test_hourly_email_refuses_an_empty_alert(tmp_path):
    settings = build_settings(tmp_path)
    with pytest.raises(ValueError, match="at least one"):
        render_pulse_email([], settings, NOW)


def test_daily_email_does_not_truncate_the_24_hour_runner_ledger(tmp_path):
    from brief.models import Brief, Scorecard
    from tests.test_tracks import _tape

    settings = build_settings(tmp_path)
    runners = []
    for index in range(16):
        candidate = _tape(
            f"DAY{index}", mcap=300_000 + index * 10_000,
            vol24=800_000, vol6=250_000, liq=60_000,
            trades6=1_200, buys6=650,
        )
        candidate.run_multiple = 2.0 + index / 10
        candidate.read = f"$DAY{index} qualified during the hourly scan."
        runners.append(candidate)
    brief = Brief(
        generated_at=NOW, scorecard=Scorecard(), metas=[], new_and_moving=[],
        ctos=[], follow_ups=[], onchain=[], excluded=[], source_statuses=[],
        runners=runners, blocked_runners=[],
    )

    body = render_email(brief, settings)

    for index in range(16):
        assert f"$DAY{index}" in body


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
    # Shop-talk labels are no longer shown to the reader; the row carries
    # only measured problems.
    assert "no linked social context" not in body
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

    assert "$260k" in body
    assert "hit</span> $4k" not in body
    # The written read is no longer printed: it recited the numbers shown
    # beside it. A coin with no story shows its numbers and nothing else.
    assert "$4,000 by the cutoff" not in body


def test_email_uses_verified_news_as_coin_thesis(tmp_path):
    from brief.models import Brief, Scorecard
    from tests.test_tracks import _tape

    settings = build_settings(tmp_path)
    runner = _tape("STORY", mcap=1_200_000, vol24=2_000_000, vol6=500_000,
                   liq=120_000, trades6=1_300, buys6=700)
    runner.news_evidence = [{
        "source": "OpenNews",
        "summary": "The project shipped a privacy payment app after its public beta went viral.",
        "url": "https://example.com/story",
    }]
    brief = Brief(
        generated_at=NOW, scorecard=Scorecard(), metas=[], new_and_moving=[],
        ctos=[], follow_ups=[], onchain=[], excluded=[], source_statuses=[],
        runners=[runner], blocked_runners=[],
    )

    body = render_email(brief, settings)

    assert "privacy payment app" in body
    assert "running laps on everything" not in body


def test_email_never_lets_raw_blocked_peak_displace_approved_runner(tmp_path):
    """The audit recap keeps rugs; the public email must lead with approved names."""
    from brief.models import Brief, Scorecard
    from tests.test_tracks import _tape

    settings = build_settings(tmp_path)
    approved = _tape("GOOD", mcap=1_200_000, vol24=2_000_000, vol6=500_000,
                     liq=120_000, trades6=1_300, buys6=700)
    approved.peak_market_cap = 1_400_000
    blocked = _tape("RUG", mcap=9_000_000_000, vol24=100_000, vol6=5_000,
                    liq=0, trades6=12, buys6=11)
    blocked.peak_market_cap = 10_000_000_000
    blocked.risk_labels = ["liquidity neither locked nor burned, it can be pulled"]
    brief = Brief(
        generated_at=NOW, scorecard=Scorecard(), metas=[], new_and_moving=[],
        ctos=[], follow_ups=[], onchain=[], excluded=[], source_statuses=[],
        runners=[approved], blocked_runners=[blocked],
        recap={"all": [{"mint": blocked.token.mint}, {"mint": approved.token.mint}]},
    )

    body = render_email(brief, settings)

    assert "$GOOD" in body
    assert "$RUG" not in body


def test_email_explains_intraday_round_trips_without_vendor_branding(tmp_path):
    """Old coins stay only when the email shows the actual start/high/cutoff path."""
    from brief.models import Brief, Scorecard
    from tests.test_tracks import _tape

    settings = build_settings(tmp_path)
    runner = _tape(
        "ROUNDTRIP", mcap=500_000, vol24=1_100_000, vol6=150_000,
        liq=85_000, trades6=1_400, buys6=720,
    )
    runner.token.url = "https://gmgn.ai/sol/token/secret-provider-link"
    runner.peak_market_cap = 1_450_000
    runner.observed_peak_market_cap = 1_450_000
    runner.provider_evidence["gmgn"] = {
        "kline24hOpenPrice": 0.0005,
        "kline24hHighPrice": 0.00145,
        "kline24hPeakMarketCap": 1_450_000,
        "kline24hPeakFromOpenPct": 190.0,
        "kline24hChangePct": 0.4,
        "kolCount": 12,
        "smartMoneyCount": 30,
    }
    brief = Brief(
        generated_at=NOW, scorecard=Scorecard(), metas=[], new_and_moving=[],
        ctos=[], follow_ups=[], onchain=[], excluded=[], source_statuses=[],
        runners=[runner], blocked_runners=[],
    )

    body = render_email(brief, settings)

    assert "24h path: $500k to $1.4M at the high, then $500k" in body
    assert "the move faded and finished +0%" in body
    # The move has its own line in the card rather than sharing a slash-strip.
    assert "+190% to the high" in body
    assert "finished +0%" in body
    assert "fomo.family/tokens/solana/" in body
    assert "gmgn" not in body.casefold()
