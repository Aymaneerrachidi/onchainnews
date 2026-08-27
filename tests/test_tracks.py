from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from brief.engine import build_brief
from brief.ledger import Ledger, iso
from brief.render.html import render_html
from brief.render.markdown import render_markdown
from brief.render.telegram import render_digest, render_telegram
from tests.conftest import build_settings


NOW = datetime(2026, 8, 6, 6, 45, tzinfo=ZoneInfo("Europe/Paris"))


def test_ledger_write_retry_recovers_after_transient_lock(tmp_path):
    ledger = Ledger(tmp_path / "retry.db")
    attempts = 0

    def flaky_write() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        ledger.db.execute(
            "INSERT INTO collector_state(key,value) VALUES(?,?)",
            ("retry-test", "ok"),
        )

    try:
        ledger._write_retry(flaky_write)
        assert attempts == 3
        assert ledger.collector_state("retry-test") == "ok"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_strong_older_token_is_a_mover_even_though_it_launched_days_ago(mover_settings):
    """The client asks for the strongest coins of the day, not only of the last 24h."""
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert [c.token.symbol for c in brief.movers] == ["MOVER"]
        mover = brief.movers[0]
        assert mover.track == "MOVER"
        assert not brief.new_and_moving, "a five-day-old pair must not enter the 24h launch track"
        assert mover.signals.age_hours > 24
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_ranked_discovery_finds_a_mover_the_dexscreener_feeds_never_show(
    mover_settings, monkeypatch
):
    """The narrow feeds are why the report was thin; ranked discovery widens it."""
    monkeypatch.setenv("BIRDEYE_API_KEY", "test-key")
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        symbols = [c.token.symbol for c in brief.movers]
        assert "VOLUME" in symbols
        status = next(s for s in brief.source_statuses if s.name == "Birdeye ranked discovery")
        assert status.available
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_ranked_discovery_skips_majors_and_names_below_the_floors(mover_settings, monkeypatch):
    monkeypatch.setenv("BIRDEYE_API_KEY", "test-key")
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        surfaced = {c.token.symbol for c in brief.movers + brief.ctos + brief.new_and_moving}
        assert "SOL" not in surfaced, "quote assets can never be a pick"
        assert "TINY" not in surfaced, "below the market-cap floor"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_run_still_works_without_a_birdeye_key(mover_settings):
    """The key is an upgrade, not a dependency."""
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert "VOLUME" not in {c.token.symbol for c in brief.movers}
        assert brief.movers or brief.ctos
        status = next(s for s in brief.source_statuses if s.name == "Birdeye ranked discovery")
        assert not status.available
        assert "not configured" in status.detail
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_quiet_token_of_the_same_age_is_not_a_mover(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        surfaced = {c.token.symbol for c in brief.movers + brief.ctos + brief.new_and_moving}
        assert "SLEEPY" not in surfaced
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_community_takeover_surfaces_outside_the_launch_window(mover_settings):
    """A CTO is an old token by definition; gating it on a 24h-old pair emptied the track."""
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert [c.token.symbol for c in brief.ctos] == ["TAKEN"]
        cto = brief.ctos[0]
        assert cto.track == "CTO"
        assert cto.signals.age_hours > 24 * 7
        assert cto.signals.cto_volume_since_claim == 260000
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_brief_is_not_empty_when_no_launch_qualifies(mover_settings):
    """The regression that made the report unsellable: a shortlist of zero."""
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert brief.movers or brief.ctos
        html = render_html(brief)
        assert "NOTHING CLEARED THE BAR TODAY" not in html
        assert "$MOVER" in html and "$TAKEN" in html
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_every_pick_carries_a_one_line_read(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        picks = [*brief.new_and_moving, *brief.movers, *brief.ctos]
        assert picks
        for candidate in picks:
            assert candidate.read.startswith(f"${candidate.token.symbol} — ")
            assert "market cap" in candidate.read
            assert "on the day" in candidate.read
        html = render_html(brief)
        assert '<section id="picks"' in html
        assert brief.movers[0].read.split(" — ")[1][:20] in html
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_movers_are_scored_like_every_other_featured_name(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        await build_brief(mover_settings, ledger, commit=True, now=NOW)
        assert ledger.feature_state("MINTM")["times_featured"] == 1
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_other_chains_are_excluded_until_configured(mover_settings, tmp_path):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert "OFFCHAIN" not in {c.token.symbol for c in brief.movers}
        reasons = [
            reason
            for item in brief.excluded
            if item.token.mint == "MINTBASE"
            for reason in item.reasons
        ]
        assert any("chain base not in solana" in reason for reason in reasons)
    finally:
        ledger.close()

    widened = build_settings(tmp_path / "base", "movers.json")
    widened.values["thresholds"]["chains"] = ["solana", "base"]
    ledger = Ledger(widened.path("run", "database_path"))
    try:
        brief = await build_brief(widened, ledger, commit=False, now=NOW)
        assert "OFFCHAIN" in {c.token.symbol for c in brief.movers}
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_digest_leads_with_what_ran_today(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        digest = render_digest(brief, report_url="https://example.test/brief")
        assert digest.startswith("RUNNERS TODAY")
        assert "$RUNNER 8.0x" in digest
        assert "https://example.test/brief" in digest
        assert "Data, not advice" in digest
        # A digest, not the whole report.
        assert "SCREENING FUNNEL" not in digest
        assert len(digest) < len(render_markdown(brief))
        assert all(len(chunk) <= 3900 for chunk in render_telegram(brief))
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_full_report_is_still_available_on_demand(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        chunks = render_telegram(brief, digest=False)
        assert any("SCREENING FUNNEL" in chunk for chunk in chunks)
    finally:
        ledger.close()


def test_archive_prune_keeps_the_replay_window_and_drops_the_rest(tmp_path):
    ledger = Ledger(tmp_path / "prune.db")
    try:
        for age_days in (1, 5, 20, 60):
            ledger.archive_response(
                method="GET",
                endpoint=f"https://api.test/{age_days}",
                request_params=None,
                request_body=None,
                status=200,
                response_body="{}" * 100,
                captured_at=NOW - timedelta(days=age_days),
            )
        assert ledger.stats()["raw_responses"] == 4
        removed = ledger.prune_archive(NOW, 14)
        assert removed == 2
        remaining = ledger.db.execute("SELECT endpoint FROM raw_responses ORDER BY endpoint").fetchall()
        assert [row["endpoint"] for row in remaining] == ["https://api.test/1", "https://api.test/5"]
    finally:
        ledger.close()


def test_pool_and_locker_accounts_are_not_counted_as_holder_concentration():
    """Summing RugCheck's raw topHolders counted the AMM vault as a whale."""
    from brief.sources.rugcheck import parse_report

    payload = {
        "mint": "MINTC",
        "mintAuthority": "",
        "freezeAuthority": "",
        "markets": [{"lp": {"lpLockedPct": 100}}],
        "knownAccounts": {
            "POOLVAULT": {"name": "Pump Fun AMM", "type": "AMM"},
            "LOCKACCT": {"name": "Locker", "type": "LOCKER"},
        },
        "topHolders": [
            {"address": "POOLVAULT", "owner": "POOLOWNER", "pct": 45.0},
            {"address": "LOCKACCT", "owner": "LOCKOWNER", "pct": 15.0},
            {"address": "1nc1nerator11111111111111111111111111111111", "owner": "B", "pct": 10.0},
            {"address": "WHALE1", "owner": "W1", "pct": 8.0},
            {"address": "WHALE2", "owner": "W2", "pct": 6.0},
        ],
        "risks": [],
    }
    report = parse_report("MINTC", payload)
    assert report.top10_pct == 14.0  # only the two real holders
    assert "POOLVAULT" in report.excluded_accounts


def test_genuinely_concentrated_token_is_still_rejected():
    from brief.sources.rugcheck import parse_report

    payload = {
        "mint": "MINTW",
        "markets": [],
        "knownAccounts": {"POOLVAULT": {"type": "AMM"}},
        "topHolders": [
            {"address": "POOLVAULT", "owner": "P", "pct": 5.0},
            *[{"address": f"W{i}", "owner": f"O{i}", "pct": 8.0} for i in range(10)],
        ],
        "risks": [],
    }
    assert parse_report("MINTW", payload).top10_pct == 80.0


def test_established_runner_is_not_rejected_by_the_launch_wash_trading_limit(tmp_path):
    """A hot older name turns its pool over many times; the launch limit killed it."""
    from brief.models import Enrichment, SafetyReport, TokenSnapshot, TransactionWindow
    from brief.screen import safety_gate, volume_liquidity_limit

    settings = build_settings(tmp_path / "ratio")

    def token(age_hours: float) -> TokenSnapshot:
        return TokenSnapshot(
            mint="MINTR", symbol="RUN", name="Runner", chain_id="solana",
            pair_address="PAIRR", url="", price_usd=1.0, market_cap=1_000_000,
            liquidity_usd=100_000, volume_24h=5_000_000, volume_6h=2_000_000,
            price_change_24h=80, price_change_6h=30,
            pair_created_at=NOW - timedelta(hours=age_hours),
            txns_6h=TransactionWindow(400, 200),
        )

    fresh, established = token(3), token(72)
    assert volume_liquidity_limit(fresh, settings, NOW) == 25.0
    assert volume_liquidity_limit(established, settings, NOW) == 75.0

    report = SafetyReport("MINTR", True, True, 100.0, 12.0)
    fresh_reasons, _ = safety_gate(fresh, report, Enrichment(), settings, NOW)
    established_reasons, _ = safety_gate(established, report, Enrichment(), settings, NOW)
    assert any("volume/liquidity" in reason for reason in fresh_reasons)
    assert not any("volume/liquidity" in reason for reason in established_reasons)


def test_large_cap_with_no_real_tape_is_rejected(tmp_path):
    """A thin float prices a few buys into a big notional cap; that is not strength."""
    from brief.models import TokenSnapshot, TransactionWindow
    from brief.screen import hard_filter

    settings = build_settings(tmp_path / "thin")
    thin = TokenSnapshot(
        mint="MINTT", symbol="THIN", name="Thin", chain_id="solana", pair_address="P",
        url="", price_usd=1.0, market_cap=5_300_000, liquidity_usd=60_000,
        volume_24h=78_000, volume_6h=40_000, price_change_24h=16682, price_change_6h=900,
        pair_created_at=NOW - timedelta(minutes=30), txns_6h=TransactionWindow(350, 13),
    )
    reasons = hard_filter(thin, settings, NOW)
    assert any("0.01x market cap" in reason for reason in reasons)


def test_compressed_archive_replays_identically(tmp_path):
    payload = '{"pairs": [{"baseToken": {"symbol": "ALPHA"}}]}'
    ledger = Ledger(tmp_path / "packed.db", compress_archive=True)
    try:
        ledger.archive_response(
            method="GET", endpoint="https://api.test/pairs", request_params={"a": "1"},
            request_body=None, status=200, response_body=payload, captured_at=NOW,
        )
        stored = ledger.db.execute("SELECT typeof(response_body) FROM raw_responses").fetchone()[0]
        assert stored == "blob"
        replayed = ledger.replay_response(
            NOW.date().isoformat(), "GET", "https://api.test/pairs", {"a": "1"}
        )
        assert replayed == {"pairs": [{"baseToken": {"symbol": "ALPHA"}}]}
    finally:
        ledger.close()


def test_compacting_old_plaintext_rows_preserves_replay(tmp_path):
    """The existing gigabyte-scale archive must shrink without losing a date."""
    payload = '{"value": ' + '"x"' * 500 + "}"
    ledger = Ledger(tmp_path / "legacy.db", compress_archive=False)
    try:
        ledger.archive_response(
            method="GET", endpoint="https://api.test/legacy", request_params=None,
            request_body=None, status=200, response_body=payload, captured_at=NOW,
        )
        assert ledger.db.execute("SELECT typeof(response_body) FROM raw_responses").fetchone()[0] == "text"
        assert ledger.compact_archive() == 1
        assert ledger.db.execute("SELECT typeof(response_body) FROM raw_responses").fetchone()[0] == "blob"
        assert ledger.compact_archive() == 0
        raw = ledger.replay_response(NOW.date().isoformat(), "GET", "https://api.test/legacy")
        assert raw == payload
        packed = ledger.db.execute("SELECT LENGTH(response_body) FROM raw_responses").fetchone()[0]
        assert packed < len(payload)
    finally:
        ledger.close()


def test_prune_is_a_no_op_when_retention_is_disabled(tmp_path):
    ledger = Ledger(tmp_path / "keep.db")
    try:
        ledger.archive_response(
            method="GET", endpoint="https://api.test/old", request_params=None,
            request_body=None, status=200, response_body="{}",
            captured_at=NOW - timedelta(days=400),
        )
        assert ledger.prune_archive(NOW, 0) == 0
        assert ledger.stats()["raw_responses"] == 1
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_committed_run_prunes_the_archive(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        ledger.archive_response(
            method="GET", endpoint="https://api.test/stale", request_params=None,
            request_body=None, status=200, response_body="{}",
            captured_at=NOW - timedelta(days=90),
        )
        await build_brief(mover_settings, ledger, commit=True, now=NOW)
        stale = ledger.db.execute(
            "SELECT COUNT(*) FROM raw_responses WHERE endpoint='https://api.test/stale'"
        ).fetchone()[0]
        assert stale == 0
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_empty_day_states_the_result_rather_than_looking_broken(settings, monkeypatch):
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW + timedelta(days=400))
        assert not brief.new_and_moving and not brief.movers and not brief.ctos
        html = render_html(brief)
        assert "NOTHING CLEARED THE BAR TODAY. AN EMPTY BRIEF IS A RESULT, NOT AN OUTAGE." in html
        digest = render_digest(brief)
        assert "Nothing ran today" in digest
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_age_is_a_wall_that_no_multiple_buys_past(mover_settings):
    """The record is about coins that launched and worked.

    Inside the wall the bar eases with age: a pair in its first day only has to
    be up a third, one in its second has to have done a real multiple. Nothing
    older gets in at all, however large the number beside it.
    """
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        runners = {c.token.symbol: c for c in brief.runners}
        # 30 hours old and up 8x: past its first day, so it needs the multiple.
        assert "RUNNER" in runners
        assert runners["RUNNER"].run_multiple == pytest.approx(8.0)
        # Same age, only up 64%: not a multiple, so it is not the day's news.
        assert "MOVER" not in runners
        # ...though it is still an editorial mover, the two views are independent.
        assert "MOVER" in {c.token.symbol for c in brief.movers}
        assert all(c.signals.age_hours <= 36 for c in brief.runners)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_journal_records_a_fresh_launch_that_ran(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        runners = {c.token.symbol: c for c in brief.runners}
        assert "FRESHRUN" in runners, "a 5h-old pair up 60% is exactly today's news"
        assert runners["FRESHRUN"].signals.age_hours < 24
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_a_flag_labels_the_row_instead_of_hiding_the_coin(mover_settings):
    """The old behaviour dropped a coin for one failed check. It now shows up flagged."""
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        faded = next(c for c in brief.runners if c.token.symbol == "FADED")
        assert faded.faded_from_peak == -35
        assert any("fading" in label for label in faded.risk_labels)
        html = render_html(brief)
        assert "$FADED" in html, "a coin that ran then dumped is still part of the day"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_bundled_supply_is_kept_internal_not_published(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert "BUNDLED" not in {c.token.symbol for c in brief.runners}
        blocked = next(c for c in brief.blocked_runners if c.token.symbol == "BUNDLED")
        assert any("bundled supply" in label for label in blocked.risk_labels)
        html = render_html(brief)
        assert "Ran, but disqualified" not in html
        assert "$BUNDLED" not in html
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_runners_are_grouped_by_shared_lore(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert brief.lore_groups, "several dog-meta runners should cluster"
        biggest = max(brief.lore_groups.values(), key=len)
        assert len(biggest) >= 2
        assert all(c.lore for c in biggest)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_journal_ignores_the_novelty_rules(mover_settings):
    """A journal is a record. A coin that ran yesterday and again today is news twice."""
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        ledger.record_feature("MINTR", "RUNNER", 700000, 1, NOW - timedelta(days=1))
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert "RUNNER" in {c.token.symbol for c in brief.runners}
    finally:
        ledger.close()


def test_kol_buy_detection_reads_balance_deltas():
    """Balance deltas work across every DEX; parsing swap instructions would not."""
    from brief.kol import _mints_bought

    transaction = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {"owner": "WALLET", "mint": "MINTX", "uiTokenAmount": {"uiAmount": 0}},
                {"owner": "WALLET", "mint": "MINTSOLD", "uiTokenAmount": {"uiAmount": 500}},
            ],
            "postTokenBalances": [
                {"owner": "WALLET", "mint": "MINTX", "uiTokenAmount": {"uiAmount": 1200}},
                {"owner": "WALLET", "mint": "MINTSOLD", "uiTokenAmount": {"uiAmount": 100}},
                {"owner": "OTHER", "mint": "MINTZ", "uiTokenAmount": {"uiAmount": 999}},
            ],
        }
    }
    assert _mints_bought(transaction, "WALLET") == {"MINTX"}


def test_failed_transactions_are_not_buys():
    from brief.kol import _mints_bought

    transaction = {
        "meta": {
            "err": {"InstructionError": [0, "Custom"]},
            "preTokenBalances": [],
            "postTokenBalances": [
                {"owner": "WALLET", "mint": "MINTX", "uiTokenAmount": {"uiAmount": 5}}
            ],
        }
    }
    assert _mints_bought(transaction, "WALLET") == set()


def test_kol_wallet_list_accepts_plain_addresses_and_named_tables(tmp_path):
    from brief.kol import configured_wallets

    settings = build_settings(
        tmp_path / "kol",
        "movers.json",
        extra='\n[kol]\nenabled = true\nwallets = [ "AAAAbbbbCCCCddddEEEE", { address = "ZZZZyyyyXXXXwwww", name = "Cupsey" } ]\n',
    )
    wallets = configured_wallets(settings)
    assert wallets["ZZZZyyyyXXXXwwww"] == "Cupsey"
    assert wallets["AAAAbbbbCCCCddddEEEE"] == "AAAA...EEEE"


@pytest.mark.asyncio
async def test_kol_tracking_is_dormant_with_no_wallets(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        status = next(s for s in brief.source_statuses if s.name == "KOL wallet flow")
        assert not status.available
        assert "no wallets configured" in status.detail
        assert all(not c.kol_buyers for c in brief.runners)
    finally:
        ledger.close()


def _fake(symbol, change24h, turnover, mcap=1_000_000.0):
    from brief.models import Candidate, Enrichment, SafetyReport, Signals, TokenSnapshot
    token = TokenSnapshot(
        mint="MINT" + symbol, symbol=symbol, name=symbol, chain_id="solana", pair_address="P",
        url="", price_usd=1.0, market_cap=mcap, liquidity_usd=mcap * 0.4,
        volume_24h=mcap * turnover, volume_6h=0, price_change_24h=change24h,
        price_change_6h=0, pair_created_at=NOW - timedelta(days=30),
    )
    signals = Signals(
        turnover=turnover, acceleration=0, buy_imbalance_1h=None, buy_imbalance_6h=None,
        liquidity_depth=0.4, holder_growth_24h=None, maker_quality=None, age_hours=720.0,
    )
    return Candidate(token=token, signals=signals, safety=SafetyReport("m"), enrichment=Enrichment())


def test_a_spectacular_move_with_no_trading_is_rejected_as_a_data_artifact(tmp_path):
    """A feed reported 16,226,272% on a coin that was flat and barely traded."""
    from brief.journal import implausible_run

    settings = build_settings(tmp_path / "plaus")
    assert implausible_run(_fake("ANTFUN", 16_226_272.0, 0.02, 80_000_000.0), settings)
    assert implausible_run(_fake("QUIET", 1500.0, 0.03), settings)


def test_a_real_run_backed_by_volume_is_kept(tmp_path):
    from brief.journal import implausible_run

    settings = build_settings(tmp_path / "plaus2")
    # $FOMO: +6,988% on 8.15x turnover, which the tape corroborates.
    assert not implausible_run(_fake("FOMO", 6988.0, 8.15, 380_000.0), settings)
    # A modest mover well under the corroboration threshold is untouched.
    assert not implausible_run(_fake("MILD", 60.0, 0.05), settings)


def _tape(symbol, *, mcap, vol24, vol6, liq, trades6, buys6, boosts=0, reuse=0):
    """A candidate shaped from one row of a real report."""
    from brief.models import Candidate, Enrichment, SafetyReport, Signals, TokenSnapshot, TransactionWindow
    sells = max(0, trades6 - buys6)
    token = TokenSnapshot(
        mint="MINT" + symbol, symbol=symbol, name=symbol, chain_id="solana", pair_address="P",
        url="", price_usd=1.0, market_cap=mcap, liquidity_usd=liq, volume_24h=vol24,
        volume_6h=vol6, price_change_24h=500.0, price_change_6h=100.0,
        pair_created_at=NOW - timedelta(hours=12), active_boosts=boosts,
        txns_6h=TransactionWindow(buys6, sells),
    )
    signals = Signals(
        turnover=vol24 / mcap if mcap else 0, acceleration=0, buy_imbalance_1h=None,
        buy_imbalance_6h=(buys6 / trades6 if trades6 else None), liquidity_depth=liq / mcap,
        holder_growth_24h=None, maker_quality=None, age_hours=12.0,
    )
    c = Candidate(token=token, signals=signals, safety=SafetyReport("m"), enrichment=Enrichment())
    c.recycled_label_count = reuse
    return c


def test_runner_universe_scales_liquidity_and_holders_by_verified_peak(tmp_path):
    from brief.journal import runner_universe_reasons
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "banded-runner-gates")
    settings.values["journal"].update({
        "runner_universe_min_runner_score": 40,
        "publisher_max_top10_pct": 30,
        "gmgn_max_dev_team_hold_rate": 0.15,
        "min_liquidity_by_chain": {"solana": 40_000},
        "min_liquidity_by_peak_market_cap": [
            {"max_peak_market_cap": 500_000, "solana": 12_000},
            {"max_peak_market_cap": 1_000_000, "solana": 20_000},
            {"max_peak_market_cap": 10_000_000, "solana": 40_000},
            {"max_peak_market_cap": 0, "solana": 100_000},
        ],
        "min_holders_by_peak_market_cap": [
            {"max_peak_market_cap": 500_000, "value": 300},
            {"max_peak_market_cap": 1_000_000, "value": 500},
            {"max_peak_market_cap": 10_000_000, "value": 1_000},
            {"max_peak_market_cap": 0, "value": 2_500},
        ],
    })

    coin = _tape("EARLY", mcap=300_000, vol24=500_000, vol6=200_000, liq=12_500,
                 trades6=2_000, buys6=1_050)
    coin.peak_market_cap = 300_000
    coin.scores["runner"] = 60
    coin.safety = SafetyReport(
        coin.token.mint,
        mint_authority_renounced=True,
        freeze_authority_disabled=True,
        lp_locked_or_burned_pct=100,
        top10_pct=20,
        holder_count=350,
        source="rugcheck",
    )
    coin.provider_evidence["gmgn"] = {
        "isHoneypot": 0,
        "washTrading": False,
        "devTeamHoldRate": 0.02,
        "renownedTrustedCount": 1,
        "exactTraderHistoryChecked": True,
    }
    assert runner_universe_reasons(coin, settings) == []

    coin.token.liquidity_usd = 11_999
    assert any("$12,000" in reason for reason in runner_universe_reasons(coin, settings))
    coin.token.liquidity_usd = 12_500
    coin.safety.holder_count = 299
    assert any("floor of 300" in reason for reason in runner_universe_reasons(coin, settings))


def test_large_runner_requires_large_pool_and_holder_base(tmp_path):
    from brief.journal import runner_universe_reasons
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "large-runner-gates")
    settings.values["journal"].update({
        "runner_universe_min_runner_score": 40,
        "min_liquidity_by_peak_market_cap": [
            {"max_peak_market_cap": 500_000, "solana": 12_000},
            {"max_peak_market_cap": 1_000_000, "solana": 20_000},
            {"max_peak_market_cap": 10_000_000, "solana": 40_000},
            {"max_peak_market_cap": 0, "solana": 100_000},
        ],
        "min_holders_by_peak_market_cap": [
            {"max_peak_market_cap": 500_000, "value": 300},
            {"max_peak_market_cap": 1_000_000, "value": 500},
            {"max_peak_market_cap": 10_000_000, "value": 1_000},
            {"max_peak_market_cap": 0, "value": 2_500},
        ],
    })
    coin = _tape("LARGE", mcap=20_000_000, vol24=5_000_000, vol6=1_000_000,
                 liq=99_999, trades6=3_000, buys6=1_600)
    coin.peak_market_cap = 20_000_000
    coin.scores["runner"] = 75
    coin.safety = SafetyReport(
        coin.token.mint,
        mint_authority_renounced=True,
        freeze_authority_disabled=True,
        lp_locked_or_burned_pct=100,
        top10_pct=15,
        holder_count=2_499,
        source="rugcheck",
    )
    coin.provider_evidence["gmgn"] = {
        "isHoneypot": 0,
        "washTrading": False,
        "devTeamHoldRate": 0.01,
    }
    reasons = runner_universe_reasons(coin, settings)
    assert any("$100,000" in reason for reason in reasons)
    assert any("floor of 2,500" in reason for reason in reasons)


def test_wash_traded_pool_is_removed(tmp_path):
    """$SPCX traded $42M against a $35k pool: 1,192x its own liquidity."""
    from brief.journal import inorganic_reasons

    settings = build_settings(tmp_path / "org1")
    reasons = inorganic_reasons(
        _tape("SPCX", mcap=945_000, vol24=42_263_000, vol6=10_000_000, liq=35_000,
              trades6=19_273, buys6=10_600), settings)
    assert any("wash-trading shape" in r for r in reasons)


def test_speed_alone_does_not_condemn_a_coin(tmp_path):
    """A normal hot launch printed 219 trades a minute with a $47 average.

    An earlier rule rejected anything above 40 a minute and threw away real
    runners, every one of which traded larger size than that reference.
    """
    from brief.journal import inorganic_reasons

    settings = build_settings(tmp_path / "cadence")
    # $GUNICORN: 284 trades a minute, but $89 of real money behind each one.
    fast = _tape("GUNICORN", mcap=900_000, vol24=9_000_000, vol6=9_120_000, liq=74_000,
                 trades6=102_240, buys6=52_140)
    fast.token.txns_24h = fast.token.txns_6h
    assert inorganic_reasons(fast, settings) == []


def test_dust_at_speed_is_still_removed(tmp_path):
    """Volume and cadence only damn a coin together: many prints, no money."""
    from brief.journal import inorganic_reasons

    settings = build_settings(tmp_path / "dust")
    spam = _tape("SPAM", mcap=400_000, vol24=300_000, vol6=90_000, liq=50_000,
                 trades6=30_000, buys6=15_400)
    spam.token.txns_24h = spam.token.txns_6h
    assert any("spam rather than demand" in r for r in inorganic_reasons(spam, settings))


def test_neither_a_boost_nor_a_reused_ticker_removes_a_coin(tmp_path):
    """Both are shown on the row instead.

    A boost is Dexscreener's ad product and honest teams buy one. A shared
    ticker is common enough that dropping every coin with one threw away real
    runners; the client asked for safety, not novelty.
    """
    from brief.journal import inorganic_reasons, risk_labels

    settings = build_settings(tmp_path / "org3")
    c = _tape("BABYANSEM", mcap=518_000, vol24=503_000, vol6=400_000, liq=54_000,
              trades6=10_363, buys6=6_200, boosts=30, reuse=2)
    c.token.txns_24h = c.token.txns_6h
    assert inorganic_reasons(c, settings) == []
    labels = risk_labels(c, settings, NOW)
    assert any("also used by 2 other recent mint(s)" in f for f in labels)


def test_a_coin_with_almost_no_holders_is_removed(tmp_path):
    """"Real holders" is the ask; a few wallets is not a distribution."""
    from brief.journal import inorganic_reasons
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "holders")
    c = _tape("THIN", mcap=400_000, vol24=600_000, vol6=200_000, liq=50_000,
              trades6=1_200, buys6=640)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=40)
    assert any("only 40 holders" in r for r in inorganic_reasons(c, settings))

    c.safety = SafetyReport("m", holder_count=1_764)
    assert inorganic_reasons(c, settings) == []


def test_the_pump_and_die_shape_is_removed(tmp_path):
    """An insta-x that stopped trading hours ago is not today's market."""
    from brief.journal import inorganic_reasons, risk_labels

    settings = build_settings(tmp_path / "dead")
    dead = _tape("DEADCAT", mcap=400_000, vol24=2_000_000, vol6=40_000, liq=60_000,
                 trades6=400, buys6=210)
    dead.token.txns_24h = dead.token.txns_6h
    dead.token.price_change_24h = 1400.0        # a 15x on paper
    reasons = inorganic_reasons(dead, settings)
    assert any("the move is over" in r for r in reasons)

    alive = _tape("ALIVE", mcap=400_000, vol24=2_000_000, vol6=700_000, liq=60_000,
                  trades6=1_800, buys6=950)
    alive.token.txns_24h = alive.token.txns_6h
    alive.token.price_change_24h = 1400.0
    assert not any("the move is over" in r for r in inorganic_reasons(alive, settings))

    # A server-filtered GMGN organic result has already cleared volume,
    # liquidity, holders, distribution and wash checks. Cooling after its run
    # is recap context, not evidence that the earlier move was fake.
    dead.provider_evidence["gmgn"] = {"organicQualified": True}
    assert not any("the move is over" in r for r in inorganic_reasons(dead, settings))
    assert any("peaked earlier" in r for r in risk_labels(dead, settings, NOW))


def test_a_token_rugcheck_calls_rugged_is_removed(tmp_path):
    from brief.journal import rug_or_bundle
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "rugged")
    c = _tape("GONE", mcap=400_000, vol24=600_000, vol6=200_000, liq=50_000,
              trades6=1_200, buys6=640)
    c.safety = SafetyReport("m", rugged=True)
    assert any("rugged" in r for r in rug_or_bundle(c, settings))


def test_a_book_with_no_sellers_is_removed(tmp_path):
    from brief.journal import inorganic_reasons

    settings = build_settings(tmp_path / "org4")
    reasons = inorganic_reasons(
        _tape("GBACK", mcap=299_000, vol24=208_000, vol6=60_000, liq=39_000,
              trades6=1_647, buys6=1_416), settings)
    assert any("manufactured book" in r for r in reasons)


def test_a_genuine_runner_survives_every_organic_check(tmp_path):
    """$FOMO: real volume, human cadence, two-sided book, original ticker."""
    from brief.journal import inorganic_reasons, risk_labels

    settings = build_settings(tmp_path / "org5")
    assert inorganic_reasons(
        _tape("FOMO", mcap=380_000, vol24=3_098_000, vol6=900_000, liq=83_000,
              trades6=2_467, buys6=1_233), settings) == []


def test_same_funder_holder_pack_is_removed_even_with_low_nominal_top10(tmp_path):
    """Nominal concentration can lie when one entity splits across wallets."""
    from brief.journal import inorganic_reasons
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "clustered")
    c = _tape("PACK", mcap=2_400_000, vol24=8_000_000, vol6=2_500_000, liq=140_000,
              trades6=12_000, buys6=6_200)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=4_000, top10_pct=12.0, lp_locked_or_burned_pct=100.0)
    c.warnings.append(
        "same-funder holder cluster: 20 traced top holders hold 4.8% funded by FS4RY…Ne42 inside 120m; effective top10 after clustering 31.0%"
    )

    reasons = inorganic_reasons(c, settings)
    assert any("same-funder holder cluster" in reason for reason in reasons)


