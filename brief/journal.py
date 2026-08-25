"""The daily journal: what actually ran today, and how safe each runner is.

The editorial tracks answer "which few names are worth a close look". This
module answers a different question -- "what happened today" -- and it answers
it for every coin that ran, not for a shortlist. The distinction matters: a coin
that failed one concentration check is still part of the day's record, so it is
labelled rather than hidden. Only the conditions that make a coin uninvestable
at any price -- a live mint authority, unlocked liquidity, a bundled supply --
keep it out entirely.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from brief.config import Settings
from brief.models import Candidate, TokenSnapshot
from brief.sources.gmgn import transfer_tax_pct


def _age_hours(token: TokenSnapshot, now: datetime) -> float | None:
    if token.pair_created_at is None:
        return None
    return max(0.0, (now.astimezone(timezone.utc) - token.pair_created_at).total_seconds() / 3600)


def run_multiple(token: TokenSnapshot) -> float:
    """How many times the coin is up over 24 hours. +400% is a 5x."""
    return 1.0 + token.price_change_24h / 100.0


def verified_window_multiple(candidate: Candidate) -> float:
    """Best measured multiple inside the report window, never lifetime ATH.

    The daily close can hide a runner that peaked and faded. Prefer the stored
    hourly peak or GMGN's trailing-day candles when they have a usable opening
    baseline, and retain the aggregate 24h move as the final fallback.
    """
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    multiples = [max(1.0, run_multiple(candidate.token))]
    kline_change = float(gmgn.get("kline24hPeakFromOpenPct") or 0)
    if kline_change > 0:
        multiples.append(1.0 + kline_change / 100.0)
    start = float(candidate.start_market_cap or 0)
    if start > 0:
        peak = max(
            float(candidate.peak_market_cap or 0),
            float(candidate.observed_peak_market_cap or 0),
            float(gmgn.get("kline24hPeakMarketCap") or 0),
            float(candidate.token.market_cap or 0),
        )
        if peak > 0:
            multiples.append(peak / start)
    return max(multiples)


def limit_runner_board(runners: list[Candidate], settings: Settings) -> list[Candidate]:
    """Keep the normal edition compact and reserve overflow for real multiples.

    Thirteen to fifteen names is the expected editorial size, but it is not a
    quota. The first fifteen verified runners remain ranked normally. Slots
    sixteen through twenty are available only to additional coins that made
    the configured exceptional in-window multiple.
    """
    section = settings.section("journal")
    maximum = int(section.get("max_runners", 0) or 0)
    if maximum <= 0:
        return list(runners)
    standard = min(
        maximum,
        int(section.get("publication_standard_coins", 15) or 15),
    )
    if len(runners) <= standard:
        return list(runners)
    minimum_multiple = float(
        section.get("publication_overflow_min_multiple", 5.0) or 5.0
    )
    overflow = [
        candidate for candidate in runners[standard:]
        if verified_window_multiple(candidate) >= minimum_multiple
    ]
    return [*runners[:standard], *overflow[: max(0, maximum - standard)]]


def implausible_run(candidate: Candidate, settings: Settings) -> bool:
    """A move the tape does not corroborate is a data artifact, not a run.

    Feeds occasionally report a nonsense 24-hour change when a pool is reseeded
    or the price denominator moves, and the number is spectacular enough to top
    every ranking. The tell is that nothing else agrees with it: no turnover, a
    flat last hour. Believing it once puts a fake 162,000x on the broadcast.
    """
    section = settings.section("journal")
    multiple = run_multiple(candidate.token)

    ceiling = float(section.get("max_credible_multiple", 1000))
    if ceiling and multiple > ceiling:
        return True

    big = float(section.get("corroborate_above_multiple", 10))
    floor = float(section.get("min_turnover_for_big_run", 0.15))
    # Turnover is volume over market cap, so with no cap on record it reads as
    # zero however hard the coin traded. Only a computed zero is corroboration
    # of nothing; a missing denominator is not.
    if multiple >= big and candidate.token.market_cap > 0 and candidate.signals.turnover < floor:
        return True
    return False


def publisher_is_fresh(candidate: Candidate, settings: Settings, now: datetime | None = None) -> bool:
    """Whether the publisher floors should be read at their fresh-launch level.

    Every floor in this file was calibrated against established Solana coins.
    Applied unchanged to a twelve-hour-old launch they reject it for being
    twelve hours old: it cannot have a thousand holders yet, its pool is thin
    enough that a genuine run churns many multiples of it, and its top ten
    still hold what the bonding curve gave them. Fresh coins are judged on the
    same evidence, at the level that evidence actually reaches by then.
    """
    age = _age_hours(candidate.token, now) if now is not None else candidate.signals.age_hours
    return age is not None and age <= float(settings.section("journal").get("fresh_gate_hours", 36) or 36)


def kol_traders(candidate: Candidate) -> set[str]:
    """Tracked wallets that actually traded the coin.

    Holding is not evidence. A launch can airdrop supply into well-known
    wallets precisely so that screens like this one report smart money in the
    coin, and the wallet's owner never touched it. Only a buy or a sell is a
    decision, so only those count towards qualification. Holdings stay visible
    on the row as context.
    """
    return set(candidate.kol_buyers) | set(candidate.kol_sellers)


def kol_trade_count(candidate: Candidate) -> int:
    """How many tracked wallets bought or sold it, from our tape or GMGN's.

    The single definition. Qualification, the payload and the email all read
    this, so a coin can never be admitted on evidence the reader is not shown.
    """
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    flow = gmgn.get("walletFlow", {}) or {}
    return max(
        len(kol_traders(candidate)),
        len(set(flow.get("kolBuyerNames", [])) | set(flow.get("kolSellerNames", []))),
        int(gmgn.get("kolCount") or 0),
    )


def kol_touch_required(candidate: Candidate, settings: Settings) -> bool:
    """Whether a tracked-wallet touch is mandatory for this coin's chain.

    Our KOL and smart-money coverage is a Solana list. On Solana a runner that
    no tracked wallet went near is genuinely odd and worth holding back. Off
    Solana the same silence says nothing about the coin and everything about
    who we follow, so requiring a touch there would reject Base and BNB runners
    for a gap in our own data.
    """
    section = settings.section("journal")
    if not bool(section.get("require_kol_trade_for_publish", False)):
        return False
    chains = [str(c).strip().lower() for c in (section.get("require_kol_trade_chains", []) or [])]
    return not chains or candidate.token.chain_id.lower() in chains


def six_hour_volume_known(token: TokenSnapshot) -> bool:
    """Whether the six-hour window was actually reported for this pair.

    Dexscreener returns no h6 bucket at all for the EVM chains, so the field
    arrives as zero and every Base and BNB coin looks like a move that ended
    hours ago. A pair trading two million dollars across the day did not trade
    exactly nothing in the last six hours; that is a missing number, not a dead
    market, and it must not be read as one.
    """
    return not (token.volume_6h <= 0 < token.volume_24h)


def latin_symbol(symbol: str) -> bool:
    """Whether a minimum character count means anything for this ticker.

    A two-character CJK ticker is a whole word -- the film coin that ran to $77m
    was written with exactly two. Counting characters is a Latin-alphabet
    assumption, so it is only applied to Latin-alphabet tickers.
    """
    return all(ord(character) < 0x2E80 for character in str(symbol or ""))


def chain_min_liquidity(section: Settings, chain: str, default: float) -> float:
    """Pool depth expectations differ per chain; Solana's floor is not universal."""
    table = section.get("min_liquidity_by_chain") or {}
    if isinstance(table, dict):
        override = table.get(str(chain or "").lower())
        if override is not None:
            return float(override)
    return float(section.get("min_liquidity", default) or default)


