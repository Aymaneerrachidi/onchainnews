from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re

from brief.config import Settings
from brief.ledger import Ledger
from brief.models import Candidate, CTORecord, Enrichment, Exclusion, SafetyReport, Signals, TokenSnapshot


def allowed_chains(settings: Settings) -> tuple[str, ...]:
    configured = settings.get("thresholds", "chains", ["solana"]) or ["solana"]
    return tuple(str(chain).strip().lower() for chain in configured if str(chain).strip())


def hard_filter(token: TokenSnapshot, settings: Settings, now: datetime | None = None) -> list[str]:
    thresholds = settings.section("thresholds")
    chains = allowed_chains(settings)
    reasons: list[str] = []
    if token.chain_id.lower() not in chains:
        reasons.append(f"chain {token.chain_id or 'unknown'} not in {', '.join(chains)}")
    if token.market_cap < float(thresholds["min_market_cap"]):
        reasons.append(f"market cap below ${float(thresholds['min_market_cap']):,.0f}")
    if token.liquidity_usd < float(thresholds["min_liquidity"]):
        reasons.append(f"liquidity below ${float(thresholds['min_liquidity']):,.0f}")
    if token.txns_6h.total <= 0:
        reasons.append("no trades in last 6h")
    min_volume = float(thresholds.get("min_volume_24h", 0))
    if token.volume_24h < min_volume:
        reasons.append(f"24h volume below ${min_volume:,.0f}")
    # A brand-new market cap nobody is trading against is not evidence of
    # strength: a thin float prices a handful of buys into a large notional
    # cap. Established tokens are left alone here — their own track already
    # demands far higher turnover, and applying this floor across the whole
    # universe emptied the pool the movers track draws from.
    min_turnover = float(thresholds.get("min_turnover_24h", 0))
    turnover = token.volume_24h / token.market_cap if token.market_cap else 0
    if min_turnover and turnover < min_turnover and _is_fresh(token, now):
        reasons.append(f"24h volume is only {turnover:.2f}x market cap")
    return reasons


def _is_fresh(token: TokenSnapshot, now: datetime | None) -> bool:
    """True for pairs under a day old, and for pairs with no known creation time."""
    if token.pair_created_at is None or now is None:
        return True
    return (now.astimezone(timezone.utc) - token.pair_created_at).total_seconds() < 86400


def volume_liquidity_limit(token: TokenSnapshot, settings: Settings, now: datetime) -> float:
    """Wash-trading proxy, scaled to the age of the pair.

    At launch, volume far above pool depth is the signature of a manufactured
    tape. On an established name a violent day legitimately turns the pool over
    many times, so the launch limit would reject exactly the strongest movers.
    """
    thresholds = settings.section("thresholds")
    strict = float(thresholds.get("max_volume_liquidity_ratio", 25))
    if token.pair_created_at is None:
        return strict
    age_hours = (now.astimezone(timezone.utc) - token.pair_created_at).total_seconds() / 3600
    if age_hours < 24:
        return strict
    return float(thresholds.get("max_volume_liquidity_ratio_established", strict))


def safety_gate(
    token: TokenSnapshot,
    report: SafetyReport,
    enrichment: Enrichment,
    settings: Settings,
    now: datetime,
) -> tuple[list[str], list[str]]:
    thresholds = settings.section("thresholds")
    excluded: list[str] = []
    warnings: list[str] = []

    if report.mint_authority_renounced is False or enrichment.mint_authority_renounced is False:
        excluded.append("mint authority live")
    if report.freeze_authority_disabled is False or enrichment.freeze_authority_disabled is False:
        excluded.append("freeze authority live")
    if report.lp_locked_or_burned_pct is not None and report.lp_locked_or_burned_pct <= 0:
        excluded.append("LP neither locked nor burned")
    if report.top10_pct is not None and report.top10_pct > float(thresholds["max_top10_pct"]):
        excluded.append(f"top10 {report.top10_pct:.0f}%")
    ratio = token.volume_24h / token.liquidity_usd if token.liquidity_usd else float("inf")
    ratio_limit = volume_liquidity_limit(token, settings, now)
    if ratio > ratio_limit:
        excluded.append(f"volume/liquidity {ratio:.1f}x above the {ratio_limit:.0f}x limit for its age")
    if report.mint_authority_renounced is None and enrichment.mint_authority_renounced is None:
        warnings.append("mint authority unavailable")
    if report.freeze_authority_disabled is None and enrichment.freeze_authority_disabled is None:
        warnings.append("freeze authority unavailable")
    if report.lp_locked_or_burned_pct is None:
        warnings.append("LP lock unavailable")
    if report.top10_pct is None:
        warnings.append("holder concentration unavailable")
    if (
        report.mint_authority_renounced is not None
        and enrichment.mint_authority_renounced is not None
        and report.mint_authority_renounced != enrichment.mint_authority_renounced
    ):
        warnings.append("RugCheck/Helius disagree on mint authority")
    if (
        report.freeze_authority_disabled is not None
        and enrichment.freeze_authority_disabled is not None
        and report.freeze_authority_disabled != enrichment.freeze_authority_disabled
    ):
        warnings.append("RugCheck/Helius disagree on freeze authority")
    if token.active_boosts and enrichment.holder_change_24h is None:
        warnings.append("paid boosts; organic holder growth unavailable")
    elif token.active_boosts and enrichment.holder_change_24h <= 0:
        warnings.append("paid boosts without holder growth")
    if (
        enrichment.unique_makers_24h is not None
        and token.txns_6h.total >= 100
        and enrichment.unique_makers_24h / token.txns_6h.total < 0.05
    ):
        excluded.append("high transaction count, few unique makers")
    min_social_age = float(settings.get("intelligence", "min_social_account_age_days", 3))
    if enrichment.social_resolves is False:
        excluded.append("linked X account does not resolve")
    if enrichment.social_account_age_days is not None and enrichment.social_account_age_days < min_social_age:
        excluded.append(f"linked X account only {enrichment.social_account_age_days:.1f}d old")
    elif token.socials and enrichment.social_account_age_days is None:
        warnings.append("social account age unavailable")
    return excluded, warnings