def test_publisher_can_require_a_tracked_kol_wallet_touch(tmp_path):
    from brief.journal import publisher_quality_reasons
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "kol-gate")
    settings.values.setdefault("journal", {})["require_kol_trade_for_publish"] = True
    settings.values["journal"]["min_kol_trades_for_publish"] = 1
    c = _tape("KOLMISS", mcap=600_000, vol24=2_500_000, vol6=800_000, liq=100_000,
              trades6=2_000, buys6=1_050)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=2_500, top10_pct=12.0, lp_locked_or_burned_pct=100.0)
    c.kol_wallets_scanned = 100

    assert any("no tracked KOL wallet traded" in r for r in publisher_quality_reasons(c, settings, NOW))

    c.kol_buyers = ["Chairman"]
    assert not any("no tracked KOL wallet traded" in r for r in publisher_quality_reasons(c, settings, NOW))


def test_kol_flow_lane_promotes_wallet_discovered_runners(tmp_path):
    from brief.engine import _add_kol_flow_runners
    from brief.models import SafetyReport
    from brief.scoring import score_candidate

    settings = build_settings(tmp_path / "kol-lane")
    settings.values.setdefault("kol", {}).update({
        "runner_lane_enabled": True,
        "runner_lane_max": 10,
        "runner_min_market_cap": 200_000.0,
        "runner_min_volume_24h": 100_000.0,
        "runner_max_age_hours": 24.0,
        "runner_min_buyers": 5,
        "runner_min_participants": 5,
        "runner_min_realised_sol": 5.0,
        "runner_min_sol_spent": 0.0,
        "runner_require_positive_realised": True,
        "runner_max_manipulation": 75.0,
    })
    c = _tape("OMO", mcap=900_000, vol24=1_600_000, vol6=500_000, liq=95_000,
              trades6=2_200, buys6=1_120)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=2_800, top10_pct=14.0, lp_locked_or_burned_pct=100.0)
    c.kol_wallets_scanned = 120
    c.kol_buyers = ["Wugi", "Chairman", "Pain", "Gasp", "Cupsey"]
    c.kol_holders = ["Wugi", "Chairman"]
    c.kol_sellers = ["Pain", "Gasp"]
    c.kol_realised_sol = 22.0
    c.kol_sol_spent = 18.0
    score_candidate(c, settings)

    runners, blocked, count = _add_kol_flow_runners([], [], [c], settings, NOW)

    assert count == 1
    assert runners[0].track == "KOL"
    assert "$OMO" in runners[0].read
    assert any("KOL-flow runner" in label for label in runners[0].risk_labels)
    assert blocked == []