def _floor(section: Settings, key: str, default: float, *, fresh: bool) -> float:
    """The fresh variant of a floor when one is configured, else the standard."""
    if fresh:
        relaxed = section.get(f"fresh_{key}")
        if relaxed is not None:
            return float(relaxed)
    return float(section.get(key, default) or default)


def belongs_in_journal(candidate: Candidate, settings: Settings, now: datetime) -> bool:
    """Record anything that verifiably crossed the daily runner floor.

    New launches can use GMGN ATH because their lifetime is wholly inside the
    report window. Older tokens need either a measured local 24h move, a
    reconstructed chart peak, or the configured daily change. Current market
    cap is never required to remain above the floor: a runner that died is still
    part of the day's tape.
    """
    section = settings.section("journal")
    token = candidate.token
    age = _age_hours(token, now)

    # Venue rules are per chain: PumpSwap means nothing on Ethereum, and the
    # EVM chains spread their liquidity across many routers.
    configured = section.get("venues") or {}
    if isinstance(configured, dict):
        allowed = configured.get(token.chain_id.lower(), [])
    else:
        allowed = configured
    venues = [str(v).strip().lower() for v in (allowed or []) if str(v).strip()]
    if venues and token.dex_id.lower() not in venues:
        return False

    ceiling = float(section.get("max_age_hours", 36))
    if ceiling > 0 and (age is None or age > ceiling):
        return False

    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    kline_peak_cap = float(gmgn.get("kline24hPeakMarketCap") or 0)
    lifetime_ath = float(gmgn.get("athMarketCap") or 0)
    window_peak = max(
        float(candidate.peak_market_cap or 0),
        float(candidate.observed_peak_market_cap or 0),
        float(token.market_cap or 0),
        kline_peak_cap,
    )
    # The size gate is lifetime ATH >= $1M. For established coins that ATH is
    # only eligibility; the trailing-day candles below must still prove a new
    # move. Keeping those concepts separate fixes runners that peaked between
    # local hourly snapshots without resurrecting flat legacy coins.
    peak = max(window_peak, lifetime_ath)
    peak_floor = float(section.get("peak_market_cap_floor", 250_000) or 250_000)
    start = float(candidate.start_market_cap or 0)
    measured_window_multiple = window_peak / start if start > 0 else 1.0
    fresh_window = float(section.get("fresh_window_hours", 24) or 24)
    min_volume = float(section.get("min_volume_24h", 50_000))
    new_launch_peak = (
        age is not None
        and age <= fresh_window
        and peak >= peak_floor
        # A peak with no trade behind it is a price, not a market. Without this
        # a fresh coin reached the recap on a $2M cap and $0 of daily volume.
        and token.volume_24h >= min_volume
    )
    if new_launch_peak:
        return True

    if token.volume_24h < min_volume:
        return False
    if implausible_run(candidate, settings):
        return False

    # The peak floor is universal. Previously it was checked only in the fresh
    # launch branch, allowing a sub-floor old coin through on percentage alone.
    if peak < peak_floor:
        return False

    # A raw daily-change fallback is only meaningful when the pair age is
    # known.  Peak-tape candidates with a measured in-window peak have already
    # returned above; an undated coin must not slip into a 24-hour recap merely
    # because a provider reports a large percentage change.
    if age is None:
        return False

    if age <= fresh_window:
        return token.price_change_24h >= float(section.get("min_fresh_change_pct", 30))

    # The public recap treats the first 30 days as the token's launch lifecycle.
    # Reaching the verified daily peak floor is the event. After day 30, a large
    # existing market must make a fresh, size-adjusted move to return to the tape.
    old_coin_age = float(section.get("old_coin_age_hours", fresh_window) or fresh_window)
    if age <= old_coin_age:
        return True

    # A close-only floor is optional. Production leaves it disabled because an
    # established coin that ran 80% and round-tripped is still part of the
    # day's tape; its drawdown is disclosed rather than used to erase history.
    floor_change = float(section.get("older_min_live_change_pct", 0) or 0)
    if floor_change and token.price_change_24h < floor_change:
        return False

    # Established coins need a move that actually occurred inside this report
    # window.  KOL count, market cap and a lifetime ATH are context, not proof
    # that the token ran today.  GMGN candles preserve a spike even when the
    # close gave it back; the local hourly tape supplies the same proof once it
    # has accumulated enough observations.
    kline_peak_change = float(gmgn.get("kline24hPeakFromOpenPct") or 0)
    kline_close_change = float(gmgn.get("kline24hChangePct") or 0)
    has_gmgn_candles = int(gmgn.get("kline24hCandleCount") or 0) > 0
    measured_peak_change = max(0.0, (measured_window_multiple - 1.0) * 100.0)
    older_multiple = float(section.get("older_than_a_day_multiple", 5.0))
    if older_multiple > 0:
        required_change = (older_multiple - 1.0) * 100.0
    else:
        baseline_change = float(section.get("min_daily_change_pct", 50.0) or 50.0)
        micro_ceiling = float(section.get("old_coin_micro_cap_ceiling", 0) or 0)
        low_ceiling = float(section.get("old_coin_low_cap_ceiling", 0) or 0)
        small_ceiling = float(section.get("old_coin_small_cap_ceiling", 0) or 0)
        large_floor = float(section.get("old_coin_large_cap_floor", 0) or 0)
        # Choose the movement band from the market size reached today, not a
        # stale lifetime ATH. A former $100M coin trading at $3M still needs the
        # sub-$10M +75% revival, not the large-cap +30% shortcut.
        market_size = window_peak
        if micro_ceiling > 0 and market_size < micro_ceiling:
            required_change = float(section.get("old_coin_micro_min_change_pct", baseline_change) or baseline_change)
        elif low_ceiling > 0 and market_size < low_ceiling:
            required_change = float(section.get("old_coin_low_min_change_pct", baseline_change) or baseline_change)
        elif small_ceiling > 0 and market_size < small_ceiling:
            required_change = float(section.get("old_coin_small_min_change_pct", baseline_change) or baseline_change)
        elif large_floor > 0 and market_size >= large_floor:
            required_change = float(section.get("old_coin_large_min_change_pct", baseline_change) or baseline_change)
        else:
            required_change = float(section.get("old_coin_mid_min_change_pct", baseline_change) or baseline_change)
    # When GMGN candles exist they are the authority. This prevents a noisy
    # local market-cap estimate or a conflicting aggregate percentage from
    # promoting a flat/falling old token. Without candles, Dex and our own
    # hourly tape remain the graceful fallback.
    if has_gmgn_candles:
        observed_move = max(kline_peak_change, kline_close_change)
        # Production uses several independent market feeds. Keep GMGN candles
        # authoritative by default, but allow the verified aggregate daily
        # change to preserve a large-cap move such as CATE when one provider's
        # first candle starts after the move had already begun.
        if bool(section.get("old_coin_allow_aggregate_change_with_kline", False)):
            observed_move = max(observed_move, token.price_change_24h)
    else:
        observed_move = max(token.price_change_24h, measured_peak_change)
    if observed_move >= required_change:
        return True

    # The only lower-change exception for an older pair is a high printed in
    # this trailing-day candle set that reaches its GMGN lifetime ATH.  Requiring
    # an in-window candle prevents a stale historical ATH from qualifying it.
    if not bool(section.get("allow_old_new_ath_exception", True)):
        return False

    ath_tolerance = float(section.get("new_ath_tolerance_pct", 2.0) or 2.0) / 100.0
    min_ath_move = float(section.get("min_new_ath_move_pct", 10.0) or 10.0)
    verified_fresh_ath = (
        lifetime_ath >= peak_floor
        and kline_peak_cap >= lifetime_ath * (1.0 - ath_tolerance)
        and bool(gmgn.get("kline24hPeakAt"))
        and kline_peak_change >= min_ath_move
    )
    return verified_fresh_ath


