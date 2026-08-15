from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from brief.engine import build_brief
from brief.ledger import Ledger, iso
from brief.render.html import render_html
from brief.render.markdown import render_markdown
from brief.render.telegram import render_digest, render_telegram
from tests.conftest import build_settings


NOW = datetime(2026, 8, 6, 6, 45, tzinfo=ZoneInfo("Europe/Paris"))


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
async def test_journal_records_an_old_coin_doing_a_big_multiple(mover_settings):
    """Old coins earn their place with a 5x, not by drifting up a few percent."""
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        runners = {c.token.symbol: c for c in brief.runners}
        assert "RUNNER" in runners
        assert runners["RUNNER"].run_multiple == pytest.approx(8.0)
        # +64% on a five-day-old pair is not a 5x, so it stays out.
        assert "MOVER" not in runners
        # ...but it is still an editorial mover, the two views are independent.
        assert "MOVER" in {c.token.symbol for c in brief.movers}
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
async def test_bundled_supply_is_the_one_thing_that_removes_a_runner(mover_settings):
    ledger = Ledger(mover_settings.path("run", "database_path"))
    try:
        brief = await build_brief(mover_settings, ledger, commit=False, now=NOW)
        assert "BUNDLED" not in {c.token.symbol for c in brief.runners}
        blocked = next(c for c in brief.blocked_runners if c.token.symbol == "BUNDLED")
        assert any("bundled supply" in label for label in blocked.risk_labels)
        html = render_html(brief)
        assert "Ran, but disqualified" in html
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