def test_kol_flow_lane_cannot_override_missing_contract_security(tmp_path):
    from brief.engine import _add_kol_flow_runners
    from brief.models import SafetyReport
    from brief.scoring import score_candidate

    settings = build_settings(tmp_path / "kol-lane-security")
    settings.values.setdefault("kol", {}).update({
        "runner_lane_enabled": True,
        "runner_lane_max": 10,
        "runner_min_market_cap": 250_000.0,
        "runner_min_volume_24h": 250_000.0,
        "runner_max_age_hours": 24.0,
        "runner_min_buyers": 5,
        "runner_min_participants": 5,
        "runner_min_realised_sol": 5.0,
        "runner_require_positive_realised": True,
        "runner_require_safety": True,
        "runner_require_holder_count": True,
    })
    settings.values["journal"].update({
        "block_on_missing_safety_data": True,
        "require_holder_count": True,
        "fresh_min_holders": 300,
        "min_lp_locked_pct": 90.0,
        "publisher_max_top10_pct": 30.0,
        "fresh_publisher_max_top10_pct": 30.0,
        "min_liquidity_by_chain": {"solana": 40_000.0},
    })
    c = _tape("UNVERIFIED", mcap=900_000, vol24=1_600_000, vol6=500_000, liq=95_000,
              trades6=2_200, buys6=1_120)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport(
        "m",
        holder_count=2_800,
        top10_pct=14.0,
        lp_locked_or_burned_pct=100.0,
        source="rugcheck",
    )
    c.kol_wallets_scanned = 120
    c.kol_buyers = ["Wugi", "Chairman", "Pain", "Gasp", "Cupsey"]
    c.kol_holders = ["Wugi", "Chairman"]
    c.kol_sellers = ["Pain", "Gasp"]
    c.kol_realised_sol = 22.0
    score_candidate(c, settings)

    runners, blocked, promoted = _add_kol_flow_runners([], [], [c], settings, NOW)

    assert promoted == 0
    assert runners == []
    assert any(
        "mint authority/contract mintability" in reason
        for candidate in blocked for reason in candidate.risk_labels
    )