def redistributed_launch_bundle(candidate: Candidate, settings: Settings) -> bool:
    """Whether a launch bundle has demonstrably dispersed into a real market.

    Launch bundling is a serious warning, but it is not immutable. An older
    token with tens of thousands of holders, low current concentration, deep
    locked liquidity and broad KOL participation is no longer controlled by
    the launch allocation in the same way. This exception never overrides
    wash trading, insider/dev concentration, live authorities or a rug verdict.
    """
    section = settings.section("journal")
    if not bool(section.get("allow_redistributed_launch_bundles", True)):
        return False
    token = candidate.token
    report = candidate.safety
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    age = candidate.signals.age_hours
    if age is None:
        return False
    return bool(
        age >= float(section.get("bundle_redistribution_min_age_hours", 72) or 72)
        and (report.holder_count or 0) >= int(section.get("bundle_redistribution_min_holders", 5_000) or 5_000)
        and report.top10_pct is not None
        and report.top10_pct <= float(section.get("bundle_redistribution_max_top10_pct", 20) or 20)
        and report.lp_locked_or_burned_pct is not None
        and report.lp_locked_or_burned_pct >= float(section.get("bundle_redistribution_min_lp_pct", 90) or 90)
        and int(gmgn.get("kolCount") or 0) >= int(section.get("bundle_redistribution_min_kol", 5) or 5)
        and float(gmgn.get("insiderRate") or 0) <= float(section.get("bundle_redistribution_max_insider_rate", 0.05) or 0.05)
        and float(gmgn.get("devTeamHoldRate") or 0) <= float(section.get("bundle_redistribution_max_dev_rate", 0.05) or 0.05)
        and token.liquidity_usd >= float(section.get("bundle_redistribution_min_liquidity", 100_000) or 100_000)
        and token.volume_24h >= float(section.get("bundle_redistribution_min_volume_24h", 1_000_000) or 1_000_000)
    )


def rug_or_bundle(candidate: Candidate, settings: Settings) -> list[str]:
    """Disqualifying conditions only.

    Everything here means the coin can be taken from its holders or was never
    distributed in the first place. Softer problems belong in `risk_labels`.
    """
    section = settings.section("journal")
    report = candidate.safety
    enrichment = candidate.enrichment
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    reasons: list[str] = []

    if report.rugged:
        reasons.append("RugCheck has already marked this token as rugged")
    gmgn_honeypot = gmgn.get("isHoneypot")
    if (
        gmgn_honeypot is True
        or gmgn_honeypot == 1
        or str(gmgn_honeypot or "").strip().lower() in {"true", "yes"}
    ):
        reasons.append("GMGN marks the contract as a honeypot")
    if report.mint_authority_renounced is False or enrichment.mint_authority_renounced is False:
        reasons.append("mint authority still live, supply can be inflated")
    if report.freeze_authority_disabled is False or enrichment.freeze_authority_disabled is False:
        reasons.append("freeze authority still live, holders can be frozen")
    gmgn_burned = (
        str(gmgn.get("burnStatus") or "").lower() == "yes"
        or float(gmgn.get("burnRatio") or 0) >= 0.90
    )
    if (
        report.lp_locked_or_burned_pct is not None
        and report.lp_locked_or_burned_pct <= 0
        and not gmgn_burned
    ):
        reasons.append("liquidity neither locked nor burned, it can be pulled")
    bundle_pct = float(section.get("bundle_top10_pct", 50))
    effective_top10 = report.top10_pct
    if effective_top10 is None and gmgn.get("top10Pct") is not None:
        effective_top10 = float(gmgn["top10Pct"])
    if effective_top10 is not None and effective_top10 > bundle_pct:
        reasons.append(f"bundled supply, top 10 circulating wallets hold {effective_top10:.0f}%")

    # GMGN sees launch-specific manipulation that a contract audit cannot:
    # bundled buys, insider flow and coordinated wash volume. These are direct
    # adverse observations, not arbitrary score cut-offs, so they fail closed
    # while KOL absence, boosts and a high standalone rug heuristic do not.
    if gmgn.get("washTrading") is True:
        reasons.append("GMGN detected wash trading")

    max_fee = float(section.get("max_total_fee_pct", 0) or 0)
    transfer_tax = transfer_tax_pct(gmgn)
    fee_chains = {
        str(chain).strip().lower()
        for chain in (section.get("fee_check_chains", []) or [])
    }
    fee_is_percentage = candidate.token.chain_id.lower() in fee_chains and transfer_tax is not None
    if max_fee and fee_is_percentage and transfer_tax > max_fee:
        reasons.append(
            f"taxed token: {transfer_tax:.1f}% is taken on every trade"
        )
    for field, label, setting, default in (
        ("bundlerRate", "GMGN bundled launch flow", "gmgn_max_bundler_rate", 0.30),
        ("insiderRate", "GMGN insider/rat-trader flow", "gmgn_max_insider_rate", 0.30),
        ("devTeamHoldRate", "GMGN dev-team holding", "gmgn_max_dev_team_hold_rate", 0.15),
    ):
        value = gmgn.get(field)
        ceiling = float(section.get(setting, default) or default)
        if value is not None and float(value) > ceiling:
            if field == "bundlerRate" and redistributed_launch_bundle(candidate, settings):
                continue
            reasons.append(f"{label} is {float(value):.0%}, above {ceiling:.0%}")
    for flag in report.risk_flags:
        lowered = flag.lower()
        if "sell tax" in lowered:
            try:
                if float(lowered.split("%")[0]) >= float(section.get("max_sell_tax_pct", 15)):
                    reasons.append(f"{flag}: selling is penalised")
            except ValueError:
                pass
        elif any(word in lowered for word in ("taken back", "hidden owner", "self-destruct", "paused", "blacklist")):
            reasons.append(flag)

    if (
        enrichment.unique_makers_24h is not None
        and candidate.token.txns_6h.total >= 100
        and enrichment.unique_makers_24h / candidate.token.txns_6h.total < 0.05
    ):
        reasons.append("manufactured tape, many trades from very few wallets")

    # A phantom float. When almost no supply is in the pool, a handful of
    # dollars prices the whole supply at any number the feed will print, and
    # impostor clones of a real coin arrive claiming hundreds of millions. Real
    # runners in this record sit under 20x their pool; this only catches the
    # ones that are arithmetically impossible.
    cap_ratio = float(section.get("max_market_cap_liquidity", 0) or 0)
    token = candidate.token
    if cap_ratio and token.liquidity_usd > 0 and token.market_cap > token.liquidity_usd * cap_ratio:
        reasons.append(
            f"phantom market cap: ${token.market_cap:,.0f} priced off a "
            f"${token.liquidity_usd:,.0f} pool"
        )

    # The same fiction from the other side. A hundred-million-dollar coin that
    # trades half a million dollars in a day is not a hundred-million-dollar
    # coin: almost none of the supply is reachable, and the cap is whatever the
    # last few trades implied. A real market of that size turns over.
    phantom_cap = float(section.get("phantom_cap_above", 0) or 0)
    phantom_turnover = float(section.get("phantom_min_turnover", 0) or 0)
    if phantom_cap and phantom_turnover and token.market_cap >= phantom_cap and token.volume_24h >= 0:
        turnover = token.volume_24h / token.market_cap if token.market_cap else 0.0
        if turnover < phantom_turnover:
            reasons.append(
                f"phantom market cap: ${token.market_cap:,.0f} on only "
                f"${token.volume_24h:,.0f} of daily volume"
            )
    return reasons