def compute_signals(token: TokenSnapshot, enrichment: Enrichment, cto: CTORecord | None, now: datetime) -> Signals:
    age = None
    if token.pair_created_at:
        age = max(0.0, (now.astimezone(timezone.utc) - token.pair_created_at).total_seconds() / 3600)
    maker_quality = None
    if enrichment.unique_makers_24h is not None and token.txns_6h.total:
        maker_quality = enrichment.unique_makers_24h / token.txns_6h.total
    cto_volume = None
    if cto and cto.claim_date:
        # Volume measured since the claim. A takeover claimed days ago still has
        # a measurable current tape; the old 24h cutoff left every older claim
        # with no vitality evidence at all, which silently emptied the track.
        claim_age = (now.astimezone(timezone.utc) - cto.claim_date).total_seconds() / 3600
        cto_volume = token.volume_6h if claim_age <= 6 else token.volume_24h
    return Signals(
        turnover=token.volume_24h / token.market_cap if token.market_cap else 0,
        acceleration=token.price_change_6h - token.price_change_24h / 4,
        buy_imbalance_1h=token.txns_1h.buy_ratio,
        buy_imbalance_6h=token.txns_6h.buy_ratio,
        liquidity_depth=token.liquidity_usd / token.market_cap if token.market_cap else 0,
        holder_growth_24h=enrichment.holder_change_24h,
        maker_quality=maker_quality,
        age_hours=age,
        cto_volume_since_claim=cto_volume,
        cto_holder_change=enrichment.holder_change_24h if cto else None,
    )


def rank_key(candidate: Candidate) -> tuple[float, ...]:
    signal = candidate.signals
    # A transparent lexicographic ordering: live CTO/newness, then current motion,
    # participation, turnover and depth. No pseudo-conviction score is emitted.
    claim_recency = candidate.cto.claim_date.timestamp() if candidate.cto and candidate.cto.claim_date else 0.0
    holder = float(signal.holder_growth_24h or 0)
    return (
        float(len(candidate.strength_reasons)),
        float(len(candidate.interest_reasons)),
        1.0 if candidate.cto else 0.0,
        claim_recency,
        1.0 if candidate.token.from_profile else 0.0,
        -float(candidate.token.active_boosts),
        -(signal.age_hours if signal.age_hours is not None else 1_000_000),
        signal.acceleration,
        signal.buy_imbalance_6h or 0,
        holder,
        signal.turnover,
        signal.liquidity_depth,
    )