def test_kol_flow_lane_rejects_thin_old_or_losing_wallet_flow(tmp_path):
    from brief.engine import _add_kol_flow_runners
    from brief.models import SafetyReport
    from brief.scoring import score_candidate

    settings = build_settings(tmp_path / "kol-lane-reject")
    settings.values.setdefault("kol", {}).update({
        "runner_lane_enabled": True,
        "runner_lane_max": 10,
        "runner_min_market_cap": 200_000.0,
        "runner_min_volume_24h": 100_000.0,
        "runner_max_age_hours": 24.0,
        "runner_min_buyers": 5,
        "runner_min_participants": 5,
        "runner_min_realised_sol": 5.0,
        "runner_require_positive_realised": True,
        "runner_max_manipulation": 75.0,
    })

    def candidate(symbol: str):
        c = _tape(symbol, mcap=900_000, vol24=1_600_000, vol6=500_000, liq=95_000,
                  trades6=2_200, buys6=1_120)
        c.token.txns_24h = c.token.txns_6h
        c.safety = SafetyReport("m", holder_count=2_800, top10_pct=14.0, lp_locked_or_burned_pct=100.0)
        c.kol_wallets_scanned = 120
        return c

    one_loser = candidate("ONELOSS")
    one_loser.kol_buyers = ["West"]
    one_loser.kol_sellers = ["West"]
    one_loser.kol_realised_sol = -12.0

    four_kols = candidate("FOUR")
    four_kols.kol_buyers = ["Wugi", "Chairman", "Pain", "Gasp"]
    four_kols.kol_holders = ["Wugi"]
    four_kols.kol_realised_sol = 40.0

    old = candidate("OLDKOL")
    old.kol_buyers = ["Wugi", "Chairman", "Pain", "Gasp", "Cupsey"]
    old.kol_holders = ["Wugi"]
    old.kol_realised_sol = 40.0
    old.signals.age_hours = 25.0
    old.token.pair_created_at = NOW - timedelta(hours=25)

    for c in (one_loser, four_kols, old):
        score_candidate(c, settings)

    runners, blocked, count = _add_kol_flow_runners([], [], [one_loser, four_kols, old], settings, NOW)

    assert count == 0
    assert runners == []
    assert blocked == []