def runner_universe_reasons(candidate: Candidate, settings: Settings) -> list[str]:
    """Confirmed-danger checks for the complete Discord runner browser.

    Missing provider fields are retained as unknowns. A measured failure is
    rejected: rug/honeypot status, live owner controls, wash trading, excessive
    top-holder or developer control, pullable zero-lock liquidity, or a pool
    below the configured chain floor. The concise editorial recap may apply
    stricter quality and KOL ranking on top of this broad screened universe.
    """
    section = settings.section("journal")
    report = candidate.safety
    enrichment = candidate.enrichment
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    reasons: list[str] = []
    holder_exceptions = {
        str(mint).strip().lower()
        for mint in (section.get("holder_structure_exception_mints", []) or [])
        if str(mint).strip()
    }
    holder_exception = candidate.token.mint.lower() in holder_exceptions

    min_runner_score = float(section.get("runner_universe_min_runner_score", 0) or 0)
    runner_score = candidate.scores.get("runner")
    if min_runner_score and (runner_score is None or float(runner_score) < min_runner_score):
        rendered = "unavailable" if runner_score is None else f"{float(runner_score):.1f}"
        reasons.append(
            f"runner quality score {rendered}, below {min_runner_score:.0f}"
        )

    if report.rugged:
        reasons.append("security provider marks the token as rugged/honeypot")
    honeypot = gmgn.get("isHoneypot")
    if (
        honeypot is True
        or honeypot == 1
        or str(honeypot or "").strip().lower() in {"true", "yes"}
    ):
        reasons.append("GMGN marks the contract as a honeypot")

    if report.mint_authority_renounced is False or enrichment.mint_authority_renounced is False:
        reasons.append("mint authority still live")
    if report.freeze_authority_disabled is False or enrichment.freeze_authority_disabled is False:
        reasons.append("freeze/pause/blacklist powers still live")
    if gmgn.get("washTrading") is True:
        reasons.append("GMGN detected wash trading")

    holder_count = enrichment.holder_count or report.holder_count
    if not holder_exception and (holder_count is None or int(holder_count) <= 0):
        reasons.append("holder count unavailable or zero")

    top10 = report.top10_pct
    if top10 is None and gmgn.get("top10Pct") is not None:
        top10 = float(gmgn["top10Pct"])
    max_top10 = float(section.get("publisher_max_top10_pct", 30) or 30)
    if top10 is None and not holder_exception:
        reasons.append("top-10 concentration unavailable")
    elif float(top10) > max_top10:
        reasons.append(f"top 10 hold {float(top10):.0f}%, above {max_top10:.0f}%")

    dev_hold = gmgn.get("devTeamHoldRate")
    max_dev = float(section.get("gmgn_max_dev_team_hold_rate", 0.15) or 0.15)
    if dev_hold is not None and float(dev_hold) > max_dev:
        reasons.append(f"dev team holds {float(dev_hold):.0%}, above {max_dev:.0%}")

    burned = (
        str(gmgn.get("burnStatus") or "").lower() == "yes"
        or float(gmgn.get("burnRatio") or 0) >= 0.90
    )
    lp_pct = report.lp_locked_or_burned_pct
    if lp_pct is not None and float(lp_pct) <= 0 and not burned:
        reasons.append("liquidity is confirmed pullable")

    min_liquidity = chain_min_liquidity(section, candidate.token.chain_id, 0)
    if min_liquidity and candidate.token.liquidity_usd < min_liquidity:
        reasons.append(
            f"liquidity below chain floor "
            f"(${candidate.token.liquidity_usd:,.0f} < ${min_liquidity:,.0f})"
        )
    return list(dict.fromkeys(reasons))


