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


def _age_hours(token: TokenSnapshot, now: datetime) -> float | None:
    if token.pair_created_at is None:
        return None
    return max(0.0, (now.astimezone(timezone.utc) - token.pair_created_at).total_seconds() / 3600)


def run_multiple(token: TokenSnapshot) -> float:
    """How many times the coin is up over 24 hours. +400% is a 5x."""
    return 1.0 + token.price_change_24h / 100.0


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
    if multiple >= big and candidate.signals.turnover < floor:
        return True
    return False


def belongs_in_journal(candidate: Candidate, settings: Settings, now: datetime) -> bool:
    """Only coins created inside the age ceiling, and only if they ran.

    The record is about what launched and worked, so age is a hard wall rather
    than something a big multiple can buy past. A pair with no known creation
    time is excluded too: it cannot be shown to be new, and the whole point of
    the ceiling is that everything in the record provably is.

    Inside the wall the bar eases with age. A pair still in its first day only
    has to be up `min_fresh_change_pct`; past that it has to have done a real
    multiple, because a day-old coin that is only up a third is not news.
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

    if token.volume_24h < float(section.get("min_volume_24h", 50_000)):
        return False
    if implausible_run(candidate, settings):
        return False


    ceiling = float(section.get("max_age_hours", 36))
    if age is None or (ceiling and age > ceiling):
        return False

    fresh_window = float(section.get("fresh_window_hours", 24))
    if age <= fresh_window:
        return token.price_change_24h >= float(section.get("min_fresh_change_pct", 30))
    older_multiple = float(section.get("older_than_a_day_multiple", 5.0))
    if older_multiple > 0:
        return run_multiple(token) >= older_multiple
    return token.price_change_24h >= float(section.get("min_daily_change_pct", 25.0))


def rug_or_bundle(candidate: Candidate, settings: Settings) -> list[str]:
    """Disqualifying conditions only.

    Everything here means the coin can be taken from its holders or was never
    distributed in the first place. Softer problems belong in `risk_labels`.
    """
    section = settings.section("journal")
    report = candidate.safety
    enrichment = candidate.enrichment
    reasons: list[str] = []

    if report.rugged:
        reasons.append("RugCheck has already marked this token as rugged")
    if report.mint_authority_renounced is False or enrichment.mint_authority_renounced is False:
        reasons.append("mint authority still live, supply can be inflated")
    if report.freeze_authority_disabled is False or enrichment.freeze_authority_disabled is False:
        reasons.append("freeze authority still live, holders can be frozen")
    if report.lp_locked_or_burned_pct is not None and report.lp_locked_or_burned_pct <= 0:
        reasons.append("liquidity neither locked nor burned, it can be pulled")
    bundle_pct = float(section.get("bundle_top10_pct", 50))
    if report.top10_pct is not None and report.top10_pct > bundle_pct:
        reasons.append(f"bundled supply, top 10 circulating wallets hold {report.top10_pct:.0f}%")
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
    return reasons


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
    kol_touch_count = len(set(candidate.kol_buyers) | set(candidate.kol_holders) | set(candidate.kol_sellers))
    strong_kol_flow = kol_touch_count >= int(section.get("strong_kol_wallets", 3) or 3)

    if bool(section.get("require_kol_trade_for_publish", False)):
        min_kol_touches = int(section.get("min_kol_trades_for_publish", 1) or 1)
        if candidate.token.chain_id.lower() == "solana" and candidate.kol_wallets_scanned:
            if kol_touch_count < min_kol_touches:
                reasons.append(
                    f"no tracked KOL wallet traded it "
                    f"({kol_touch_count}/{min_kol_touches} required from {candidate.kol_wallets_scanned} scanned)"
                )
        elif candidate.token.chain_id.lower() == "solana":
            reasons.append("tracked KOL wallet scan unavailable")

    if bool(section.get("exclude_boosted", False)) and token.active_boosts:
        reasons.append("active Dexscreener boost; paid placement is not organic discovery")

    min_liquidity = float(section.get("min_liquidity", 0) or 0)
    if min_liquidity and token.liquidity_usd < min_liquidity:
        reasons.append(f"liquidity below publisher floor (${token.liquidity_usd:,.0f} < ${min_liquidity:,.0f})")

    if bool(section.get("require_holder_count", False)):
        min_holders = int(section.get("min_holders", 0) or 0)
        if report.holder_count is None:
            reasons.append("holder count unavailable")
        elif min_holders and report.holder_count < min_holders:
            reasons.append(f"only {report.holder_count:,} holders, below publisher floor")

    min_lp = float(section.get("min_lp_locked_pct", 0) or 0)
    if min_lp:
        if report.lp_locked_or_burned_pct is None:
            reasons.append("LP lock/burn status unavailable")
        elif report.lp_locked_or_burned_pct < min_lp:
            reasons.append(f"LP only {report.lp_locked_or_burned_pct:.0f}% locked or burned")

    max_top10 = float(section.get("publisher_max_top10_pct", 0) or 0)
    if max_top10:
        if report.top10_pct is None:
            reasons.append("top-10 concentration unavailable")
        elif report.top10_pct > max_top10:
            reasons.append(f"top 10 hold {report.top10_pct:.0f}%, above publisher ceiling")

    if bool(section.get("require_socials", False)) and not token.socials and not strong_kol_flow:
        reasons.append("no linked social context")
    if bool(section.get("require_social_resolves", False)) and enrichment.social_resolves is False:
        reasons.append("linked X account does not resolve")

    min_symbol = int(section.get("min_symbol_length", 0) or 0)
    if min_symbol and len(str(token.symbol or "").strip()) < min_symbol:
        reasons.append(f"ticker is under {min_symbol} characters")

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
        if min_extreme_recent_share and token.volume_24h:
            recent_share = token.volume_6h / token.volume_24h
            if recent_share < min_extreme_recent_share:
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
        min_holders = int(section.get("min_holders", 0) or 0)
        min_trades = int(section.get("min_trades_24h", 0) or 0)
        min_volume = float(section.get("min_volume_24h", 0) or 0)
        min_liquidity = float(section.get("min_liquidity", 0) or 0)
        max_top10 = float(section.get("publisher_max_top10_pct", 0) or 0)
        min_buy_ratio = float(section.get("organic_min_buy_ratio", 0.42) or 0.42)
        max_buy_ratio = float(section.get("organic_max_buy_ratio", 0.72) or 0.72)

        if report.holder_count is not None and (not min_holders or report.holder_count >= min_holders):
            confirmations.append("holders")
        if token.txns_24h.total and (not min_trades or token.txns_24h.total >= min_trades):
            confirmations.append("trades")
        if not min_volume or token.volume_24h >= min_volume:
            confirmations.append("volume")
        if not min_liquidity or token.liquidity_usd >= min_liquidity:
            confirmations.append("liquidity")
        if token.socials or strong_kol_flow:
            confirmations.append("context")
        if report.top10_pct is not None and (not max_top10 or report.top10_pct <= max_top10):
            confirmations.append("distribution")
        if (
            candidate.signals.buy_imbalance_6h is not None
            and min_buy_ratio <= candidate.signals.buy_imbalance_6h <= max_buy_ratio
        ):
            confirmations.append("two-sided book")

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

    ratio = token.volume_24h / token.liquidity_usd if token.liquidity_usd else 0.0
    max_ratio = float(section.get("max_volume_liquidity", 150))
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

    min_holders = int(section.get("min_holders", 200))
    holders = candidate.safety.holder_count
    if holders is not None and holders < min_holders:
        reasons.append(f"only {holders} holders, which is not a market yet")

    min_trades = int(section.get("min_trades_24h", 300))
    if token.txns_24h.total and token.txns_24h.total < min_trades:
        reasons.append(f"only {token.txns_24h.total} trades in 24h")

    # The pump-and-die shape: an enormous printed gain with nothing still
    # trading behind it. An even day puts a quarter of its volume in the last
    # six hours, so a few percent means the move finished hours ago.
    min_share = float(section.get("min_recent_volume_share", 0.08))
    if token.volume_24h and run_multiple(token) >= float(section.get("dead_check_above_multiple", 5)):
        share = token.volume_6h / token.volume_24h
        if share < min_share:
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
    return not (
        candidate.kol_buyers
        or candidate.kol_sellers
        or candidate.kol_holders
        or candidate.kol_realised_sol
    )


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
    touch_count = len(set(candidate.kol_buyers) | set(candidate.kol_holders) | set(candidate.kol_sellers))
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
    labels: list[str] = []

    caution_pct = float(section.get("caution_top10_pct", 25))
    if report.top10_pct is not None and report.top10_pct > caution_pct:
        labels.append(f"top 10 hold {report.top10_pct:.0f}%")
    if report.top10_pct is None:
        labels.append("concentration unknown")
    if report.lp_locked_or_burned_pct is None:
        labels.append("LP lock unknown")
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
                f"{len(set(candidate.kol_buyers) | set(candidate.kol_holders) | set(candidate.kol_sellers))} participants)"
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