def test_kol_flow_lane_can_promote_faded_positive_kol_tape(tmp_path):
    from brief.engine import _add_kol_flow_runners
    from brief.models import SafetyReport
    from brief.scoring import score_candidate

    settings = build_settings(tmp_path / "kol-lane-faded")
    settings.values.setdefault("kol", {}).update({
        "runner_lane_enabled": True,
        "runner_lane_max": 10,
        "runner_min_market_cap": 200_000.0,
        "runner_min_volume_24h": 100_000.0,
        "runner_max_age_hours": 24.0,
        "runner_min_buyers": 5,
        "runner_min_participants": 5,
        "runner_min_realised_sol": 5.0,
        "runner_require_positive_realised": True,
        "runner_allow_faded_below_floor": True,
        "runner_max_manipulation": 75.0,
    })
    c = _tape("CONK", mcap=30_000, vol24=120_000, vol6=40_000, liq=5_000,
              trades6=350, buys6=180)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=600, top10_pct=18.0, lp_locked_or_burned_pct=100.0)
    c.kol_wallets_scanned = 120
    c.kol_buyers = ["Apex", "Casino", "Doji", "Latuche", "Pain"]
    c.kol_sellers = ["Apex", "Casino", "Doji", "Latuche", "Pain"]
    c.kol_holders = ["Doji", "Pain"]
    c.kol_realised_sol = 42.8
    c.kol_sol_spent = 153.4
    c.observed_peak_market_cap = 260_000.0
    score_candidate(c, settings)

    runners, blocked, count = _add_kol_flow_runners([], [], [c], settings, NOW)

    assert count == 1
    assert runners[0].track == "KOL"
    assert any("KOL-tape runner" in label for label in runners[0].risk_labels)
    assert blocked == []


def test_profitable_kol_scalp_without_200k_peak_is_not_a_runner(tmp_path):
    from brief.engine import _add_kol_flow_runners
    from brief.models import SafetyReport
    from brief.scoring import score_candidate

    settings = build_settings(tmp_path / "kol-small-scalp")
    settings.values.setdefault("kol", {}).update({
        "runner_lane_enabled": True,
        "runner_min_market_cap": 200_000.0,
        "runner_min_volume_24h": 100_000.0,
        "runner_max_age_hours": 24.0,
        "runner_min_buyers": 2,
        "runner_min_participants": 2,
        "runner_min_realised_sol": 1.0,
        "runner_require_positive_realised": True,
        "runner_allow_faded_below_floor": True,
    })
    c = _tape("SMALL", mcap=18_000, vol24=120_000, vol6=40_000, liq=8_000,
              trades6=900, buys6=470)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=900, top10_pct=18.0, lp_locked_or_burned_pct=100.0)
    c.kol_buyers = ["Apex", "Pain", "Doji"]
    c.kol_sellers = ["Apex", "Pain", "Doji"]
    c.kol_realised_sol = 20.0
    c.kol_sol_spent = 50.0
    c.observed_peak_market_cap = 90_000.0
    score_candidate(c, settings)

    runners, _, count = _add_kol_flow_runners([], [], [c], settings, NOW)

    assert count == 0
    assert runners == []


def test_faded_runner_uses_verified_peak_for_turnover_safety(tmp_path):
    from brief.engine import _add_kol_flow_runners
    from brief.models import SafetyReport
    from brief.scoring import score_candidate

    settings = build_settings(tmp_path / "kol-faded-turnover")
    settings.values.setdefault("kol", {}).update({
        "runner_lane_enabled": True,
        "runner_min_market_cap": 200_000.0,
        "runner_min_volume_24h": 100_000.0,
        "runner_max_age_hours": 24.0,
        "runner_min_buyers": 2,
        "runner_min_participants": 2,
        "runner_min_realised_sol": 1.0,
        "runner_require_positive_realised": True,
        "runner_max_peak_turnover": 10.0,
        "runner_max_manipulation": 75.0,
    })
    c = _tape("FADED", mcap=7_000, vol24=1_100_000, vol6=50_000, liq=6_000,
              trades6=1_800, buys6=920)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=1_200, top10_pct=16.0, lp_locked_or_burned_pct=100.0)
    c.kol_buyers = ["Doji", "Pain", "Gasp"]
    c.kol_sellers = list(c.kol_buyers)
    c.kol_realised_sol = 12.0
    c.observed_peak_market_cap = 380_000.0
    score_candidate(c, settings)

    runners, _, count = _add_kol_flow_runners([], [], [c], settings, NOW)

    assert count == 1
    assert runners[0].token.symbol == "FADED"


def test_open_kol_conviction_is_not_mislabeled_as_negative_realised_pnl(tmp_path):
    from brief.engine import _add_kol_flow_runners
    from brief.models import KolWalletFlow, SafetyReport
    from brief.scoring import score_candidate

    settings = build_settings(tmp_path / "kol-open-conviction")
    settings.values.setdefault("kol", {}).update({
        "runner_lane_enabled": True,
        "runner_min_market_cap": 200_000.0,
        "runner_min_volume_24h": 100_000.0,
        "runner_max_age_hours": 24.0,
        "runner_min_buyers": 2,
        "runner_min_participants": 2,
        "runner_min_realised_sol": 1.0,
        "runner_require_positive_realised": True,
        "runner_open_min_buyers": 5,
        "runner_open_min_holders": 2,
        "runner_max_manipulation": 75.0,
    })
    c = _tape("OPEN", mcap=600_000, vol24=1_500_000, vol6=600_000, liq=90_000,
              trades6=2_100, buys6=1_080)
    c.token.txns_24h = c.token.txns_6h
    c.safety = SafetyReport("m", holder_count=3_000, top10_pct=14.0, lp_locked_or_burned_pct=100.0)
    c.kol_buyers = ["A", "B", "C", "D", "E", "F"]
    c.kol_holders = ["A", "B", "C"]
    c.kol_sellers = ["D", "E"]
    c.kol_realised_sol = -40.0
    c.kol_flows = [
        KolWalletFlow("D", bought=True, sold=True, realised_sol=3.0),
        KolWalletFlow("E", bought=True, sold=True, realised_sol=-1.0),
        KolWalletFlow("A", bought=True, holding=True, realised_sol=-20.0),
    ]
    score_candidate(c, settings)

    runners, _, count = _add_kol_flow_runners([], [], [c], settings, NOW)

    assert count == 1
    assert runners[0].token.symbol == "OPEN"


def _publisher_settings(settings):
    journal = settings.values.setdefault("journal", {})
    journal.update({
        "require_socials": True,
        "exclude_recycled": True,
        "require_holder_count": True,
        "min_holders": 1000,
        "min_trades_24h": 1000,
        "min_liquidity": 40_000.0,
        "min_lp_locked_pct": 80.0,
        "publisher_max_top10_pct": 25.0,
        "extreme_multiple": 10.0,
        "extreme_min_volume_24h": 1_000_000.0,
        "extreme_min_holders": 5_000,
        "extreme_min_turnover": 0.75,
        "extreme_min_recent_volume_share": 0.15,
        "min_organic_confirmations": 6,
        "organic_min_buy_ratio": 0.42,
        "organic_max_buy_ratio": 0.72,
    })
    return settings