def publisher_quality_reasons(candidate: Candidate, settings: Settings, now: datetime) -> list[str]:
    """Creator-facing quality gate.

    The journal used to be a broad record of anything that moved, with softer
    issues shown as labels. That is useful for an analyst, but too loose for a
    public creator recap. This gate keeps paid boosts, weak distribution,
    dead socials and already-fading moves out of the published runner set.
    """
    section = settings.section("journal")
    reasons: list[str] = []
    token = candidate.token
    report = candidate.safety
    enrichment = candidate.enrichment
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    gmgn_flow = gmgn.get("walletFlow", {}) or {}
    kol_touch_count = kol_trade_count(candidate)
    strong_kol_flow = kol_touch_count >= int(section.get("strong_kol_wallets", 3) or 3)

    if kol_touch_required(candidate, settings):
        min_kol_touches = int(section.get("min_kol_trades_for_publish", 1) or 1)
        coverage_available = bool(candidate.kol_wallets_scanned or gmgn_flow.get("coverageAvailable") or "kolCount" in gmgn)
        if coverage_available:
            if kol_touch_count < min_kol_touches:
                reasons.append(
                    f"no tracked KOL wallet traded it ({kol_touch_count}/{min_kol_touches} required)"
                )
        else:
            reasons.append("tracked KOL wallet scan unavailable")

    if bool(section.get("exclude_boosted", False)) and token.active_boosts:
        reasons.append("active Dexscreener boost; paid placement is not organic discovery")

    min_liquidity = chain_min_liquidity(section, token.chain_id, 0)
    if min_liquidity and token.liquidity_usd < min_liquidity:
        reasons.append(f"liquidity below publisher floor (${token.liquidity_usd:,.0f} < ${min_liquidity:,.0f})")

    fresh = publisher_is_fresh(candidate, settings, now)
    # RugCheck answers for Solana and GoPlus for the EVM chains, but neither has
    # indexed a coin that is two hours old, and no source at all covers some
    # chains. Treating "we could not measure this" as "this failed" quietly
    # deleted every young Base and BNB runner from the recap while Solana, where
    # the data arrives fastest, filled the whole page. A gap is reported on the
    # row instead; only a measured failure removes a coin.
    strict_missing = bool(section.get("block_on_missing_safety_data", False))

    if strict_missing and (not report.source or report.source == "unavailable"):
        reasons.append("contract-security provider unavailable")

    # Unknown authority state is not the same thing as safe. Public filter
    # results fail closed because users reasonably read their presence as a
    # completed contract check, not as an analyst watchlist.
    mint_safe = (
        report.mint_authority_renounced is True
        or enrichment.mint_authority_renounced is True
    )
    freeze_safe = (
        report.freeze_authority_disabled is True
        or enrichment.freeze_authority_disabled is True
    )
    if strict_missing and not mint_safe:
        reasons.append("mint authority/contract mintability not confirmed disabled")
    if strict_missing and not freeze_safe:
        reasons.append("freeze/pause/blacklist powers not confirmed disabled")

    if bool(section.get("require_holder_count", False)):
        min_holders = int(_floor(section, "min_holders", 0, fresh=fresh))
        holder_count = enrichment.holder_count or report.holder_count
        if holder_count is None:
            if strict_missing:
                reasons.append("holder count unavailable")
        elif min_holders and holder_count < min_holders:
            reasons.append(f"only {holder_count:,} holders, below publisher floor")

    min_lp = float(section.get("min_lp_locked_pct", 0) or 0)
    if min_lp:
        gmgn_burned = (
            str(gmgn.get("burnStatus") or "").lower() == "yes"
            or float(gmgn.get("burnRatio") or 0) >= min_lp / 100.0
        )
        if report.lp_locked_or_burned_pct is None and not gmgn_burned:
            if strict_missing:
                reasons.append("LP lock/burn status unavailable")
        elif (
            report.lp_locked_or_burned_pct is not None
            and report.lp_locked_or_burned_pct < min_lp
            and not gmgn_burned
        ):
            reasons.append(f"LP only {report.lp_locked_or_burned_pct:.0f}% locked or burned")

    max_top10 = _floor(section, "publisher_max_top10_pct", 0, fresh=fresh)
    if max_top10:
        top10_pct = report.top10_pct
        if top10_pct is None and gmgn.get("top10Pct") is not None:
            top10_pct = float(gmgn["top10Pct"])
        if top10_pct is None:
            if strict_missing:
                reasons.append("top-10 concentration unavailable")
        elif top10_pct > max_top10:
            reasons.append(f"top 10 hold {top10_pct:.0f}%, above publisher ceiling")

    if bool(section.get("require_socials", False)) and not token.socials and not strong_kol_flow:
        reasons.append("no linked social context")
    if bool(section.get("require_social_resolves", False)) and enrichment.social_resolves is False:
        reasons.append("linked X account does not resolve")

    min_symbol = int(section.get("min_symbol_length", 0) or 0)
    if min_symbol and latin_symbol(token.symbol) and len(str(token.symbol or "").strip()) < min_symbol:
        reasons.append(f"ticker is under {min_symbol} characters")

    blocked_symbols = {
        str(entry).strip().casefold()
        for entry in (section.get("blocked_symbols", []) or [])
        if str(entry).strip()
    }
    if str(token.symbol or "").strip().casefold() in blocked_symbols:
        reasons.append(f"blocked ticker: {token.symbol}")

    blocked_terms = [
        str(term).casefold()
        for term in (section.get("blocked_symbol_terms", []) or [])
        if str(term).strip()
    ]
    label = f"{token.symbol} {token.name}".casefold()
    for term in blocked_terms:
        if re.search(rf"(^|[^a-z0-9]){re.escape(term)}([^a-z0-9]|$)", label):
            reasons.append(f"blocked low-signal theme term: {term}")
            break

    if bool(section.get("exclude_recycled", False)) and candidate.recycled_label_count and not strong_kol_flow:
        reasons.append(f"ticker/name reused by {candidate.recycled_label_count} other recent mint(s)")

    age = _age_hours(token, now)
    min_age = float(section.get("min_age_hours", 0) or 0)
    if min_age and age is not None and age < min_age:
        reasons.append(f"only {age:.1f}h old; too early for publisher recap")

    if bool(section.get("require_last_hour_activity", False)):
        # Both signals have to be silent: a flat price alone can just be a
        # steady hour, but a flat price with no prints at all is a dead pair.
        # Only a source that reports the hour can say the hour was empty.
        if token.intraday_known and token.txns_1h.total == 0 and token.volume_1h <= 0:
            reasons.append("no trade at all in the last hour")

    max_fade = float(section.get("max_negative_1h_pct", 0) or 0)
    if max_fade and token.price_change_1h <= -abs(max_fade):
        reasons.append(f"fading {token.price_change_1h:.0f}% in the last hour")

    extreme_multiple = float(section.get("extreme_multiple", 0) or 0)
    if extreme_multiple and candidate.run_multiple >= extreme_multiple:
        min_extreme_volume = float(section.get("extreme_min_volume_24h", 0) or 0)
        min_extreme_holders = int(section.get("extreme_min_holders", 0) or 0)
        min_extreme_turnover = float(section.get("extreme_min_turnover", 0) or 0)
        min_extreme_recent_share = float(section.get("extreme_min_recent_volume_share", 0) or 0)
        if min_extreme_volume and token.volume_24h < min_extreme_volume:
            reasons.append(
                f"{candidate.run_multiple:.0f}x move on only ${token.volume_24h:,.0f} volume"
            )
        if min_extreme_turnover and candidate.signals.turnover < min_extreme_turnover:
            reasons.append(
                f"{candidate.run_multiple:.0f}x move on only {candidate.signals.turnover:.2f}x turnover"
            )
        if min_extreme_holders and (report.holder_count is None or report.holder_count < min_extreme_holders):
            holder_text = "unavailable holders" if report.holder_count is None else f"{report.holder_count:,} holders"
            reasons.append(f"{candidate.run_multiple:.0f}x move with {holder_text}")
        if min_extreme_recent_share and token.volume_24h and six_hour_volume_known(token):
            recent_share = token.volume_6h / token.volume_24h
            if (
                recent_share < min_extreme_recent_share
                and gmgn.get("organicQualified") is not True
            ):
                reasons.append(
                    f"{candidate.run_multiple:.0f}x move but only {recent_share:.0%} of volume stayed active in 6h"
                )
        if not token.socials and not strong_kol_flow:
            reasons.append(f"{candidate.run_multiple:.0f}x move with no linked social context")
        if candidate.recycled_label_count and not strong_kol_flow:
            reasons.append(f"{candidate.run_multiple:.0f}x move on recycled ticker/name")

    min_confirmations = int(section.get("min_organic_confirmations", 0) or 0)
    if min_confirmations:
        confirmations: list[str] = []
        min_holders = int(_floor(section, "min_holders", 0, fresh=fresh))
        min_trades = int(_floor(section, "min_trades_24h", 0, fresh=fresh))
        min_volume = float(section.get("min_volume_24h", 0) or 0)
        min_liquidity = chain_min_liquidity(section, token.chain_id, 0)
        max_top10 = _floor(section, "publisher_max_top10_pct", 0, fresh=fresh)
        min_buy_ratio = float(section.get("organic_min_buy_ratio", 0.42) or 0.42)
        max_buy_ratio = float(section.get("organic_max_buy_ratio", 0.72) or 0.72)

        # Two of these checks read a number that only Solana reports in time:
        # holder count and top-10 share. Counting an unmeasured check as a
        # failure put the EVM ceiling at five out of seven against a demand for
        # six, so no Base, BNB or Ethereum coin could ever qualify however
        # organic it was. A coin is judged on the checks that could be taken.
        applicable = 0

        if report.holder_count is not None:
            applicable += 1
            if not min_holders or report.holder_count >= min_holders:
                confirmations.append("holders")
        if token.txns_24h.total:
            applicable += 1
            if not min_trades or token.txns_24h.total >= min_trades:
                confirmations.append("trades")
        applicable += 1
        if not min_volume or token.volume_24h >= min_volume:
            confirmations.append("volume")
        applicable += 1
        if not min_liquidity or token.liquidity_usd >= min_liquidity:
            confirmations.append("liquidity")
        applicable += 1
        if token.socials or strong_kol_flow:
            confirmations.append("context")
        if report.top10_pct is not None:
            applicable += 1
            if not max_top10 or report.top10_pct <= max_top10:
                confirmations.append("distribution")
        if candidate.signals.buy_imbalance_6h is not None:
            applicable += 1
            if min_buy_ratio <= candidate.signals.buy_imbalance_6h <= max_buy_ratio:
                confirmations.append("two-sided book")

        min_confirmations = min(min_confirmations, applicable)
        if len(confirmations) < min_confirmations:
            reasons.append(
                f"only {len(confirmations)}/{min_confirmations} organic confirmations "
                f"({', '.join(confirmations) or 'none'})"
            )

    return reasons