def _label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def populate_editorial_reasons(candidate: Candidate) -> None:
    """Attach concrete evidence for the two client-facing editorial questions."""
    token = candidate.token
    signal = candidate.signals
    strength: list[str] = []
    interest: list[str] = []

    if signal.turnover >= 0.5:
        strength.append(f"24h turnover is {signal.turnover:.2f}x market cap")
    if signal.acceleration >= 10:
        strength.append(f"6h pace runs {signal.acceleration:+.0f}pp ahead of the 24h average")
    if signal.liquidity_depth >= 0.15:
        strength.append(f"liquidity is {signal.liquidity_depth:.0%} of market cap")
    if signal.buy_imbalance_6h is not None and signal.buy_imbalance_6h >= 0.55 and token.txns_6h.total >= 50:
        strength.append(f"{signal.buy_imbalance_6h:.0%} buys across {token.txns_6h.total:,} six-hour trades")
    if candidate.safety.top10_pct is not None and candidate.safety.top10_pct <= 20:
        strength.append(f"nominal top 10 holds {candidate.safety.top10_pct:.1f}%")
    if candidate.safety.lp_locked_or_burned_pct is not None and candidate.safety.lp_locked_or_burned_pct >= 80:
        strength.append(f"LP is {candidate.safety.lp_locked_or_burned_pct:.0f}% locked or burned")
    if signal.holder_growth_24h is not None and signal.holder_growth_24h > 0:
        strength.append(f"holders increased by {signal.holder_growth_24h:,}")

    if signal.age_hours is not None and signal.age_hours <= 6:
        interest.append(f"launched {signal.age_hours:.1f} hours before the report")
    if token.price_change_24h >= 25:
        interest.append(f"price is {token.price_change_24h:+.0f}% over 24 hours")
    if token.meta_slugs:
        interest.append(f"trades inside the {', '.join(sorted(token.meta_slugs))} meta rotation")
    if token.from_profile:
        interest.append("appeared in the latest Dexscreener profile feed")
    if candidate.cto and has_cto_vitality(candidate):
        interest.append("recent CTO has measurable post-claim activity")
    if token.socials:
        interest.append("launch has linked social context")

    candidate.strength_reasons = strength
    candidate.interest_reasons = interest
    candidate.read = describe_candidate(candidate)


def describe_candidate(candidate: Candidate) -> str:
    """A single descriptive sentence built only from measured values."""
    token = candidate.token
    signal = candidate.signals
    if signal.age_hours is not None and signal.age_hours < 48:
        opening = f"{signal.age_hours:.0f}h-old pair"
    elif signal.age_hours is not None:
        opening = f"{signal.age_hours / 24:.0f}d-old pair"
    else:
        opening = "pair age unavailable"
    if token.meta_slugs:
        opening += f" in the {sorted(token.meta_slugs)[0]} meta"
    parts = [
        f"{opening} at ${token.market_cap:,.0f} market cap",
        f"{token.price_change_24h:+.0f}% on the day",
        f"${token.volume_24h:,.0f} of 24h volume ({signal.turnover:.1f}x market cap)",
    ]
    if signal.buy_imbalance_6h is not None and token.txns_6h.total >= 50:
        parts.append(f"{signal.buy_imbalance_6h:.0%} of {token.txns_6h.total:,} six-hour trades were buys")
    if candidate.safety.lp_locked_or_burned_pct is not None:
        parts.append(f"LP {candidate.safety.lp_locked_or_burned_pct:.0f}% locked or burned")
    if candidate.safety.top10_pct is not None:
        parts.append(f"top 10 hold {candidate.safety.top10_pct:.0f}%")
    if candidate.cto and candidate.cto.claim_date:
        parts.append(f"community takeover claimed {candidate.cto.claim_date.strftime('%d %b')}")
    return f"${token.symbol} — " + ", ".join(parts) + "."


def is_mover(candidate: Candidate, settings: Settings) -> bool:
    """Already-listed names showing measurable motion today.

    Deliberately independent of the launch window: the client asks for the
    strongest coins *of the day*, which is rarely the same set as the coins
    born in the last 24 hours.
    """
    movers = settings.section("movers")
    token = candidate.token
    signal = candidate.signals
    if token.price_change_24h < float(movers.get("min_price_change_24h", 25)):
        return False
    if signal.turnover < float(movers.get("min_turnover", 0.5)):
        return False
    if token.volume_24h < float(movers.get("min_volume_24h", 100_000)):
        return False
    max_age_days = float(movers.get("max_age_days", 120))
    if signal.age_hours is not None and signal.age_hours > max_age_days * 24:
        return False
    if candidate.recycled_label_count:
        return False
    return len(candidate.strength_reasons) >= int(movers.get("min_strength_signals", 2))


def is_live_cto(candidate: Candidate, settings: Settings, now: datetime) -> bool:
    """A takeover recent enough to matter, with measurable post-claim activity."""
    section = settings.section("cto")
    if not candidate.cto or not has_cto_vitality(candidate):
        return False
    claim = candidate.cto.claim_date
    if claim is not None:
        age_days = (now.astimezone(timezone.utc) - claim).total_seconds() / 86400
        if age_days > float(section.get("max_claim_age_days", 7)):
            return False
    if candidate.recycled_label_count:
        return False
    return len(candidate.strength_reasons) >= int(section.get("min_strength_signals", 2))