def test_publisher_gate_blocks_insta_x_with_weak_organic_proof(tmp_path):
    """A 30x+ print is not enough when holders, turnover and context lag."""
    from brief.journal import publisher_quality_reasons
    from brief.models import SafetyReport

    settings = _publisher_settings(build_settings(tmp_path / "publisher-weak"))
    suspect = _tape("TNOS", mcap=1_640_000, vol24=326_000, vol6=180_000, liq=80_000,
                    trades6=1_527, buys6=780)
    suspect.token.txns_24h = suspect.token.txns_6h
    suspect.token.price_change_24h = 3400.0
    suspect.run_multiple = 35.0
    suspect.safety = SafetyReport(
        "m",
        holder_count=1_527,
        lp_locked_or_burned_pct=100.0,
        top10_pct=1.4,
    )

    reasons = publisher_quality_reasons(suspect, settings, NOW)
    assert any("move on only $326,000 volume" in r for r in reasons)
    assert any("move on only 0.20x turnover" in r for r in reasons)
    assert any("move with 1,527 holders" in r for r in reasons)
    assert any("no linked social context" in r for r in reasons)


def test_publisher_gate_keeps_extreme_runner_when_tape_confirms_it(tmp_path):
    """A violent move survives only when independent signals agree."""
    from brief.journal import publisher_quality_reasons
    from brief.models import SafetyReport

    settings = _publisher_settings(build_settings(tmp_path / "publisher-strong"))
    fomo = _tape("FOMO", mcap=380_000, vol24=3_098_000, vol6=900_000, liq=83_000,
                 trades6=2_467, buys6=1_233)
    fomo.token.txns_24h = fomo.token.txns_6h
    fomo.token.price_change_24h = 6988.0
    fomo.token.socials = [{"type": "twitter", "url": "https://x.com/fomo"}]
    fomo.run_multiple = 70.88
    fomo.safety = SafetyReport(
        "m",
        holder_count=6_200,
        lp_locked_or_burned_pct=100.0,
        top10_pct=19.0,
    )

    assert publisher_quality_reasons(fomo, settings, NOW) == []


def test_a_big_move_no_tracked_wallet_touched_is_flagged(tmp_path):
    """These wallets exist because they find moves like this. Silence is odd."""
    from brief.journal import risk_labels, untouched_by_tracked_wallets

    settings = build_settings(tmp_path / "untouched")
    ran = _tape("GHOST", mcap=400_000, vol24=900_000, vol6=250_000, liq=60_000,
                trades6=1_200, buys6=640)
    ran.token.price_change_24h = 1100.0          # a 12x
    ran.run_multiple = 12.0
    ran.kol_wallets_scanned = 66

    assert untouched_by_tracked_wallets(ran, settings)
    assert any("not one tracked wallet touched it" in f
               for f in risk_labels(ran, settings, NOW))


def test_a_big_solana_runner_without_wallet_heat_is_blocked_for_publishing(tmp_path):
    """For the public recap, silence from a wide wallet net is not clean."""
    from brief.journal import missing_wallet_confirmation

    settings = build_settings(tmp_path / "missing-wallet-confirmation")
    settings.values.setdefault("journal", {})["require_wallet_touch_for_publish"] = True
    settings.values["journal"]["wallet_touch_required_above_multiple"] = 1.8
    settings.values["journal"]["wallet_touch_required_min_mcap"] = 200_000

    ran = _tape("GHOST", mcap=400_000, vol24=900_000, vol6=250_000, liq=60_000,
                trades6=1_200, buys6=640)
    ran.token.chain_id = "solana"
    ran.run_multiple = 2.4
    ran.kol_wallets_scanned = 150

    assert missing_wallet_confirmation(ran, settings)

    ran.kol_buyers = ["Wugi"]
    assert missing_wallet_confirmation(ran, settings)

    ran.kol_buyers = ["Wugi", "Cupsey"]
    assert not missing_wallet_confirmation(ran, settings)


def test_a_runner_the_wallets_traded_is_not_flagged(tmp_path):
    """Selling counts: the position may have been opened before the window."""
    from brief.journal import untouched_by_tracked_wallets

    settings = build_settings(tmp_path / "touched")
    ran = _tape("MOLLIE", mcap=187_000, vol24=1_085_000, vol6=300_000, liq=37_000,
                trades6=1_245, buys6=535)
    ran.run_multiple = 15.4
    ran.kol_wallets_scanned = 66

    ran.kol_buyers = ["Wugi"]
    assert not untouched_by_tracked_wallets(ran, settings)

    ran.kol_buyers = []
    ran.kol_realised_sol = 42.6          # closed a position opened earlier
    assert not untouched_by_tracked_wallets(ran, settings)


def test_a_modest_mover_is_not_expected_to_draw_smart_money(tmp_path):
    from brief.journal import untouched_by_tracked_wallets

    settings = build_settings(tmp_path / "modest")
    mild = _tape("MILD", mcap=400_000, vol24=200_000, vol6=60_000, liq=60_000,
                 trades6=900, buys6=470)
    mild.run_multiple = 2.0
    mild.kol_wallets_scanned = 66
    assert not untouched_by_tracked_wallets(mild, settings)


def test_silence_proves_nothing_when_the_scan_did_not_run(tmp_path):
    """A Helius outage must not accuse every runner of being ignored."""
    from brief.journal import untouched_by_tracked_wallets

    settings = build_settings(tmp_path / "noscan")
    ran = _tape("GHOST", mcap=400_000, vol24=900_000, vol6=250_000, liq=60_000,
                trades6=1_200, buys6=640)
    ran.run_multiple = 12.0
    ran.kol_wallets_scanned = 0
    assert not untouched_by_tracked_wallets(ran, settings)


def test_only_configured_venues_enter_the_record(tmp_path):
    """PumpSwap-only is a real preference, and it silently drops Raydium names."""
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "venue")
    settings.values.setdefault("journal", {})["venues"] = ["pumpswap"]

    coin = _tape("BROS", mcap=391_000, vol24=600_000, vol6=200_000, liq=60_000,
                 trades6=1_600, buys6=850)
    coin.token.price_change_24h = 1150.0
    coin.token.pair_created_at = NOW - timedelta(hours=20)

    coin.token.dex_id = "pumpswap"
    assert belongs_in_journal(coin, settings, NOW)

    # $FOMO, the biggest runner of a measured day, had migrated to Raydium.
    coin.token.dex_id = "raydium"
    assert not belongs_in_journal(coin, settings, NOW)

    settings.values["journal"]["venues"] = []
    assert belongs_in_journal(coin, settings, NOW), "empty list means every venue"


def test_goplus_maps_evm_dangers_onto_the_same_report_shape(tmp_path):
    """RugCheck is Solana-only; EVM needs the same questions answered."""
    from brief.sources.goplus import parse_security

    payload = {
        "token_symbol": "EVMCOIN", "holder_count": "4821",
        "is_mintable": "0", "transfer_pausable": "0", "is_blacklisted": "0",
        "cannot_sell_all": "0", "is_honeypot": "0",
        "buy_tax": "0", "sell_tax": "0.02",
        "creator_address": "0xabc",
        "lp_holders": [
            {"address": "0x000000000000000000000000000000000000dead", "percent": "0.91", "is_locked": 0},
            {"address": "0xpool", "percent": "0.09", "is_locked": 1},
        ],
        "holders": [
            {"address": "0xpair", "percent": "0.40", "tag": "Uniswap V3"},
            {"address": "0xw1", "percent": "0.06", "tag": ""},
            {"address": "0xw2", "percent": "0.04", "tag": ""},
        ],
    }
    r = parse_security("0xEVM", payload)
    assert r.holder_count == 4821
    assert r.mint_authority_renounced is True
    assert r.freeze_authority_disabled is True
    assert not r.rugged
    # The burn and the locked pool both count as locked liquidity.
    assert r.lp_locked_or_burned_pct == pytest.approx(100.0, abs=0.5)
    # The Uniswap pair is not a whale, so only the two real wallets count.
    assert r.top10_pct == pytest.approx(10.0, abs=0.5)


def test_a_honeypot_is_treated_as_already_rugged(tmp_path):
    from brief.journal import rug_or_bundle
    from brief.sources.goplus import parse_security

    settings = build_settings(tmp_path / "honey")
    c = _tape("TRAP", mcap=400_000, vol24=600_000, vol6=200_000, liq=50_000,
              trades6=1_200, buys6=640)
    c.safety = parse_security("0xTRAP", {"is_honeypot": "1", "holder_count": "900"})
    assert any("rugged" in r for r in rug_or_bundle(c, settings))


def test_a_sale_that_can_be_taxed_or_blocked_removes_the_coin(tmp_path):
    """Ways to lose money that no Solana check would ever look for.

    A third of every sale taken as tax, or ownership that can be clawed back
    after being renounced, is the same class of problem as a rug: the holder
    cannot get their money out.
    """
    from brief.journal import rug_or_bundle
    from brief.sources.goplus import parse_security

    settings = build_settings(tmp_path / "tax")
    c = _tape("TAXED", mcap=400_000, vol24=600_000, vol6=200_000, liq=50_000,
              trades6=1_200, buys6=640)
    c.token.txns_24h = c.token.txns_6h
    c.safety = parse_security("0xTAX", {
        "holder_count": "5000", "sell_tax": "0.35", "can_take_back_ownership": "1",
    })
    reasons = rug_or_bundle(c, settings)
    assert any("sell tax" in r for r in reasons)
    assert any("taken back" in r for r in reasons)