def inorganic_reasons(candidate: Candidate, settings: Settings) -> list[str]:
    """Signs the move was manufactured rather than bought by a crowd.

    A rug takes money from holders; this is a different failure. The coin may be
    perfectly safe to hold and still be a machine trading with itself, a bought
    trend slot, or the ninth mint to wear a ticker that worked once. None of
    those belong in a record of what the market actually did, and putting one on
    a broadcast costs more credibility than leaving the slot empty.
    """
    section = settings.section("journal")
    token = candidate.token
    signal = candidate.signals
    reasons: list[str] = []

    for warning in candidate.warnings:
        lowered = warning.casefold()
        if (
            "same-funder holder cluster" in lowered
            or "fresh-wallet holder pack" in lowered
            or "effective top10 after clustering" in lowered
        ):
            reasons.append(warning)

    if token.volume_24h <= 0:
        reasons.append("no 24h volume at all")

    ratio = token.volume_24h / token.liquidity_usd if token.liquidity_usd else 0.0
    # Volume against pool depth says almost nothing about a new launch. A real
    # 20x on a $40k pool trades far past this ceiling because the pool is small,
    # not because the tape is fake. Turnover against market cap below is the
    # measure that survives a thin float, so the depth ratio is loosened here
    # rather than being allowed to reject every young runner.
    max_ratio = _floor(section, "max_volume_liquidity", 150,
                       fresh=publisher_is_fresh(candidate, settings))
    if token.liquidity_usd and ratio > max_ratio:
        reasons.append(f"wash-trading shape: {ratio:,.0f}x its own liquidity traded in 24h")

    max_turnover = float(section.get("max_turnover", 30))
    if signal.turnover > max_turnover:
        reasons.append(f"{signal.turnover:,.0f}x its market cap traded in 24h")

    # Cadence alone says nothing: a normal hot launch prints hundreds of trades
    # a minute, and every coin an earlier cadence rule rejected turned out to
    # have a larger average trade than a reference the client vouched for. Speed
    # is what a crowd looks like. A bot looks like dust -- many prints with
    # almost no money behind them -- so the two are only damning together.
    per_minute = token.txns_6h.total / 360 if token.txns_6h.total else 0
    min_trade = float(section.get("min_average_trade_usd", 15))
    average = token.volume_6h / token.txns_6h.total if token.txns_6h.total and token.volume_6h else None
    if average is not None and token.txns_6h.total >= 200 and average < min_trade:
        reasons.append(
            f"average trade is ${average:,.2f} across {per_minute:.0f} trades a minute, "
            "which is spam rather than demand"
        )

    min_holders = int(_floor(section, "min_holders", 200, fresh=publisher_is_fresh(candidate, settings)))
    # Helius/GMGN holder counts are full-chain counts. Some contract-security
    # providers return a partial sample; using that smaller number made a
    # 1,300-holder runner look like it had only 104 holders. The engine already
    # applies source precedence, and enrichment carries the authoritative count
    # as a final guard for candidates reconstructed from old snapshots.
    holders = candidate.enrichment.holder_count or candidate.safety.holder_count
    # Zero means the source had no figure. A coin with genuinely no holders
    # cannot trade, so a zero here is always missing data.
    if holders:
        holders = holders if holders > 0 else None
    else:
        holders = None
    if holders is not None and holders < min_holders:
        reasons.append(f"only {holders} holders, which is not a market yet")

    min_trades = int(_floor(section, "min_trades_24h", 300, fresh=publisher_is_fresh(candidate, settings)))
    if token.txns_24h.total and token.txns_24h.total < min_trades:
        reasons.append(f"only {token.txns_24h.total} trades in 24h")

    # The pump-and-die shape: an enormous printed gain with nothing still
    # trading behind it. An even day puts a quarter of its volume in the last
    # six hours, so a few percent means the move finished hours ago.
    min_share = float(section.get("min_recent_volume_share", 0.08))
    if (
        token.volume_24h
        and six_hour_volume_known(token)
        and run_multiple(token) >= float(section.get("dead_check_above_multiple", 5))
    ):
        share = token.volume_6h / token.volume_24h
        gmgn_organic = bool(
            (candidate.provider_evidence.get("gmgn", {}) or {}).get("organicQualified")
        )
        if share < min_share and not gmgn_organic:
            reasons.append(
                f"the move is over: only {share:.0%} of the day's volume traded in the last six hours"
            )

    max_buys = float(section.get("max_buy_ratio", 0.85))
    if (
        signal.buy_imbalance_6h is not None
        and token.txns_6h.total >= int(section.get("one_sided_min_trades", 300))
        and signal.buy_imbalance_6h >= max_buys
    ):
        reasons.append(
            f"{signal.buy_imbalance_6h:.0%} of trades are buys, which is a manufactured book"
        )
    return reasons