def mover_rank_key(candidate: Candidate) -> tuple[float, ...]:
    """Strongest first, on the same transparent lexicographic basis as rank_key."""
    signal = candidate.signals
    return (
        float(len(candidate.strength_reasons)),
        candidate.token.price_change_24h,
        signal.turnover,
        signal.acceleration,
        signal.buy_imbalance_6h or 0,
        signal.liquidity_depth,
    )


def is_editorial_pick(candidate: Candidate, settings: Settings) -> bool:
    min_strength = int(settings.get("editorial", "min_strength_signals", 3))
    min_interest = int(settings.get("editorial", "min_interest_signals", 2))
    gaps: list[str] = []
    if len(candidate.strength_reasons) < min_strength:
        gaps.append(f"only {len(candidate.strength_reasons)}/{min_strength} strength signals")
    if len(candidate.interest_reasons) < min_interest:
        gaps.append(f"only {len(candidate.interest_reasons)}/{min_interest} interest signals")
    if candidate.recycled_label_count:
        gaps.append(f"ticker/name reused by {candidate.recycled_label_count} other recent mint(s)")
    candidate.editorial_gaps = gaps
    return not gaps


def has_cto_vitality(candidate: Candidate) -> bool:
    """Require recent measurable activity, not merely presence in the CTO feed."""
    if not candidate.cto:
        return False
    has_organic_context = bool(candidate.token.socials)
    recent_volume = candidate.signals.cto_volume_since_claim or 0
    holder_growth = candidate.signals.cto_holder_change or 0
    return (recent_volume > 0 and candidate.signals.turnover >= 0.2 and has_organic_context) or holder_growth > 0


def screen(
    tokens: list[TokenSnapshot],
    safety: dict[str, SafetyReport],
    enrichments: dict[str, Enrichment],
    ctos: dict[str, CTORecord],
    ledger: Ledger,
    settings: Settings,
    now: datetime,
) -> tuple[list[Candidate], list[Exclusion], list[Candidate]]:
    """Screen the universe into editorial candidates, exclusions and a journal pool.

    The third return value is every token that cleared the hard filter, built
    into a Candidate regardless of the safety gate or the novelty rules. The
    editorial tracks need a strict, de-duplicated shortlist; the daily journal
    needs the opposite -- a complete record of what ran, with its problems
    written on the row. Screening once and splitting here keeps both honest
    without paying for the universe twice.
    """
    candidates: list[Candidate] = []
    exclusions: list[Exclusion] = []
    journal_pool: list[Candidate] = []
    symbol_counts = Counter(_label(token.symbol) for token in tokens if _label(token.symbol))
    lookback_days = int(settings.get("editorial", "recycled_symbol_lookback_days", 30))
    for token in tokens:
        hard_reasons = hard_filter(token, settings, now)
        if hard_reasons:
            exclusions.append(Exclusion(token, hard_reasons, "hard filter"))
            continue
        report = safety.get(token.mint, SafetyReport(token.mint))
        enrichment = enrichments.get(token.mint, Enrichment())
        gate_reasons, warnings = safety_gate(token, report, enrichment, settings, now)
        candidate = Candidate(
            token=token,
            signals=compute_signals(token, enrichment, ctos.get(token.mint), now),
            safety=report,
            enrichment=enrichment,
            warnings=warnings,
            cto=ctos.get(token.mint),
        )
        symbol_key = _label(token.symbol)
        batch_reuse = max(0, symbol_counts.get(symbol_key, 1) - 1) if symbol_key else 0
        historical_reuse = ledger.recent_symbol_reuse(token.symbol, token.mint, now, lookback_days)
        candidate.recycled_label_count = batch_reuse + historical_reuse
        if candidate.recycled_label_count:
            candidate.warnings.append(
                f"ticker/name reuse proxy found {candidate.recycled_label_count} other recent mint(s)"
            )
        populate_editorial_reasons(candidate)
        journal_pool.append(candidate)

        if gate_reasons:
            exclusions.append(Exclusion(token, gate_reasons, "safety gate"))
            continue
        allowed, badge, multiple = ledger.novelty(
            token.mint, token.market_cap, now,
            int(settings.get("thresholds", "novelty_days")),
            float(settings.get("thresholds", "follow_up_multiple")),
            int(settings.get("thresholds", "retire_after_features")),
        )
        if not allowed:
            continue
        candidate.badges.append(badge)
        candidate.follow_up_multiple = multiple
        is_editorial_pick(candidate, settings)
        candidates.append(candidate)
    candidates.sort(key=rank_key, reverse=True)
    return candidates, exclusions, journal_pool