def test_venue_rules_are_per_chain(tmp_path):
    """PumpSwap means nothing on Ethereum."""
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "venue2")
    settings.values["journal"]["venues"] = {"solana": ["pumpswap"]}

    coin = _tape("X", mcap=391_000, vol24=600_000, vol6=200_000, liq=60_000,
                 trades6=1_600, buys6=850)
    coin.token.price_change_24h = 1150.0
    coin.token.pair_created_at = NOW - timedelta(hours=20)

    coin.token.chain_id, coin.token.dex_id = "solana", "raydium"
    assert not belongs_in_journal(coin, settings, NOW), "Solana is restricted to PumpSwap"

    coin.token.chain_id, coin.token.dex_id = "ethereum", "uniswap"
    assert belongs_in_journal(coin, settings, NOW), "no rule for ethereum means every venue"


def test_solana_wallet_silence_is_not_held_against_other_chains(tmp_path):
    """The tracked wallets are Solana wallets; they cannot buy on Base."""
    from brief.journal import untouched_by_tracked_wallets

    settings = build_settings(tmp_path / "chainkol")
    c = _tape("BASECOIN", mcap=400_000, vol24=900_000, vol6=250_000, liq=60_000,
              trades6=1_200, buys6=640)
    c.run_multiple = 12.0
    c.token.chain_id = "base"
    c.kol_wallets_scanned = 0          # what the engine records off Solana
    assert not untouched_by_tracked_wallets(c, settings)


def test_a_coin_past_the_age_wall_is_excluded_however_large_the_move(tmp_path):
    """A 50x on a week-old pair is not what the report is for."""
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "wall")
    coin = _tape("OLDRUN", mcap=900_000, vol24=3_000_000, vol6=900_000, liq=120_000,
                 trades6=2_400, buys6=1_260)
    coin.token.price_change_24h = 4900.0        # a 50x

    coin.token.pair_created_at = NOW - timedelta(hours=30)
    assert belongs_in_journal(coin, settings, NOW), "inside the wall a multiple counts"

    coin.token.pair_created_at = NOW - timedelta(hours=37)
    assert not belongs_in_journal(coin, settings, NOW), "an hour past the wall is out"

    coin.token.pair_created_at = NOW - timedelta(days=7)
    assert not belongs_in_journal(coin, settings, NOW)


def test_a_pair_with_no_known_creation_time_is_excluded(tmp_path):
    """The point of the wall is that everything inside it provably is new."""
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "unknownage")
    coin = _tape("NOAGE", mcap=400_000, vol24=900_000, vol6=300_000, liq=60_000,
                 trades6=1_400, buys6=740)
    coin.token.price_change_24h = 900.0
    coin.token.pair_created_at = None
    assert not belongs_in_journal(coin, settings, NOW)

    # Production disables the absolute age ceiling so measured in-window peaks
    # can survive after a fade.  Missing creation time must still not make an
    # unverified daily-change candidate eligible.
    settings.values["journal"]["max_age_hours"] = 0
    assert not belongs_in_journal(coin, settings, NOW)


def test_the_first_day_bar_is_lower_than_the_second(tmp_path):
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "bands")
    coin = _tape("BAND", mcap=400_000, vol24=900_000, vol6=300_000, liq=60_000,
                 trades6=1_400, buys6=740)
    coin.token.price_change_24h = 40.0          # up 40%, not a multiple

    coin.token.pair_created_at = NOW - timedelta(hours=6)
    assert belongs_in_journal(coin, settings, NOW), "a fresh pair only needs to run"

    coin.token.pair_created_at = NOW - timedelta(hours=30)
    assert not belongs_in_journal(coin, settings, NOW), "past a day it needs a multiple"


def test_production_runner_window_keeps_30h_launches_but_old_coins_need_a_real_move(tmp_path):
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "production-bands")
    settings.values["journal"].update({
        "max_age_hours": 0,
        "fresh_window_hours": 30,
        "peak_market_cap_floor": 250_000,
        "older_than_a_day_multiple": 0,
        "min_daily_change_pct": 50,
    })
    coin = _tape("WINDOW", mcap=400_000, vol24=900_000, vol6=300_000, liq=60_000,
                 trades6=1_400, buys6=740)
    coin.token.price_change_24h = 0
    coin.token.pair_created_at = NOW - timedelta(hours=29.9)
    assert belongs_in_journal(coin, settings, NOW), "a launch just inside 30h belongs"

    coin.token.pair_created_at = NOW - timedelta(hours=31)
    coin.token.price_change_24h = 49
    assert not belongs_in_journal(coin, settings, NOW), "an older static name cannot ride its market cap"

    coin.provider_evidence["gmgn"] = {
        "kline24hCandleCount": 24,
        "kline24hPeakFromOpenPct": 55,
    }
    assert belongs_in_journal(coin, settings, NOW), "GMGN candles can prove an intraday run that faded"

    coin.start_market_cap = 100_000
    coin.peak_market_cap = 400_000
    coin.provider_evidence["gmgn"]["kline24hPeakFromOpenPct"] = 39
    assert not belongs_in_journal(coin, settings, NOW), "GMGN candles override a conflicting local cap estimate"


def test_old_coin_new_ath_exception_requires_an_in_window_gmgn_candle(tmp_path):
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "ath-band")
    settings.values["journal"].update({
        "max_age_hours": 0,
        "fresh_window_hours": 30,
        "peak_market_cap_floor": 250_000,
        "older_than_a_day_multiple": 0,
        "min_daily_change_pct": 50,
        "new_ath_tolerance_pct": 2,
        "min_new_ath_move_pct": 10,
    })
    coin = _tape("ATH", mcap=990_000, vol24=900_000, vol6=300_000, liq=80_000,
                 trades6=1_400, buys6=740)
    coin.token.pair_created_at = NOW - timedelta(days=10)
    coin.token.price_change_24h = 20
    coin.provider_evidence["gmgn"] = {"athMarketCap": 1_000_000}
    assert not belongs_in_journal(coin, settings, NOW), "a stale lifetime ATH is not today's event"

    coin.provider_evidence["gmgn"].update({
            "kline24hPeakMarketCap": 995_000,
            "kline24hMarketCapVerified": True,
        "kline24hPeakFromOpenPct": 20,
        "kline24hPeakAt": NOW.isoformat(),
    })
    assert belongs_in_journal(coin, settings, NOW), "the trailing-day candle verifies the ATH"


def test_production_peak_floor_and_size_adjusted_old_coin_moves(tmp_path):
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "tiered-old-runner-bands")
    settings.values["journal"].update({
        "max_age_hours": 0,
        "fresh_window_hours": 30,
        "peak_market_cap_floor": 250_000,
        "older_than_a_day_multiple": 0,
        "old_coin_age_hours": 720,
        "old_coin_micro_cap_ceiling": 500_000,
        "old_coin_low_cap_ceiling": 1_000_000,
        "old_coin_small_cap_ceiling": 10_000_000,
        "old_coin_large_cap_floor": 20_000_000,
        "old_coin_micro_min_change_pct": 150,
        "old_coin_low_min_change_pct": 100,
        "old_coin_small_min_change_pct": 75,
        "old_coin_mid_min_change_pct": 50,
        "old_coin_large_min_change_pct": 30,
        "allow_old_new_ath_exception": False,
    })

    def old_coin(symbol: str, peak: float, move: float):
        coin = _tape(symbol, mcap=peak * 0.8, vol24=2_000_000, vol6=600_000,
                     liq=120_000, trades6=2_000, buys6=1_050)
        coin.token.pair_created_at = NOW - timedelta(days=45)
        coin.peak_market_cap = peak
        coin.provider_evidence["gmgn"] = {
            "kline24hCandleCount": 24,
            "kline24hPeakMarketCap": peak,
            "kline24hPeakFromOpenPct": move,
            "kline24hChangePct": move - 10,
        }
        return coin

    assert not belongs_in_journal(old_coin("MICROMISS", 400_000, 149.9), settings, NOW)
    assert belongs_in_journal(old_coin("MICROPASS", 400_000, 150), settings, NOW)
    assert not belongs_in_journal(old_coin("LOWMISS", 750_000, 99.9), settings, NOW)
    assert belongs_in_journal(old_coin("LOWPASS", 750_000, 100), settings, NOW)
    assert not belongs_in_journal(old_coin("SMALLMISS", 9_000_000, 74.9), settings, NOW)
    assert belongs_in_journal(old_coin("SMALLPASS", 9_000_000, 75), settings, NOW)
    assert not belongs_in_journal(old_coin("MIDMISS", 15_000_000, 49.9), settings, NOW)
    assert belongs_in_journal(old_coin("MIDPASS", 15_000_000, 50), settings, NOW)
    assert not belongs_in_journal(old_coin("LARGEMISS", 25_000_000, 29.9), settings, NOW)
    assert belongs_in_journal(old_coin("LARGEPASS", 25_000_000, 30), settings, NOW)

    below_floor = old_coin("BELOWFLOOR", 249_999, 500)
    assert not belongs_in_journal(below_floor, settings, NOW)