def faded_from_peak(token: TokenSnapshot) -> float | None:
    """Detect the ran-then-dumped shape: strongly up on the day, down right now.

    This is the coin that went to an all-time high and gave it back. The journal
    still records it, because what it did today is the point, but nobody should
    read the 24h number without seeing the last hour next to it.
    """
    if token.price_change_24h < 50:
        return None
    if token.price_change_1h < -15:
        return token.price_change_1h
    return None


def untouched_by_tracked_wallets(candidate: Candidate, settings: Settings) -> bool:
    """A large move that not one tracked wallet went near.

    These wallets are on the leaderboard because they find moves like this. When
    a coin doubles several times over and none of them bought, sold or held any
    of it, the move was made by someone else and it is worth saying so out loud.

    Weak evidence on its own -- it is 66 wallets out of a very large market, so
    plenty of real runners are missed by all of them. That is why this labels
    the row rather than removing it.
    """
    section = settings.section("journal")
    if not candidate.kol_wallets_scanned:
        return False
    threshold = float(section.get("expect_tracked_wallets_above", 5.0))
    if candidate.run_multiple < threshold:
        return False
    return not (kol_traders(candidate) or candidate.kol_realised_sol)


def missing_wallet_confirmation(candidate: Candidate, settings: Settings) -> bool:
    """Whether a publishable Solana runner lacks the wallet heat it should have.

    This is stricter than the row label above. The newsletter is for a creator,
    not a lab notebook: if a big Solana runner was missed by the entire tracked
    wallet net, it should not headline as clean unless holder/distribution data
    proves another organic crowd.
    """
    section = settings.section("journal")
    if not bool(section.get("require_wallet_touch_for_publish", False)):
        return False
    if candidate.token.chain_id.lower() != "solana" or not candidate.kol_wallets_scanned:
        return False
    threshold = float(section.get("wallet_touch_required_above_multiple", 2.0) or 2.0)
    min_mcap = float(section.get("wallet_touch_required_min_mcap", 0) or 0)
    if candidate.run_multiple < threshold:
        return False
    if min_mcap and candidate.token.market_cap < min_mcap:
        return False
    touch_count = len(kol_traders(candidate))
    min_buyers = int(section.get("wallet_touch_required_min_buyers", 2) or 2)
    min_participants = int(section.get("wallet_touch_required_min_participants", 3) or 3)
    min_realised = float(section.get("wallet_touch_required_min_realised_sol", 50.0) or 50.0)
    if len(set(candidate.kol_buyers)) >= min_buyers:
        return False
    if touch_count >= min_participants:
        return False
    if abs(float(candidate.kol_realised_sol or 0)) >= min_realised:
        return False
    return True


def risk_labels(candidate: Candidate, settings: Settings, now: datetime) -> list[str]:
    """Everything worth seeing on the row that is not a reason to hide it."""
    section = settings.section("journal")
    token = candidate.token
    report = candidate.safety
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    labels: list[str] = []

    bundler_rate = gmgn.get("bundlerRate")
    bundler_ceiling = float(section.get("gmgn_max_bundler_rate", 0.30) or 0.30)
    if (
        bundler_rate is not None
        and float(bundler_rate) > bundler_ceiling
        and redistributed_launch_bundle(candidate, settings)
    ):
        labels.append(
            f"{float(bundler_rate):.0%} launch bundle; current holders and concentration show redistribution"
        )

    caution_pct = float(section.get("caution_top10_pct", 25))
    if report.top10_pct is not None and report.top10_pct > caution_pct:
        labels.append(f"top 10 hold {report.top10_pct:.0f}%")
    if report.top10_pct is None:
        labels.append("concentration unknown")
    if report.lp_locked_or_burned_pct is None:
        labels.append("LP lock unknown")
    transfer_tax = transfer_tax_pct(gmgn)
    fee_chains = {str(c).strip().lower() for c in (section.get("fee_check_chains", []) or [])}
    if (
        token.chain_id.lower() in fee_chains
        and transfer_tax is not None
        and float(section.get("caution_total_fee_pct", 1) or 1) <= transfer_tax <= 50
    ):
        labels.append(f"{transfer_tax:.1f}% tax on every trade")
    ratio = token.volume_24h / token.liquidity_usd if token.liquidity_usd else float("inf")
    if ratio > float(section.get("thin_liquidity_ratio", 60)):
        labels.append(f"thin pool, {ratio:.0f}x its liquidity traded")
    if token.active_boosts:
        labels.append("active Dexscreener boost")
    if candidate.recycled_label_count:
        labels.append(f"ticker also used by {candidate.recycled_label_count} other recent mint(s)")
    if candidate.safety.source == "unavailable" and token.chain_id.lower() != "solana":
        labels.append("no contract safety source covers this chain")
    for flag in candidate.safety.risk_flags:
        if flag.lower().startswith(("upgradeable", "contract source")) or "buy tax" in flag.lower():
            labels.append(flag)
    if not token.socials:
        labels.append("no linked socials")
    if candidate.enrichment.social_resolves is False:
        # A dead X link is worth seeing, but it is not evidence that the coin
        # can be taken from its holders, so it labels rather than disqualifies.
        labels.append("linked X account does not resolve")
    age = _age_hours(token, now)
    if age is not None and age < 3:
        labels.append(f"only {age:.1f}h old")
    fade = faded_from_peak(token)
    if fade is not None:
        labels.append(f"fading, down {abs(fade):.0f}% in the last hour")
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    if (
        gmgn.get("organicQualified") is True
        and token.volume_24h
        and run_multiple(token) >= float(section.get("dead_check_above_multiple", 5))
    ):
        share = token.volume_6h / token.volume_24h
        min_share = float(section.get("min_recent_volume_share", 0.08))
        if share < min_share:
            labels.append(
                f"peaked earlier; only {share:.0%} of 24h volume remained in the last six hours"
            )
    if untouched_by_tracked_wallets(candidate, settings):
        labels.append(
            f"{candidate.run_multiple:.0f}x and not one tracked wallet touched it"
        )
    return labels


