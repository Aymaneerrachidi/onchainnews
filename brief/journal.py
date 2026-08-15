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
    """Created today and running, or any age doing a large multiple.

    Two doors, because the day's record needs today's launches *and* the older
    coin that just did a 5x, but not an established major drifting up 8%.
    """
    section = settings.section("journal")
    token = candidate.token
    age = _age_hours(token, now)

    if token.volume_24h < float(section.get("min_volume_24h", 50_000)):
        return False
    if implausible_run(candidate, settings):
        return False

    fresh_window = float(section.get("fresh_window_hours", 24))
    if age is not None and age <= fresh_window:
        return token.price_change_24h >= float(section.get("min_fresh_change_pct", 30))

    max_age_days = float(section.get("max_age_days", 0))
    if max_age_days and age is not None and age > max_age_days * 24:
        return False
    return run_multiple(token) >= float(section.get("old_coin_multiple", 5.0))


def rug_or_bundle(candidate: Candidate, settings: Settings) -> list[str]:
    """Disqualifying conditions only.

    Everything here means the coin can be taken from its holders or was never
    distributed in the first place. Softer problems belong in `risk_labels`.
    """
    section = settings.section("journal")
    report = candidate.safety
    enrichment = candidate.enrichment
    reasons: list[str] = []

    if report.mint_authority_renounced is False or enrichment.mint_authority_renounced is False:
        reasons.append("mint authority still live, supply can be inflated")
    if report.freeze_authority_disabled is False or enrichment.freeze_authority_disabled is False:
        reasons.append("freeze authority still live, holders can be frozen")
    if report.lp_locked_or_burned_pct is not None and report.lp_locked_or_burned_pct <= 0:
        reasons.append("liquidity neither locked nor burned, it can be pulled")
    bundle_pct = float(section.get("bundle_top10_pct", 50))
    if report.top10_pct is not None and report.top10_pct > bundle_pct:
        reasons.append(f"bundled supply, top 10 circulating wallets hold {report.top10_pct:.0f}%")
    if (
        enrichment.unique_makers_24h is not None
        and candidate.token.txns_6h.total >= 100
        and enrichment.unique_makers_24h / candidate.token.txns_6h.total < 0.05
    ):
        reasons.append("manufactured tape, many trades from very few wallets")
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
        labels.append(f"{token.active_boosts} paid boost(s)")
    if candidate.recycled_label_count:
        labels.append(f"ticker reused by {candidate.recycled_label_count} other recent mint(s)")
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
    """Biggest run first, then the real trading behind it."""
    token = candidate.token
    return (
        float(len(candidate.kol_buyers)),
        token.price_change_24h,
        candidate.signals.turnover,
        token.volume_24h,
    )


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
        disqualifying = rug_or_bundle(candidate, settings)
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
    return runners, blocked