def test_recent_coin_uses_verified_daily_kline_peak_for_one_million_floor(tmp_path):
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "recent-million-peak")
    settings.values["journal"].update({
        "max_age_hours": 0,
        "fresh_window_hours": 30,
        "peak_market_cap_floor": 1_000_000,
        "old_coin_age_hours": 720,
    })
    coin = _tape("LAPEACE", mcap=258_000, vol24=2_900_000, vol6=240_000,
                 liq=45_000, trades6=4_700, buys6=2_500)
    coin.token.pair_created_at = NOW - timedelta(days=4)
    coin.peak_market_cap = 470_000
    coin.provider_evidence["gmgn"] = {
        "athMarketCap": 1_145_000,
        "kline24hCandleCount": 24,
        "kline24hPeakMarketCap": 1_145_000,
        "kline24hPeakFromOpenPct": 337,
    }
    assert belongs_in_journal(coin, settings, NOW)

    # A provider ATH also fixes a high that occurred between our local hourly
    # snapshots. Because this coin is under 30 days old, no legacy-move gate is
    # required after the $1M size requirement is established.
    coin.provider_evidence["gmgn"].pop("kline24hPeakMarketCap")
    coin.peak_market_cap = 470_000
    assert belongs_in_journal(coin, settings, NOW)


def test_kol_publish_gate_applies_to_every_supported_chain(tmp_path):
    from brief.journal import publisher_quality_reasons
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "all-chain-kol")
    settings.values.setdefault("journal", {})["require_kol_trade_for_publish"] = True
    settings.values["journal"]["min_kol_trades_for_publish"] = 1
    coin = _tape("BASEMISS", mcap=600_000, vol24=2_500_000, vol6=800_000, liq=100_000,
                 trades6=2_000, buys6=1_050)
    coin.token.chain_id = "base"
    coin.safety = SafetyReport("m", holder_count=2_500, top10_pct=12.0, lp_locked_or_burned_pct=100.0)
    coin.provider_evidence["gmgn"] = {"kolCount": 0}
    assert any("no tracked KOL wallet traded" in r for r in publisher_quality_reasons(coin, settings, NOW))

    coin.provider_evidence["gmgn"]["kolCount"] = 2
    assert not any("no tracked KOL wallet traded" in r for r in publisher_quality_reasons(coin, settings, NOW))


def test_gmgn_direct_manipulation_evidence_is_a_hard_stop(tmp_path):
    from brief.journal import rug_or_bundle

    settings = build_settings(tmp_path / "gmgn-risk")
    coin = _tape("BUNDLE", mcap=900_000, vol24=2_000_000, vol6=600_000,
                 liq=120_000, trades6=2_000, buys6=1_050)

    coin.provider_evidence["gmgn"] = {"washTrading": True}
    assert "GMGN detected wash trading" in rug_or_bundle(coin, settings)

    # Historical launch bundling is context, not a hard stop. Current holder
    # and developer concentration are enforced separately.
    coin.provider_evidence["gmgn"] = {"bundlerRate": 0.41}
    assert rug_or_bundle(coin, settings) == []

    coin.provider_evidence["gmgn"] = {"insiderRate": 0.35}
    assert any("insider/rat-trader" in reason for reason in rug_or_bundle(coin, settings))

    # A high heuristic rug ratio is context, not an automatic rejection; the
    # GMGN data itself shows established community coins can score high here.
    coin.provider_evidence["gmgn"] = {"rugRatio": 0.95}
    assert rug_or_bundle(coin, settings) == []

    coin.provider_evidence["gmgn"] = {"isHoneypot": 1}
    assert "GMGN marks the contract as a honeypot" in rug_or_bundle(coin, settings)

    coin.safety.top10_pct = None
    coin.provider_evidence["gmgn"] = {"top10Pct": 62.2}
    assert any("top 10 circulating wallets hold 62%" in reason for reason in rug_or_bundle(coin, settings))


def test_traded_kol_conviction_and_extreme_volume_override_only_moderate_concentration(tmp_path):
    from brief.journal import publisher_quality_reasons, rug_or_bundle, runner_universe_reasons
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "conviction-override")
    coin = _tape("FIH", mcap=300_000, vol24=950_000, vol6=400_000,
                 liq=46_000, trades6=3_000, buys6=1_700)
    coin.safety = SafetyReport(
        "m", holder_count=1_455, top10_pct=36.25, lp_locked_or_burned_pct=100.0,
    )
    coin.provider_evidence["gmgn"] = {
        "exactTraderHistoryChecked": True,
        "renownedTrustedCount": 4,
        "devTeamHoldRate": 0.278,
        "insiderRate": 0.0,
        "washTrading": False,
        "isHoneypot": 0,
    }

    assert not any("dev-team" in r for r in rug_or_bundle(coin, settings))
    assert not any("top 10" in r or "dev team" in r for r in runner_universe_reasons(coin, settings))
    assert not any("top 10 hold" in r for r in publisher_quality_reasons(coin, settings, NOW))

    # Strong flow is not permission to waive confirmed contract danger.
    coin.provider_evidence["gmgn"]["isHoneypot"] = 1
    assert any("honeypot" in r for r in rug_or_bundle(coin, settings))

    # Nor does it excuse extreme ownership control.
    coin.provider_evidence["gmgn"]["isHoneypot"] = 0
    coin.provider_evidence["gmgn"]["devTeamHoldRate"] = 0.31
    assert any("dev-team" in r for r in rug_or_bundle(coin, settings))


def test_old_launch_bundle_is_context_while_current_concentration_is_enforced(tmp_path):
    from brief.journal import risk_labels, rug_or_bundle
    from brief.models import SafetyReport

    settings = build_settings(tmp_path / "redistributed-bundle")
    settings.values["journal"].update({
        "allow_redistributed_launch_bundles": True,
        "bundle_redistribution_min_age_hours": 72,
        "bundle_redistribution_min_holders": 5_000,
        "bundle_redistribution_max_top10_pct": 20,
        "bundle_redistribution_min_lp_pct": 90,
        "bundle_redistribution_min_kol": 5,
        "bundle_redistribution_min_liquidity": 100_000,
        "bundle_redistribution_min_volume_24h": 1_000_000,
    })
    coin = _tape("REDIST", mcap=13_000_000, vol24=33_000_000, vol6=5_000_000,
                 liq=1_600_000, trades6=35_000, buys6=21_000)
    coin.token.pair_created_at = NOW - timedelta(days=8)
    coin.signals.age_hours = 8 * 24
    coin.safety = SafetyReport(
        "m", holder_count=55_000, top10_pct=12.0, lp_locked_or_burned_pct=98.0,
    )
    coin.provider_evidence["gmgn"] = {
        "bundlerRate": 0.41,
        "insiderRate": 0.0,
        "devTeamHoldRate": 0.0,
        "kolCount": 34,
    }

    assert not any("bundled launch flow" in reason for reason in rug_or_bundle(coin, settings))
    assert any("launch bundle" in label for label in risk_labels(coin, settings, NOW))

    # The launch metric itself remains informational even when ownership later
    # concentrates; the independent top-holder gate blocks the token instead.
    coin.safety.top10_pct = 35.0
    assert not any("bundled launch flow" in reason for reason in rug_or_bundle(coin, settings))


def test_gmgn_market_fees_are_not_misread_as_transfer_tax(tmp_path):
    from brief.journal import risk_labels, rug_or_bundle

    settings = build_settings(tmp_path / "explicit-tax")
    settings.values["journal"].update({
        "fee_check_chains": ["bsc"],
        "max_total_fee_pct": 3.0,
        "caution_total_fee_pct": 1.0,
    })
    coin = _tape("FEES", mcap=900_000, vol24=2_000_000, vol6=600_000,
                 liq=120_000, trades6=2_000, buys6=1_050)
    coin.token.chain_id = "bsc"
    coin.provider_evidence["gmgn"] = {
        # These are market-activity fees, not token tax.
        "totalFee": 33.4,
        "tradeFee": 28.1,
        "buyTax": 0.03,
        "sellTax": 0.03,
    }
    assert not any("taxed token" in reason for reason in rug_or_bundle(coin, settings))
    assert "3.0% tax on every trade" in risk_labels(coin, settings, NOW)

    coin.provider_evidence["gmgn"]["sellTax"] = 0.031
    assert any("taxed token: 3.1%" in reason for reason in rug_or_bundle(coin, settings))


def test_full_holder_enrichment_overrides_a_partial_safety_count(tmp_path):
    from brief.journal import inorganic_reasons
    from brief.models import Enrichment, SafetyReport

    settings = build_settings(tmp_path / "holder-precedence")
    coin = _tape("HOLDERS", mcap=900_000, vol24=2_000_000, vol6=600_000,
                 liq=120_000, trades6=2_000, buys6=1_050)
    coin.safety = SafetyReport("m", holder_count=104)
    coin.enrichment = Enrichment(holder_count=1_329, source="gmgn")
    assert not any("holders" in reason for reason in inorganic_reasons(coin, settings))


def test_verified_old_runner_survives_a_negative_close(tmp_path):
    from brief.journal import belongs_in_journal

    settings = build_settings(tmp_path / "faded-old-runner")
    settings.values["journal"].update({
        "max_age_hours": 0,
        "fresh_window_hours": 30,
        "peak_market_cap_floor": 250_000,
        "older_than_a_day_multiple": 0,
        "min_daily_change_pct": 50,
        "older_min_live_change_pct": 0,
    })
    coin = _tape("FADED", mcap=700_000, vol24=2_000_000, vol6=600_000,
                 liq=120_000, trades6=2_000, buys6=1_050)
    coin.token.pair_created_at = NOW - timedelta(days=8)
    coin.token.price_change_24h = -64
    coin.provider_evidence["gmgn"] = {
        "kline24hCandleCount": 24,
        "kline24hPeakFromOpenPct": 81,
        "kline24hChangePct": -64,
    }
    assert belongs_in_journal(coin, settings, NOW)