_STOPWORDS = {
    "the", "a", "an", "of", "and", "coin", "token", "inu", "sol", "solana",
    "official", "on", "by", "for", "to", "is", "it", "meme",
}


def _words(text: str) -> list[str]:
    return [
        word for word in re.split(r"[^a-z0-9]+", text.casefold())
        if len(word) >= 3 and word not in _STOPWORDS
    ]


def assign_lore(candidates: list[Candidate], settings: Settings) -> dict[str, list[Candidate]]:
    """Group the day's runners by shared narrative.

    Dexscreener's trending metas are the strongest signal and are used first.
    Anything left over is grouped by a shared significant word in the name,
    which is how copycats of one story cluster together.
    """
    groups: dict[str, list[Candidate]] = {}
    leftovers: list[Candidate] = []
    for candidate in candidates:
        slugs = sorted(candidate.token.meta_slugs)
        if slugs:
            candidate.lore = slugs[0]
            groups.setdefault(slugs[0], []).append(candidate)
        else:
            leftovers.append(candidate)

    counts: Counter[str] = Counter()
    for candidate in leftovers:
        counts.update(set(_words(f"{candidate.token.name} {candidate.token.symbol}")))
    for candidate in leftovers:
        shared = sorted(
            word for word in set(_words(f"{candidate.token.name} {candidate.token.symbol}"))
            if counts[word] > 1
        )
        candidate.lore = shared[0] if shared else ""
        if candidate.lore:
            groups.setdefault(candidate.lore, []).append(candidate)

    min_size = int(settings.get("journal", "min_lore_group", 2))
    return {name: members for name, members in groups.items() if len(members) >= min_size}


def mark_lore_freshness(candidates: list[Candidate], ledger, now: datetime, settings: Settings) -> None:
    """A lore that keeps coming back is not a new lore.

    The brief is meant to surface stories that have not been done over and over,
    so a runner whose narrative already produced recent mints is flagged rather
    than presented as original.
    """
    lookback = int(settings.get("editorial", "recycled_symbol_lookback_days", 30))
    for candidate in candidates:
        reused = candidate.recycled_label_count
        if not reused and candidate.lore:
            reused = ledger.recent_symbol_reuse(candidate.lore, candidate.token.mint, now, lookback)
        candidate.lore_is_fresh = reused == 0
        if not candidate.lore_is_fresh and candidate.lore:
            candidate.risk_labels.append(f"lore '{candidate.lore}' has run before")


def journal_rank_key(candidate: Candidate) -> tuple[float, ...]:
    """Organic attention first: volume, trades and holders beat raw percent."""
    token = candidate.token
    return (
        candidate.scores.get("runner", 0.0),
        candidate.scores.get("organic", 0.0),
        -candidate.scores.get("manipulation", 0.0),
        float(len(candidate.kol_buyers)),
        token.volume_24h,
        float(token.txns_24h.total or token.txns_6h.total),
        float(candidate.safety.holder_count or 0),
        candidate.signals.turnover,
        token.price_change_24h,
    )


def _matches_any_reason(reason: str, patterns: list[str]) -> bool:
    lowered = reason.casefold()
    return any(pattern and pattern in lowered for pattern in patterns)


def caveated_runner_fill_allowed(candidate: Candidate, settings: Settings) -> bool:
    """Allow non-toxic caveated names to fill the daily tape.

    The product wants a useful morning recap, usually 5-6+ names. That does not
    mean forcing rugs through. This lets softer editorial failures remain
    visible while hard safety/manipulation failures stay blocked.
    """
    section = settings.section("journal")
    if not bool(section.get("fill_with_caveated_runners", False)):
        return False

    age = candidate.signals.age_hours
    min_age = float(section.get("fill_min_age_hours", 0.5) or 0.5)
    if age is not None and age < min_age:
        return False
    if candidate.scores.get("runner", 0.0) < float(section.get("fill_min_runner_score", 25.0) or 25.0):
        return False
    if candidate.scores.get("organic", 0.0) < float(section.get("fill_min_organic_score", 40.0) or 40.0):
        return False
    if candidate.scores.get("manipulation", 100.0) > float(section.get("fill_max_manipulation", 55.0) or 55.0):
        return False

    hard_terms = [
        str(term).casefold()
        for term in (section.get("fill_hard_block_terms", []) or [])
        if str(term).strip()
    ]
    return not any(_matches_any_reason(reason, hard_terms) for reason in candidate.risk_labels)


def build_journal(
    candidates: list[Candidate], settings: Settings, ledger, now: datetime
) -> tuple[list[Candidate], list[Candidate]]:
    """Return (runners, blocked) where blocked is the rug/bundle pile."""
    runners: list[Candidate] = []
    blocked: list[Candidate] = []
    for candidate in candidates:
        if not belongs_in_journal(candidate, settings, now):
            continue
        candidate.run_multiple = run_multiple(candidate.token)
        candidate.faded_from_peak = faded_from_peak(candidate.token)
        disqualifying = (
            rug_or_bundle(candidate, settings)
            + inorganic_reasons(candidate, settings)
            + publisher_quality_reasons(candidate, settings, now)
        )
        if missing_wallet_confirmation(candidate, settings):
            disqualifying.append(
                f"{candidate.run_multiple:.1f}x Solana runner but tracked-wallet confirmation was too thin "
                f"({len(set(candidate.kol_buyers))} buyers, "
                f"{len(kol_traders(candidate))} participants)"
            )
        if disqualifying:
            candidate.risk_labels = disqualifying
            blocked.append(candidate)
            continue
        candidate.risk_labels = risk_labels(candidate, settings, now)
        runners.append(candidate)
    assign_lore(runners, settings)
    mark_lore_freshness(runners, ledger, now, settings)
    runners.sort(key=journal_rank_key, reverse=True)
    blocked.sort(key=journal_rank_key, reverse=True)
    target_min = int(settings.get("journal", "target_min_runners", 0) or 0)
    if target_min and len(runners) < target_min:
        promoted: list[Candidate] = []
        still_blocked: list[Candidate] = []
        for candidate in blocked:
            if len(runners) + len(promoted) < target_min and caveated_runner_fill_allowed(candidate, settings):
                if "caveated runner: failed a soft editorial gate, not a hard rug/bundle/wash gate" not in candidate.risk_labels:
                    candidate.risk_labels.insert(
                        0,
                        "caveated runner: failed a soft editorial gate, not a hard rug/bundle/wash gate",
                    )
                promoted.append(candidate)
            else:
                still_blocked.append(candidate)
        if promoted:
            runners = [*runners, *promoted]
            runners.sort(key=journal_rank_key, reverse=True)
            blocked = still_blocked
    return runners, blocked
