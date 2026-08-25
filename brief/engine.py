from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from brief.config import Settings
from brief.holders import HolderSnapshotter, analyze_changes, collapse_clusters, unavailable_finding
from brief.intraday import candidate_from_pass, hard_blocked_pass, load_pulse_passes, pass_crosses_intraday_floor
from brief.intelligence import (
    anomaly_findings,
    cex_provenance_finding,
    creator_outflow_finding,
    data_quality_alerts,
    detect_lp_removal,
    holder_overlap,
    migration_detail,
    pool_liquidity_proxy,
)
from brief.journal import (
    build_journal,
    kol_trade_count,
    kol_traders,
    implausible_run, assign_lore, journal_rank_key, inorganic_reasons,
    kol_touch_required, limit_runner_board, publisher_quality_reasons,
    runner_universe_reasons,
    rug_or_bundle, risk_labels,
)
from brief.lore import attach_lore
from brief.newsletter import (
    explain_runs,
    newsletter_coin_limit,
    recap_coins,
    research_day,
    write_recap,
)
from brief.lifecycle import attach_lifecycles, build_structured_recap, persist_market_tape
from brief.kol import KolTracker
from brief.ledger import Ledger, iso
from brief.models import (
    Brief,
    Candidate,
    Enrichment,
    LaunchRecord,
    OnChainFinding,
    SafetyReport,
    SourceStatus,
    TokenSnapshot,
)
from brief.screen import (
    allowed_chains,
    compute_signals,
    describe_candidate,
    is_editorial_pick,
    is_live_cto,
    is_mover,
    mover_rank_key,
    populate_editorial_reasons,
    screen,
)
from brief.scoring import score_candidates
from brief.sources.birdeye import BirdeyeSource
from brief.sources.dexscreener import DexscreenerSource, merge_token_snapshots
from brief.sources.dune import DuneSource
from brief.sources.goplus import GoPlusSource, supports as goplus_supports
from brief.sources.gmgn import safety_from_evidence
from brief.sources.geckoterminal import GeckoTerminalSource
from brief.sources.gmgn import GmgnDiscovery, GmgnSource, aggregate_wallet_evidence
from brief.sources.helius import HeliusSource
from brief.sources.http import CachedHttpClient
from brief.sources.jupiter import JupiterSource
from brief.sources.openintel import OpenIntelSource
from brief.sources.rugcheck import RugCheckSource
from brief.sources.social import (
    SocialVerifier,
    build_dex_evidence,
    match_x_interactions,
    x_handle,
)
from brief.sources.x import XSource


log = logging.getLogger("brief.engine")


def _apply_intraday_snapshot(token: TokenSnapshot, fresh: TokenSnapshot, now: datetime) -> None:
    """Copy Dexscreener's exact short windows without replacing the base pair.

    GMGN/Birdeye can win the provider union because they carry the strongest
    daily market snapshot, but their six-hour buckets may be absent. Replacing
    the whole token would throw away that context; copying only the short
    windows lets the liveness gates read real data.
    """
    token.volume_6h = fresh.volume_6h
    token.volume_1h = fresh.volume_1h
    token.txns_6h = fresh.txns_6h
    token.txns_1h = fresh.txns_1h
    token.price_change_6h = fresh.price_change_6h
    token.price_change_1h = fresh.price_change_1h
    token.price_change_5m = fresh.price_change_5m
    token.intraday_known = fresh.intraday_known
    if fresh.pair_created_at is not None and (
        token.pair_created_at is None
        or (now - token.pair_created_at).total_seconds() < 60
    ):
        # A zero-age provider stamp is missing data, not a launch this minute.
        token.pair_created_at = fresh.pair_created_at
    if not token.socials and fresh.socials:
        token.socials = fresh.socials


def kol_discovery_mints(kol_activity: dict[str, object], settings: Settings) -> list[str]:
    """Mints worth sending back through market/safety discovery.

    Tracked-wallet activity is not enough to publish a coin. It is, however, a
    discovery source: if good wallets bought or realised SOL on a mint that
    Dexscreener's feeds did not surface, the mint still deserves the normal
    Dex/RugCheck/Helius gates.
    """
    section = settings.section("kol")
    # 0 or a negative value means "resolve every KOL-touched mint". Dexscreener
    # lookups are free and batched; the expensive Helius holder checks happen
    # later only on finalists.
    raw_max = section.get("max_mints_enriched", 80)
    max_mints = int(raw_max if raw_max is not None else 80)
    min_buyers = int(section.get("min_buyers_to_enrich", 1) or 1)
    min_participants = int(section.get("min_participants_to_enrich", 2) or 2)
    min_realised = float(section.get("min_realised_sol_to_enrich", 0) or 0)

    rows: list[tuple[float, str]] = []
    for mint, record in kol_activity.items():
        buyers = len(getattr(record, "buyers", []) or [])
        holders = len(getattr(record, "holders", []) or [])
        participants = getattr(record, "participants", 0)
        realised = float(getattr(record, "realised_sol", 0) or 0)
        spent = float(getattr(record, "sol_spent", 0) or 0)
        qualifies = (
            buyers >= min_buyers
            or participants >= min_participants
            or realised >= min_realised > 0
        )
        if not qualifies:
            continue
        score = buyers * 1000 + holders * 250 + participants * 100 + max(realised, 0) * 10 + spent
        rows.append((score, mint))
    rows.sort(reverse=True)
    if max_mints <= 0:
        return [mint for _, mint in rows]
    return [mint for _, mint in rows[:max_mints]]


def _kol_touch_count(candidate: Candidate) -> int:
    local = len(kol_traders(candidate))
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    flow = gmgn.get("walletFlow", {}) or {}
    gmgn_wallets = set(flow.get("kolBuyers", [])) | set(flow.get("kolSellers", []))
    return max(local, len(gmgn_wallets), int(gmgn.get("kolCount") or 0))


def _kol_record_touch_count(record: object) -> int:
    return len(
        set(getattr(record, "buyers", []) or [])
        | set(getattr(record, "holders", []) or [])
        | set(getattr(record, "sellers", []) or [])
    )


def _closed_kol_pnl(flows: list[object]) -> float:
    """Cash PnL from wallets that both entered and exited inside the window.

    The aggregate wallet-flow value treats an unsold position as a cash
    outflow. That is useful accounting, but it is not realised PnL and must not
    turn a crowded, still-held runner into a losing trade.
    """
    return sum(
        float(getattr(flow, "realised_sol", 0.0) or 0.0)
        for flow in flows
        if bool(getattr(flow, "bought", False)) and bool(getattr(flow, "sold", False))
    )


def _kol_profit_or_open_conviction(
    *,
    realised: float,
    buyers: int,
    holders: int,
    flows: list[object],
    section: dict[str, object],
) -> bool:
    min_realised = float(section.get("runner_min_realised_sol", 5.0) or 5.0)
    if realised > 0 and realised >= min_realised:
        return True
    # Open positions make net cash flow look negative. Accept that case only
    # when a genuine wallet crowd remains and completed round trips are net
    # profitable. This cannot rescue a one-wallet punt.
    return (
        buyers >= int(section.get("runner_open_min_buyers", 5) or 5)
        and holders >= int(section.get("runner_open_min_holders", 2) or 2)
        and _closed_kol_pnl(flows) > 0
    )


def _token_age_hours(token: TokenSnapshot, now: datetime) -> float | None:
    if token.pair_created_at is None:
        return None
    return max(0.0, (now.astimezone(token.pair_created_at.tzinfo) - token.pair_created_at).total_seconds() / 3600)


def _kol_tape_prequalifies(token: TokenSnapshot, record: object, settings: Settings, now: datetime) -> bool:
    section = settings.section("kol")
    if not bool(section.get("runner_lane_enabled", True)):
        return False
    if token.chain_id.lower() != "solana":
        return False
    age = _token_age_hours(token, now)
    max_age = float(section.get("runner_max_age_hours", 24) or 24)
    if age is None or (max_age and age > max_age):
        return False
    buyers = len(set(getattr(record, "buyers", []) or []))
    participants = _kol_record_touch_count(record)
    realised = float(getattr(record, "realised_sol", 0.0) or 0.0)
    spent = float(getattr(record, "sol_spent", 0.0) or 0.0)
    holders = len(set(getattr(record, "holders", []) or []))
    flows = list(getattr(record, "flows", []) or [])
    if buyers < int(section.get("runner_min_buyers", 5) or 5):
        return False
    if participants < int(section.get("runner_min_participants", 5) or 5):
        return False
    if bool(section.get("runner_require_positive_realised", True)) and not _kol_profit_or_open_conviction(
        realised=realised, buyers=buyers, holders=holders, flows=flows, section=section
    ):
        return False
    min_spent = float(section.get("runner_min_sol_spent", 0.0) or 0.0)
    if min_spent and spent < min_spent:
        return False
    return True


def _kol_flow_qualifies(candidate: Candidate, settings: Settings) -> bool:
    section = settings.section("kol")
    if not bool(section.get("runner_lane_enabled", True)):
        return False
    if candidate.token.chain_id.lower() != "solana":
        return False
    if bool(section.get("runner_require_safety", False)) and candidate.safety.source == "unavailable":
        return False
    if (
        bool(section.get("runner_require_holder_count", False))
        and not (candidate.enrichment.holder_count or candidate.safety.holder_count)
    ):
        return False
    age = candidate.signals.age_hours
    max_age = float(section.get("runner_max_age_hours", 72) or 72)
    if age is None:
        return False
    if max_age and age > max_age:
        return False
    buyers = len(set(candidate.kol_buyers))
    participants = _kol_touch_count(candidate)
    realised = float(candidate.kol_realised_sol or 0.0)
    spent = float(candidate.kol_sol_spent or 0.0)
    min_buyers = int(section.get("runner_min_buyers", 5) or 5)
    min_participants = int(section.get("runner_min_participants", 5) or 5)
    min_realised = float(section.get("runner_min_realised_sol", 5.0) or 5.0)
    min_spent = float(section.get("runner_min_sol_spent", 0.0) or 0.0)
    if buyers < min_buyers:
        return False
    if participants < min_participants:
        return False
    if bool(section.get("runner_require_positive_realised", True)) and not _kol_profit_or_open_conviction(
        realised=realised,
        buyers=buyers,
        holders=len(set(candidate.kol_holders)),
        flows=list(candidate.kol_flows),
        section=section,
    ):
        return False
    if min_spent and spent < min_spent:
        return False
    clears_current_floor = (
        max(candidate.token.market_cap, float(candidate.observed_peak_market_cap or 0.0))
        >= float(section.get("runner_min_market_cap", 200_000) or 200_000)
        and candidate.token.volume_24h >= float(section.get("runner_min_volume_24h", 100_000) or 100_000)
    )
    if not clears_current_floor:
        return False
    return True


def _kol_flow_reason(candidate: Candidate) -> str:
    buyers = len(set(candidate.kol_buyers))
    holders = len(set(candidate.kol_holders))
    sellers = len(set(candidate.kol_sellers))
    bits: list[str] = []
    if buyers:
        bits.append(f"{buyers} tracked wallet{'s' if buyers != 1 else ''} bought")
    if holders:
        bits.append(f"{holders} still holding")
    if sellers:
        bits.append(f"{sellers} sold/trimmed")
    if abs(candidate.kol_realised_sol) >= 0.1:
        bits.append(f"{candidate.kol_realised_sol:+.1f} SOL realised")
    if candidate.kol_sol_spent >= 0.1:
        bits.append(f"{candidate.kol_sol_spent:.1f} SOL spent")
    return "; ".join(bits) or "tracked wallet activity"


def _kol_hard_reasons(
    candidate: Candidate,
    settings: Settings,
    now: datetime,
) -> tuple[list[str], list[str]]:
    """Split irreversible/manufactured risk from normal end-of-day fading.

    Low recent activity is valuable recap context: it says the runner peaked
    and died. It is not evidence that the earlier run was fake. Contract risk,
    holder packs, wash volume, dust cadence and one-sided books remain hard
    exclusions.
    """
    hard = list(rug_or_bundle(candidate, settings))
    # KOL popularity is discovery/confirmation, never a security override.
    # Keep the promotion lane fail-closed on the evidence the normal publisher
    # gate requires, while leaving purely editorial issues to normal ranking.
    security_terms = (
        "contract-security provider unavailable",
        "mint authority/contract mintability",
        "freeze/pause/blacklist powers",
        "holder count unavailable",
        "holders, below publisher floor",
        "lp lock/burn status unavailable",
        "lp only ",
        "top-10 concentration unavailable",
        "top 10 hold ",
        "liquidity below publisher floor",
    )
    for reason in publisher_quality_reasons(candidate, settings, now):
        if any(term in reason.casefold() for term in security_terms):
            hard.append(reason)
    soft: list[str] = []
    peak = float(candidate.observed_peak_market_cap or 0.0)
    peak_floor = float(settings.get("kol", "runner_min_market_cap", 200_000) or 200_000)
    max_peak_turnover = float(settings.get("kol", "runner_max_peak_turnover", 10.0) or 10.0)
    verified_faded_run = (
        peak >= peak_floor
        and candidate.token.market_cap < peak_floor
        and candidate.token.volume_24h / max(peak, 1.0) <= max_peak_turnover
    )
    for reason in inorganic_reasons(candidate, settings):
        lowered = reason.casefold()
        holder_floor = int(settings.get("kol", "runner_min_holders", 0) or 0)
        softer_holder_floor = (
            " holders, which is not a market yet" in lowered
            and candidate.safety.holder_count is not None
            and candidate.safety.holder_count >= holder_floor
        )
        # Market cap and liquidity collapse after a runner fades. Ratios against
        # that final dust value cannot prove that the candle-verified run was
        # wash traded. Peak-normalised turnover remains capped, while all
        # holder, bundle and authority checks stay hard.
        stale_end_state_ratio = verified_faded_run and (
            "wash-trading shape:" in lowered or "its market cap traded" in lowered
        )
        if stale_end_state_ratio:
            continue
        if "the move is over:" in lowered or softer_holder_floor or (
            lowered.startswith("only ") and " trades in 24h" in lowered
        ):
            soft.append(reason)
        else:
            hard.append(reason)
    return list(dict.fromkeys(hard)), list(dict.fromkeys(soft))


def _attach_kol_record(candidate: Candidate, record: object, scanned_wallets: int) -> None:
    candidate.kol_wallets_scanned = scanned_wallets
    candidate.kol_buyers = list(getattr(record, "buyers", []) or [])
    candidate.kol_sellers = list(getattr(record, "sellers", []) or [])
    candidate.kol_holders = list(getattr(record, "holders", []) or [])
    candidate.kol_realised_sol = float(getattr(record, "realised_sol", 0.0) or 0.0)
    candidate.kol_sol_spent = float(getattr(record, "sol_spent", 0.0) or 0.0)
    candidate.kol_flows = list(getattr(record, "flows", []) or [])


def _kol_tape_candidates(
    tokens: list[TokenSnapshot],
    safety: dict[str, SafetyReport],
    enrichments: dict[str, Enrichment],
    kol_activity: dict[str, object],
    settings: Settings,
    now: datetime,
    scanned_wallets: int,
    existing_mints: set[str],
    observed_peaks: dict[str, float] | None = None,
) -> list[Candidate]:
    """Build candidates for fresh KOL-profit names that already faded below floors."""
    result: list[Candidate] = []
    for token in tokens:
        if token.mint in existing_mints:
            continue
        record = kol_activity.get(token.mint)
        if not record or not _kol_tape_prequalifies(token, record, settings, now):
            continue
        candidate = Candidate(
            token=token,
            signals=compute_signals(token, enrichments.get(token.mint, Enrichment()), None, now),
            safety=safety.get(token.mint, SafetyReport(token.mint)),
            enrichment=enrichments.get(token.mint, Enrichment()),
        )
        candidate.track = "KOL"
        candidate.observed_peak_market_cap = (observed_peaks or {}).get(token.mint)
        _attach_kol_record(candidate, record, scanned_wallets)
        populate_editorial_reasons(candidate)
        result.append(candidate)
    return result


def _add_kol_flow_runners(
    runners: list[Candidate],
    blocked: list[Candidate],
    journal_pool: list[Candidate],
    settings: Settings,
    now: datetime,
) -> tuple[list[Candidate], list[Candidate], int]:
    """Promote KOL-discovered names that pass hard market/safety checks.

    The normal journal starts from Dex motion and then asks whether wallets
    touched it. This lane starts from wallets and asks whether Dex/RugCheck still
    make the coin safe enough to recap.
    """
    if not bool(settings.get("kol", "runner_lane_enabled", True)):
        return runners, blocked, 0
    # The morning product is KOL-confirmed by definition. The hourly tape is
    # intentionally broader and runs with [kol].enabled=false, so this filter
    # applies only to the fully enriched daily build.
    if bool(settings.get("kol", "enabled", True)) and bool(settings.get("kol", "wallets", []) or []):
        retained: list[Candidate] = []
        rejected_mints = {candidate.token.mint for candidate in blocked}
        for candidate in runners:
            hard, _ = _kol_hard_reasons(candidate, settings, now)
            if _kol_flow_qualifies(candidate, settings) and not hard:
                retained.append(candidate)
                continue
            if candidate.token.mint not in rejected_mints:
                if not candidate.risk_labels:
                    candidate.risk_labels = [
                        "daily KOL confirmation below the two-wallet profitable-flow floor"
                    ]
                blocked.append(candidate)
                rejected_mints.add(candidate.token.mint)
        runners = retained
    existing = {candidate.token.mint for candidate in runners}
    blocked_by_mint = {candidate.token.mint: candidate for candidate in blocked}
    promoted: list[Candidate] = []
    still_blocked = [candidate for candidate in blocked]
    max_added = int(settings.get("kol", "runner_lane_max", 10) or 10)
    max_manipulation = float(settings.get("kol", "runner_max_manipulation", 75.0) or 75.0)

    for candidate in sorted(
        journal_pool,
        key=lambda c: (
            max(float(c.kol_realised_sol or 0), 0.0),
            len(set(c.kol_buyers)),
            _kol_touch_count(c),
            c.scores.get("runner", 0.0),
            c.token.volume_24h,
        ),
        reverse=True,
    ):
        if len(promoted) >= max_added:
            break
        if candidate.token.mint in existing or not _kol_flow_qualifies(candidate, settings):
            continue
        hard_reasons, soft_reasons = _kol_hard_reasons(candidate, settings, now)
        if hard_reasons:
            if candidate.token.mint not in blocked_by_mint:
                candidate.risk_labels = hard_reasons
                still_blocked.append(candidate)
                blocked_by_mint[candidate.token.mint] = candidate
            continue
        peak = float(candidate.observed_peak_market_cap or 0.0)
        peak_floor = float(settings.get("kol", "runner_min_market_cap", 200_000) or 200_000)
        peak_turnover = candidate.token.volume_24h / max(peak, 1.0) if peak else float("inf")
        verified_faded_run = (
            peak >= peak_floor
            and candidate.token.market_cap < peak_floor
            and peak_turnover <= float(settings.get("kol", "runner_max_peak_turnover", 10.0) or 10.0)
        )
        if candidate.scores.get("manipulation", 0.0) > max_manipulation and not verified_faded_run:
            candidate.risk_labels = [f"manipulation score {candidate.scores.get('manipulation', 0.0):.0f} above KOL-flow ceiling"]
            if candidate.token.mint not in blocked_by_mint:
                still_blocked.append(candidate)
                blocked_by_mint[candidate.token.mint] = candidate
            continue
        candidate.track = "KOL"
        if verified_faded_run:
            candidate.read = (
                f"${candidate.token.symbol} hit a candle-verified ${peak:,.0f} intraday market cap "
                f"before fading to ${candidate.token.market_cap:,.0f} by the cutoff; "
                f"{len(set(candidate.kol_buyers))} tracked wallets bought and the chart recorded "
                f"${candidate.token.volume_24h:,.0f} of 24-hour volume."
            )
            # These sentences were produced from the final dust market cap and
            # would restate the exact denominator error corrected above.
            candidate.dex_evidence = []
        elif not candidate.read:
            candidate.read = describe_candidate(candidate)
        current_floor = (
            candidate.token.market_cap >= float(settings.get("kol", "runner_min_market_cap", 200_000) or 200_000)
            and candidate.token.volume_24h >= float(settings.get("kol", "runner_min_volume_24h", 100_000) or 100_000)
        )
        prefix = "KOL-flow runner" if current_floor else "KOL-tape runner; faded below current floor"
        label = f"{prefix}: {_kol_flow_reason(candidate)}"
        contextual_labels = risk_labels(candidate, settings, now)
        if verified_faded_run:
            contextual_labels = [
                item for item in contextual_labels
                if "thin pool," not in item.casefold() and "its market cap traded" not in item.casefold()
            ]
        labels = [label, *soft_reasons, *contextual_labels]
        candidate.risk_labels = list(dict.fromkeys(labels))
        promoted.append(candidate)
        existing.add(candidate.token.mint)

    if promoted:
        runners = [*runners, *promoted]
        runners.sort(
            key=lambda c: (
                len(set(c.kol_buyers)),
                _kol_touch_count(c),
                max(float(c.kol_realised_sol or 0), 0.0),
                c.scores.get("runner", 0.0),
                c.token.volume_24h,
            ),
            reverse=True,
        )
        promoted_mints = {candidate.token.mint for candidate in promoted}
        still_blocked = [candidate for candidate in still_blocked if candidate.token.mint not in promoted_mints]
    return runners, still_blocked, len(promoted)


def _short_address(address: str | None) -> str:
    if not address:
        return "unknown"
    return f"{address[:5]}…{address[-4:]}" if len(address) > 12 else address


async def _apply_holder_cluster_precheck(
    candidates: list[Candidate],
    safety: dict[str, SafetyReport],
    ledger: Ledger,
    helius: HeliusSource,
    settings: Settings,
    now: datetime,
    *,
    commit: bool,
) -> SourceStatus:
    """Run the expensive same-funder check on the strongest Solana runners.

    RugCheck top10 can look harmless when one entity splits across many wallets.
    This check traces the top holder wallets' first SOL funder and turns that
    disguised cluster into a hard inorganic label before the journal can fill
    empty slots with it.
    """
    holder_settings = settings.section("holders")
    if not bool(holder_settings.get("runner_cluster_check_enabled", True)):
        return SourceStatus("Runner holder clusters", False, "disabled in [holders]")
    if not helius.configured:
        return SourceStatus("Runner holder clusters", False, "HELIUS_API_KEY is not configured")

    limit = int(holder_settings.get("runner_cluster_check_limit", 8) or 8)
    top_n = int(holder_settings.get("runner_cluster_top_holders", 24) or 24)
    min_market_cap = float(holder_settings.get("runner_cluster_min_market_cap", 200_000) or 200_000)
    min_volume = float(holder_settings.get("runner_cluster_min_volume_24h", 250_000) or 250_000)
    max_age_hours = float(holder_settings.get("runner_cluster_max_age_hours", 48) or 48)
    max_holder_count = int(holder_settings.get("runner_cluster_max_holder_count", 30_000) or 30_000)
    min_members = int(holder_settings.get("same_funder_cluster_min_wallets", 6) or 6)
    min_cluster_pct = float(holder_settings.get("same_funder_cluster_min_supply_pct", 2.0) or 2.0)
    max_window = float(holder_settings.get("same_funder_cluster_window_minutes", 240) or 240)
    fresh_hours = float(holder_settings.get("fresh_wallet_hours", 24) or 24)
    min_fresh_wallets = int(holder_settings.get("fresh_wallet_pack_min_wallets", 8) or 8)
    min_fresh_pct = float(holder_settings.get("fresh_wallet_pack_min_supply_pct", 3.0) or 3.0)
    max_history_calls = int(holder_settings.get("runner_cluster_max_history_calls", 160) or 160)

    eligible = [
        candidate for candidate in candidates
        if candidate.token.chain_id.lower() == "solana"
        and candidate.token.market_cap >= min_market_cap
        and candidate.token.volume_24h >= min_volume
        and candidate.signals.age_hours is not None
        and candidate.signals.age_hours <= max_age_hours
        and (
            (candidate.safety.holder_count or candidate.enrichment.holder_count or 0) <= max_holder_count
        )
    ]
    if bool(settings.get("journal", "require_kol_trade_for_publish", False)):
        eligible = [
            candidate for candidate in eligible
            if _kol_touch_count(candidate) > 0
        ]
    eligible.sort(
        key=lambda candidate: (
            candidate.scores.get("runner", 0.0),
            candidate.scores.get("organic", 0.0),
            -candidate.scores.get("manipulation", 100.0),
            len(kol_traders(candidate)),
            candidate.token.volume_24h,
        ),
        reverse=True,
    )
    eligible = eligible[:limit]
    if not eligible:
        return SourceStatus("Runner holder clusters", True, "no Solana runner candidates needed deep holder tracing")

    snapshotter = HolderSnapshotter(ledger, helius, settings)
    snapshotter.history_budget = max_history_calls
    known_cex = set(str(value) for value in holder_settings.get("known_cex_wallets", []))

    checked = 0
    flagged = 0
    partial = 0
    failures = 0
    for candidate in eligible:
        token = candidate.token
        report = safety.get(token.mint) or SafetyReport(token.mint)
        try:
            snapshot = await snapshotter.pull(token, report, now, commit=commit)
            if candidate.enrichment.holder_count is None:
                candidate.enrichment.holder_count = snapshot.holder_count
            if candidate.safety.holder_count is None:
                candidate.safety.holder_count = snapshot.holder_count
            if candidate.safety.top10_pct is None:
                candidate.safety.top10_pct = snapshot.top10_pct

            owners = [balance.owner for balance in snapshot.balances[:top_n]]
            trace_results = await asyncio.gather(*(snapshotter.trace_wallet(owner, now) for owner in owners))
            traces = {
                owner: trace
                for owner, trace in zip(owners, trace_results)
                if trace is not None
            }
            expected = len(owners)
            checked += 1
            if expected and len(traces) < expected:
                partial += 1
            cluster = collapse_clusters(snapshot.balances, traces, top_n, known_cex)
            if commit:
                ledger.record_cluster_snapshot(
                    token.mint, now, cluster.effective_top10_pct, cluster.cluster_count, cluster.coverage
                )

            cluster_window_ok = (
                max_window <= 0
                or cluster.largest_window_minutes is None
                or cluster.largest_window_minutes <= max_window
            )
            added_flags = 0
            if (
                cluster.largest_members >= min_members
                and cluster.largest_pct >= min_cluster_pct
                and cluster_window_ok
            ):
                window_text = (
                    f" inside {cluster.largest_window_minutes:.0f}m"
                    if cluster.largest_window_minutes is not None
                    else ""
                )
                effective = (
                    f"; effective top10 after clustering {cluster.effective_top10_pct:.1f}%"
                    if cluster.effective_top10_pct is not None
                    else ""
                )
                candidate.warnings.append(
                    "same-funder holder cluster: "
                    f"{cluster.largest_members} traced top holders hold {cluster.largest_pct:.1f}% "
                    f"funded by {_short_address(cluster.largest_funder)}{window_text}{effective}"
                )
                added_flags += 1

            fresh_cutoff = now - timedelta(hours=fresh_hours)
            amount_by_owner = {balance.owner: balance.amount for balance in snapshot.balances[:top_n]}
            fresh_owners = [
                owner for owner, trace in traces.items()
                if trace.wallet_created_at is not None and trace.wallet_created_at >= fresh_cutoff
            ]
            fresh_amount = sum(amount_by_owner.get(owner, 0.0) for owner in fresh_owners)
            fresh_pct = fresh_amount / snapshot.total_amount * 100 if snapshot.total_amount else 0.0
            if len(fresh_owners) >= min_fresh_wallets and fresh_pct >= min_fresh_pct:
                candidate.warnings.append(
                    "fresh-wallet holder pack: "
                    f"{len(fresh_owners)}/{len(traces)} traced top holders were created in the last "
                    f"{fresh_hours:.0f}h and hold {fresh_pct:.1f}%"
                )
                added_flags += 1

            if added_flags:
                flagged += 1
        except Exception as exc:
            log.warning("runner_holder_cluster_precheck_failed mint=%s error=%s", token.mint, exc)
            failures += 1

    detail = (
        f"{checked}/{len(eligible)} top Solana runners traced; {flagged} flagged; "
        f"{partial} partial; wallet-history calls {snapshotter.history_calls}"
    )
    if failures:
        detail += f"; {failures} failed"
    return SourceStatus("Runner holder clusters", failures == 0, detail)


async def _map_resilient(items: list[str], fn, concurrency: int = 12):
    semaphore = asyncio.Semaphore(concurrency)

    async def run(item: str):
        async with semaphore:
            try:
                return item, await fn(item), None
            except Exception as exc:
                log.warning("source_item_failed mint=%s error=%s", item, exc)
                return item, None, exc

    return await asyncio.gather(*(run(item) for item in items))


def _add_material(
    material: dict[str, list[tuple[int, str]]], mint: str, priority: int, line: str | None
) -> None:
    if line:
        material.setdefault(mint, []).append((priority, line))


def _merge_material(
    findings: list[OnChainFinding],
    material: dict[str, list[tuple[int, str]]],
    token_by_mint: dict[str, TokenSnapshot],
) -> list[OnChainFinding]:
    by_mint = {finding.mint: finding for finding in findings}
    for mint, entries in material.items():
        entries.sort(key=lambda item: item[0])
        token = token_by_mint.get(mint)
        if not token:
            continue
        priority, headline = entries[0]
        extra = [line for _, line in entries[1:]]
        previous = by_mint.get(mint)
        if previous:
            extra.extend([previous.headline, *previous.details])
            previous.priority = min(previous.priority, priority)
            previous.headline = headline
            previous.details = extra
            previous.status = "available"
        else:
            finding = OnChainFinding(mint, token.symbol, priority, headline, extra)
            findings.append(finding)
            by_mint[mint] = finding
    return findings


def _cluster_history_line(history: list[dict]) -> str | None:
    if not history:
        return None
    tokens: dict[str, float | None] = {}
    for match in history:
        outcomes = match.get("outcomes") or {}
        for mint in match.get("tokens") or []:
            tokens[mint] = outcomes.get(mint)
    rendered = []
    for mint, outcome in list(tokens.items())[:5]:
        label = f"{mint[:4]}…{mint[-4:]}" if len(mint) > 12 else mint
        rendered.append(f"{label} {outcome:+.0f}% at 7d" if outcome is not None else f"{label} outcome pending")
    return f"shared-funder cluster appeared on {len(tokens)} prior token(s): {', '.join(rendered)} — launch infrastructure is being reused"


def _launch_signals(token: TokenSnapshot, *, is_cto: bool) -> list[str]:
    signals: list[str] = []
    turnover = token.volume_24h / token.market_cap if token.market_cap else 0
    liquidity_depth = token.liquidity_usd / token.market_cap if token.market_cap else 0
    buy_ratio = token.txns_6h.buy_ratio
    acceleration = token.price_change_6h - token.price_change_24h / 4
    if turnover >= 1:
        signals.append(f"24h turnover {turnover:.1f}x market cap")
    if liquidity_depth >= .15:
        signals.append(f"liquidity depth {liquidity_depth:.0%} of market cap")
    if buy_ratio is not None and buy_ratio >= .60:
        signals.append(f"6h flow {buy_ratio:.0%} buys")
    if acceleration >= 10:
        signals.append(f"6h acceleration {acceleration:+.0f}pp versus its 24h pace")
    if token.socials:
        signals.append("linked social presence")
    if is_cto:
        signals.append("community takeover recorded in the window")
    if token.from_profile:
        signals.append("new Dexscreener profile activity")
    return signals


async def build_brief(
    settings: Settings,
    ledger: Ledger,
    *,
    commit: bool = True,
    now: datetime | None = None,
    replay_date: str | None = None,
) -> Brief:
    timezone = ZoneInfo(str(settings.get("run", "timezone", "UTC")))
    now = now or datetime.now(timezone)
    fixture_value = settings.get("run", "fixture_path")
    fixture_path = None
    if fixture_value:
        candidate = settings.root / str(fixture_value)
        fixture_path = candidate if candidate.is_absolute() else candidate.resolve()
    http = CachedHttpClient(
        ledger,
        timeout=float(settings.get("run", "request_timeout_seconds", 15)),
        fixture_path=fixture_path,
        replay_date=replay_date,
    )
    cache = settings.section("cache")
    urls = settings.section("sources")
    statuses: list[SourceStatus] = []
    onchain: list[OnChainFinding] = []
    material: dict[str, list[tuple[int, str]]] = {}
    quality_alert_list: list[str] = []
    weekly_notes: list[str] = []
    try:
        if commit:
            ledger.sync_cluster_outcomes()
            retention = int(settings.get("run", "archive_retention_days", 14))
            pruned = ledger.prune_archive(now, retention)
            if pruned:
                log.info("archive_pruned rows=%s retention_days=%s", pruned, retention)
        window_start = now - timedelta(hours=24)
        collector_rows = ledger.launch_events_between(window_start, now)
        collector_started_raw = (
            ledger.collector_state("coverage_contiguous_since")
            or ledger.collector_state("started_at")
        )
        collector_started_at = datetime.fromisoformat(collector_started_raw) if collector_started_raw else None
        chains = allowed_chains(settings)
        dex = DexscreenerSource(
            http,
            str(urls.get("dexscreener_base_url", "https://api.dexscreener.com")),
            int(cache.get("discovery_ttl_seconds", 600)),
            int(cache.get("pairs_ttl_seconds", 60)),
            chains=chains,
        )
        try:
            metas, tokens, ctos, degraded = await dex.universe()
            statuses.append(SourceStatus(
                "Dexscreener", True,
                f"partial: {', '.join(degraded)}" if degraded else "all discovery feeds available",
            ))
        except Exception as exc:
            log.exception("Dexscreener universe failed")
            metas, tokens, ctos = [], [], {}
            statuses.append(SourceStatus("Dexscreener", False, str(exc)))

        # GMGN adds launch-stage breadth, attention ranks, platform-tagged KOL
        # and smart-money trades, ATH context, and manipulation fields. It is a
        # union source: no GMGN flag can erase a run found by another provider.
        gmgn = GmgnSource(
            timeout=float(settings.get("gmgn", "timeout_seconds", 35)),
            ledger=ledger,
            cache_ttl=int(settings.get("gmgn", "cache_ttl_seconds", 900)),
            min_interval_seconds=float(settings.get("gmgn", "min_interval_seconds", 1.25)),
            chains=chains,
        )
        gmgn_evidence: dict[str, dict[str, object]] = {}
        gmgn_wallet_events: list[dict[str, object]] = []
        gmgn_discovery = (
            await gmgn.discover(now)
            if bool(settings.get("gmgn", "enabled", True)) and not fixture_path and not replay_date
            else GmgnDiscovery(statuses=[SourceStatus(
                "GMGN", False,
                "disabled for deterministic fixture/replay" if (fixture_path or replay_date) else "disabled in [gmgn]",
            )])
        )
        statuses.extend(gmgn_discovery.statuses)
        if commit and gmgn_discovery.statuses:
            gmgn_status = gmgn_discovery.statuses[-1]
            ledger.record_provider_health("gmgn", gmgn_status.available, now, gmgn_status.detail)
        if gmgn_discovery.tokens:
            tokens = merge_token_snapshots([*tokens, *gmgn_discovery.tokens])
        gmgn_evidence = gmgn_discovery.evidence
        gmgn_wallet_events = gmgn_discovery.wallet_events
        if commit:
            for event in gmgn_wallet_events:
                occurred = event.get("occurredAt")
                if not isinstance(occurred, datetime) or occurred < window_start or occurred > now:
                    continue
                ledger.record_wallet_event(
                    event_key=str(event.get("eventKey") or ""),
                    mint=str(event.get("mint") or ""),
                    wallet=str(event.get("wallet") or ""),
                    wallet_kind=str(event.get("walletKind") or "unknown"),
                    side=str(event.get("side") or "unknown"),
                    occurred_at=occurred,
                    amount_usd=float(event.get("amountUsd") or 0) or None,
                    payload={"name": event.get("name")},
                )

        # Independent keyless market ranking. Provider top-N pages are not the
        # same universe: CYBERLEEK was the first GeckoTerminal trending pool at
        # +80% / $34M volume while absent from both sampled primary feeds. This
        # lane supplies exact contracts only; Dexscreener hydration and the
        # normal safety/KOL gates still decide whether anything is published.
        if bool(settings.get("geckoterminal", "enabled", True)) and not fixture_path and not replay_date:
            gecko_discovery = GeckoTerminalSource(
                http,
                str(urls.get("geckoterminal_base_url", "https://api.geckoterminal.com/api/v2")),
                ttl=int(cache.get("discovery_ttl_seconds", 600)),
                requests_per_minute=int(settings.get("geckoterminal", "requests_per_minute", 25) or 25),
                request_interval_seconds=float(settings.get("geckoterminal", "request_interval_seconds", 2.5) or 2.5),
            )
            try:
                ranked = await gecko_discovery.trending_addresses(
                    chains,
                    pages=int(settings.get("geckoterminal", "trending_pages", 3) or 3),
                )
                known = {(token.chain_id.lower(), token.mint) for token in tokens}
                independent_additions = sum(key not in known for key in ranked)
                # Hydrate every ranked contract, including mints another source
                # already named. A broad GMGN row may omit the short windows;
                # exact Dex data is what proves that the coin actually moved.
                discovered = await dex.token_pairs(ranked)
                direct_markets = gecko_discovery.trending_snapshots
                tokens = merge_token_snapshots([*tokens, *direct_markets, *discovered])
                statuses.append(SourceStatus(
                    "GeckoTerminal trending discovery",
                    not gecko_discovery.partial_error,
                    f"{len(ranked)} exact contracts ranked across supported chains; "
                    f"{len(discovered)} exact markets hydrated; "
                    f"{len(direct_markets)} ranked pool snapshots retained; "
                    f"{independent_additions} were independent additions"
                    + (
                        f"; partial: {gecko_discovery.partial_error[:120]}"
                        if gecko_discovery.partial_error else ""
                    ),
                ))
            except Exception as exc:
                log.warning("geckoterminal_discovery_failed error=%s", exc)
                statuses.append(SourceStatus("GeckoTerminal trending discovery", False, str(exc)[:160]))

        # Birdeye ranks the whole token universe by 24h volume. Those names
        # enter the brief only as mover candidates, so they are pre-filtered on
        # the motion gate before reaching the expensive safety path.
        birdeye_added = 0
        if bool(settings.get("birdeye", "enabled", True)) and bool(settings.get("movers", "enabled", True)):
            birdeye = BirdeyeSource(
                http,
                str(urls.get("birdeye_base_url", "https://public-api.birdeye.so")),
                os.getenv("BIRDEYE_API_KEY"),
                int(cache.get("discovery_ttl_seconds", 600)),
                page_size=int(settings.get("birdeye", "page_size", 50)),
                requests_per_minute=int(settings.get("birdeye", "requests_per_minute", 50)),
                request_interval=float(settings.get("birdeye", "request_interval_seconds", 1.1)),
            )
            if birdeye.configured:
                try:
                    ranked: list[tuple[str, str]] = []
                    # Birdeye's package-access matrix limits Token List V3 to
                    # Solana. Some endpoint reference pages expose additional
                    # x-chain enum values, but free-package calls on those
                    # networks return 429/unsupported responses. Dexscreener,
                    # GeckoTerminal and GMGN remain the EVM discovery lanes.
                    birdeye_chains = [
                        chain for chain in chains if chain.lower() == "solana"
                    ]
                    for chain in birdeye_chains:
                        birdeye.chain = chain
                        for mint in await birdeye.top_by_volume(
                            int(settings.get("birdeye", "max_tokens", 600)),
                            float(settings.get("thresholds", "min_liquidity", 20000)),
                            float(settings.get("thresholds", "min_market_cap", 250000)),
                        ):
                            ranked.append((chain, mint))
                    known = {token.mint for token in tokens}
                    ranked_tokens = await dex.token_pairs(
                        [(chain, mint) for chain, mint in ranked if mint not in known]
                    )
                    # The journal is the widest net in the report, so discovery
                    # is pre-filtered on its bar rather than the movers bar.
                    journal_section = settings.section("journal")
                    movers_section = settings.section("movers")
                    fresh_enough = min(
                        float(movers_section.get("min_price_change_24h", 25)),
                        float(journal_section.get("min_fresh_change_pct", 30)),
                    )
                    min_volume = min(
                        float(movers_section.get("min_volume_24h", 100000)),
                        float(journal_section.get("min_volume_24h", 50000)),
                    )
                    motion = [
                        token for token in ranked_tokens
                        if token.price_change_24h >= fresh_enough and token.volume_24h >= min_volume
                    ]
                    birdeye_added = len(motion)
                    tokens = merge_token_snapshots([*tokens, *motion])
                    statuses.append(SourceStatus(
                        "Birdeye ranked discovery", True,
                        f"{len(ranked):,} Solana tokens ranked by 24h volume; "
                        f"{birdeye_added} cleared the motion gate",
                    ))
                except Exception as exc:
                    log.warning("birdeye_discovery_failed error=%s", exc)
                    statuses.append(SourceStatus("Birdeye ranked discovery", False, str(exc)))
            else:
                statuses.append(SourceStatus(
                    "Birdeye ranked discovery", False,
                    "BIRDEYE_API_KEY not configured; discovery is limited to Dexscreener feeds",
                ))

        indexed_launch_mints: set[str] = set()
        if settings.get("launch_index", "enabled", True) and collector_rows:
            cap = int(settings.get("launch_index", "max_mints_enriched_per_run", 12000))
            launch_rows_for_lookup = collector_rows[:cap]
            try:
                indexed_tokens = await dex.token_pairs([str(row["mint"]) for row in launch_rows_for_lookup])
                event_time = {
                    str(row["mint"]): datetime.fromisoformat(str(row["created_at"]))
                    for row in collector_rows
                }
                for token in indexed_tokens:
                    indexed_launch_mints.add(token.mint)
                    created = event_time.get(token.mint)
                    if created and (token.pair_created_at is None or created < token.pair_created_at):
                        token.pair_created_at = created
                tokens = merge_token_snapshots([*tokens, *indexed_tokens])
            except Exception as exc:
                log.warning("launch_index_market_enrichment_failed error=%s", exc)
                statuses.append(SourceStatus("Launch-index market enrichment", False, str(exc)))

        if collector_started_at:
            coverage_start = max(window_start, collector_started_at.astimezone(now.tzinfo))
            complete = collector_started_at <= window_start
            statuses.append(SourceStatus(
                "On-chain launch collector",
                complete,
                f"{len(collector_rows):,} Pump.fun creates stored since {coverage_start.strftime('%d %b %H:%M %Z')}"
                + ("; full 24h window" if complete else "; partial window while the collector builds history"),
            ))
        else:
            statuses.append(SourceStatus(
                "On-chain launch collector", False,
                "not started; launch counts currently fall back to Dexscreener discovery feeds",
            ))

        helius = HeliusSource(
            http,
            str(urls.get("helius_base_url", "https://mainnet.helius-rpc.com")),
            os.getenv("HELIUS_API_KEY"),
            int(cache.get("keyed_ttl_seconds", 900)),
            requests_per_minute=int(settings.get("holders", "helius_requests_per_minute", 100)),
            holder_page_limit=int(settings.get("holders", "holder_page_limit", 1000)),
            max_holder_pages=int(settings.get("holders", "max_holder_pages", 100)),
        )
        dune_wallets: dict[str, str] = {}
        dune = DuneSource(http, settings)
        if dune.enabled:
            if dune.configured:
                try:
                    alpha_wallets = await dune.alpha_wallets(now)
                    dune_wallets = {wallet.address: wallet.name for wallet in alpha_wallets}
                    statuses.append(SourceStatus(
                        "Dune PumpFun alpha wallets", True,
                        f"{len(dune_wallets)} active wallets accepted from query {dune.alpha_query_id}; "
                        f"top {dune.skip_top_n} skipped and bot-sized profit capped",
                    ))
                except Exception as exc:
                    log.warning("dune_alpha_wallets_failed error=%s", exc)
                    statuses.append(SourceStatus("Dune PumpFun alpha wallets", False, str(exc)))
            else:
                statuses.append(SourceStatus(
                    "Dune PumpFun alpha wallets", False,
                    "DUNE_API_KEY not configured; using static Kolscan wallets only",
                ))
        # Tracked-wallet flow is a check on the day's runners: a coin earns its
        # place by moving, and this answers whether the wallets that usually
        # catch these moves were in it. Scanned before screening so the answer
        # is available while the record is being built.
        kol_tracker = KolTracker(helius, settings, extra_wallets=dune_wallets)
        kol_activity = {}
        if kol_tracker.enabled:
            try:
                kol_activity = await kol_tracker.activity(now)
                statuses.append(SourceStatus(
                    "KOL wallet flow", kol_tracker.failed == 0,
                    f"{kol_tracker.scanned}/{len(kol_tracker.wallets)} wallets scanned; "
                    f"{kol_tracker.pages_scanned} complete history pages and "
                    f"{kol_tracker.transactions_scanned:,} transactions read; "
                    f"activity in {len(kol_activity)} mints"
                    + (f"; {kol_tracker.failed} wallet(s) incomplete and excluded" if kol_tracker.failed else ""),
                ))
            except Exception as exc:
                log.warning("kol_tracking_failed error=%s", exc)
                statuses.append(SourceStatus("KOL wallet flow", False, str(exc)))
        else:
            kol_detail = (
                "legacy Helius wallet sweep disabled; GMGN is the primary broad KOL/smart-money source"
                if not bool(settings.get("kol", "enabled", True))
                else "no wallets configured; add addresses to [kol].wallets in config.toml"
            )
            statuses.append(SourceStatus(
                "KOL wallet flow", False,
                kol_detail,
            ))

        if kol_activity:
            known_mints = {token.mint for token in tokens}
            kol_mints = [mint for mint in kol_discovery_mints(kol_activity, settings) if mint not in known_mints]
            if kol_mints:
                try:
                    kol_tokens = await dex.token_pairs(kol_mints)
                    tokens = merge_token_snapshots([*tokens, *kol_tokens])
                    statuses.append(SourceStatus(
                        "KOL mint discovery", True,
                        f"{len(kol_tokens)}/{len(kol_mints)} tracked-wallet mints resolved through Dexscreener",
                    ))
                except Exception as exc:
                    log.warning("kol_mint_discovery_failed error=%s", exc)
                    statuses.append(SourceStatus("KOL mint discovery", False, str(exc)))

        # The hourly scan is our historical high-water mark. Bring every mint
        # that crossed the tape gate back into today's universe before safety
        # checks, even if it has faded out of Dexscreener's discovery feeds.
        pulse_passes_for_day: dict[str, dict[str, object]] = {}
        if bool(settings.get("journal", "include_intraday_pulse_runners", True)):
            pulse_passes_for_day = load_pulse_passes(settings, window_start, now)
            known_mints = {token.mint for token in tokens}
            missing_pulse_mints = [mint for mint in pulse_passes_for_day if mint not in known_mints]
            if missing_pulse_mints:
                try:
                    pulse_tokens = await dex.token_pairs([
                        (str((pulse_passes_for_day[mint] or {}).get("chain") or "solana"), mint)
                        for mint in missing_pulse_mints
                    ])
                    tokens = merge_token_snapshots([*tokens, *pulse_tokens])
                    statuses.append(SourceStatus(
                        "Intraday tape discovery",
                        True,
                        f"{len(pulse_tokens)}/{len(missing_pulse_mints)} faded runner mints resolved",
                    ))
                except Exception as exc:
                    log.warning("pulse_mint_discovery_failed error=%s", exc)
                    statuses.append(SourceStatus("Intraday tape discovery", False, str(exc)))

        # GMGN hourly candles verify what happened inside the actual trailing
        # day.  This is especially important for 24-30h-old launches and for
        # older coins that spiked and faded before the morning report.
        if (
            bool(settings.get("gmgn", "kline_verification_enabled", True))
            and not fixture_path
            and not replay_date
        ):
            statuses.append(await gmgn.enrich_runner_klines(
                tokens,
                gmgn_evidence,
                now=now,
                limit=int(settings.get("gmgn", "kline_candidate_limit", 40)),
                min_kol_count=int(settings.get("journal", "min_kol_trades_for_publish", 1) or 1),
            ))

        # Hydrate missing 6h/1h windows before the hard screen. Previously this
        # happened after ``screen()``, so a healthy GMGN/Birdeye candidate with
        # an empty 6h bucket was rejected as "no trades" before Dexscreener was
        # ever asked. Limit the lookup to tokens that already clear every other
        # cheap market floor, keeping the free endpoint workload bounded.
        if not fixture_path and not replay_date:
            thresholds = settings.section("thresholds")
            journal_rules = settings.section("journal")
            motion_floor = min(
                float(journal_rules.get("min_fresh_change_pct", 30) or 30),
                float(journal_rules.get("min_daily_change_pct", 50) or 50),
            )
            min_kol = int(journal_rules.get("min_kol_trades_for_publish", 1) or 1)

            def runner_shaped(token: TokenSnapshot) -> bool:
                evidence = gmgn_evidence.get(token.mint, {}) or {}
                return bool(
                    abs(float(token.price_change_24h or 0)) >= motion_floor
                    or float(evidence.get("kline24hPeakFromOpenPct") or 0) >= motion_floor
                    or int(evidence.get("kolCount") or 0) >= min_kol
                    or token.mint in kol_activity
                )

            pre_screen_sparse = [
                token for token in tokens
                if token.chain_id.lower() in chains
                and token.market_cap >= float(thresholds.get("min_market_cap", 0) or 0)
                and token.liquidity_usd >= float(thresholds.get("min_liquidity", 0) or 0)
                and token.volume_24h >= float(thresholds.get("min_volume_24h", 0) or 0)
                and token.txns_6h.total <= 0
                and runner_shaped(token)
            ]
            if pre_screen_sparse:
                try:
                    hydrated_rows = merge_token_snapshots(await dex.token_pairs([
                        (token.chain_id, token.mint) for token in pre_screen_sparse
                    ]))
                    hydrated = {token.mint: token for token in hydrated_rows}
                    filled = 0
                    for token in pre_screen_sparse:
                        fresh = hydrated.get(token.mint)
                        if fresh is None:
                            continue
                        _apply_intraday_snapshot(token, fresh, now)
                        filled += 1
                    statuses.append(SourceStatus(
                        "Pre-screen intraday hydration",
                        filled == len(pre_screen_sparse),
                        f"{filled}/{len(pre_screen_sparse)} missing 6h windows resolved before market gates",
                    ))
                except Exception as exc:
                    log.warning("pre_screen_intraday_hydration_failed error=%s", exc)
                    statuses.append(SourceStatus("Pre-screen intraday hydration", False, str(exc)[:120]))

        # Store the complete provider union before any safety/editorial gate.
        # This is what lets tomorrow's recap prove that a questionable token
        # still ran, peaked, and round-tripped instead of disappearing.
        new_milestones = persist_market_tape(
            ledger, tokens, now, provider="provider-union", commit=commit
        )
        statuses.append(SourceStatus(
            "Lifecycle market tape",
            True,
            f"{len(tokens)} current observations stored; {sum(len(v) for v in new_milestones.values())} new milestones",
        ))
        token_by_mint = {token.mint: token for token in tokens}
        peak_floor = float(settings.get("journal", "peak_market_cap_floor", 250_000) or 250_000)

        def peak_provenance(token: TokenSnapshot) -> float:
            """Best verified peak for size eligibility; motion is gated later."""
            token_gmgn = gmgn_evidence.get(token.mint, {}) or {}
            gmgn_ath = float(token_gmgn.get("athMarketCap") or 0)
            kline_peak = float(token_gmgn.get("kline24hPeakMarketCap") or 0)
            lifecycle = ledger.lifecycle(token.mint, window_start, now)
            local_peak = float((lifecycle or {}).get("peak_market_cap") or 0)
            daily_move_peak = float(token.market_cap or 0) if token.price_change_24h >= float(
                settings.get("journal", "min_daily_change_pct", 25.0) or 25.0
            ) else 0.0
            # Lifetime ATH supplies the universal $1M eligibility check. Old
            # coins are still rejected later unless their trailing-day candles
            # clear the size-adjusted movement gate.
            return max(gmgn_ath, kline_peak, local_peak, daily_move_peak)

        peak_tape = {
            token.mint: peak
            for token in tokens
            if token.chain_id.lower() in chains
            and (peak := peak_provenance(token)) >= peak_floor
        }
        hard_pass_mints = [
            token.mint for token in tokens
            if token.chain_id.lower() in chains
            and token.market_cap >= float(settings.get("thresholds", "min_market_cap"))
            and token.liquidity_usd >= float(settings.get("thresholds", "min_liquidity"))
            and token.txns_6h.total > 0
        ]
        kol_tape_mints = [
            token.mint for token in tokens
            if (record := kol_activity.get(token.mint))
            and _kol_tape_prequalifies(token, record, settings, now)
        ]
        rug = RugCheckSource(
            http,
            str(urls.get("rugcheck_base_url", "https://api.rugcheck.xyz/v1")),
            int(cache.get("safety_ttl_seconds", 3600)),
            requests_per_minute=int(settings.get("rugcheck", "requests_per_minute", 45) or 45),
        )
        # Safety is answered by a different service per chain. RugCheck knows
        # Solana; GoPlus knows the EVM chains and also answers questions Solana
        # does not have, like whether a sale is taxed or can be blocked.
        chain_of = {token.mint: token.chain_id.lower() for token in tokens}
        solana_safety_pool = sorted(
            {
                m for m in [*hard_pass_mints, *kol_tape_mints, *peak_tape]
                if chain_of.get(m) == "solana"
            }
        )

        # GoPlus is free and rate-limited, and most of the universe never
        # reaches the record. Asking about all of it spent the budget before the
        # coins that mattered were reached, so the market gates that need no
        # safety data run first and only the plausible names are verified.
        journal_section = settings.section("journal")
        fresh_bar = float(journal_section.get("min_fresh_change_pct", 30))
        old_bar = (float(journal_section.get("old_coin_multiple", 5)) - 1) * 100
        min_journal_volume = float(journal_section.get("min_volume_24h", 50_000))

        def could_be_reported(token: TokenSnapshot) -> bool:
            if token.volume_24h < min_journal_volume:
                return False
            return token.price_change_24h >= min(fresh_bar, old_bar)

        # Safety calls are reserved for candidates that can still appear in the
        # recap. Always retain proven intraday peaks and KOL-discovered names;
        # flat raw-discovery rows cannot become runners and used to consume the
        # RugCheck allowance before the real tape was reached.
        solana_mints = [
            mint
            for mint in solana_safety_pool
            if mint in peak_tape
            or mint in kol_tape_mints
            or mint in ctos
            or (
                (token := token_by_mint.get(mint)) is not None
                and could_be_reported(token)
            )
        ]

        evm_mints: dict[str, list[str]] = {}
        unchecked_chains: set[str] = set()
        skipped = 0
        for mint in dict.fromkeys([*hard_pass_mints, *peak_tape]):
            chain = chain_of.get(mint, "")
            if chain == "solana":
                continue
            if not goplus_supports(chain):
                unchecked_chains.add(chain)
                continue
            token = token_by_mint.get(mint)
            if token is not None and mint not in peak_tape and not could_be_reported(token):
                skipped += 1
                continue
            evm_mints.setdefault(chain, []).append(mint)
        max_goplus_per_chain = int(settings.get("goplus", "max_tokens_per_chain", 20) or 20)
        if max_goplus_per_chain > 0:
            for chain, mints in list(evm_mints.items()):
                ranked_mints = sorted(
                    dict.fromkeys(mints),
                    key=lambda mint: (
                        bool((gmgn_evidence.get(mint, {}) or {}).get("organicQualified")),
                        int((gmgn_evidence.get(mint, {}) or {}).get("kolCount") or 0),
                        float(token_by_mint[mint].volume_24h or 0) if mint in token_by_mint else 0.0,
                        float(token_by_mint[mint].market_cap or 0) if mint in token_by_mint else 0.0,
                    ),
                    reverse=True,
                )
                skipped += max(0, len(ranked_mints) - max_goplus_per_chain)
                evm_mints[chain] = ranked_mints[:max_goplus_per_chain]
        if skipped:
            log.info("goplus_prefilter skipped=%s of %s evm tokens", skipped, skipped + sum(len(v) for v in evm_mints.values()))

        safety: dict[str, SafetyReport] = {}
        if solana_mints:
            rug_results = await _map_resilient(solana_mints, rug.report)
            safety.update({m: v for m, v, _ in rug_results if v is not None})
            rug_failures = sum(error is not None for _, _, error in rug_results)
            statuses.append(SourceStatus(
                "RugCheck (Solana)", rug_failures == 0,
                f"partial: {rug_failures}/{len(rug_results)} token reports unavailable"
                if rug_failures else f"{len(solana_mints)} Solana tokens checked",
            ))

        if evm_mints:
            goplus = GoPlusSource(
                http,
                str(urls.get("goplus_base_url", "https://api.gopluslabs.io")),
                int(cache.get("safety_ttl_seconds", 3600)),
                requests_per_minute=int(settings.get("goplus", "requests_per_minute", 30)),
            )
            checked = 0
            for chain, mints in evm_mints.items():
                try:
                    found = await goplus.reports(chain, mints)
                    safety.update(found)
                    checked += len(found)
                except Exception as exc:
                    log.warning("goplus_chain_failed chain=%s error=%s", chain, exc)
            wanted = sum(len(v) for v in evm_mints.values())
            statuses.append(SourceStatus(
                "GoPlus (EVM)", checked == wanted,
                f"{checked}/{wanted} tokens checked across {', '.join(sorted(evm_mints))}",
            ))

        if unchecked_chains:
            # GoPlus does not reach these chains, but GMGN already answered the
            # questions that decide whether a coin can be named: can it be sold,
            # what does it tax, how concentrated is it. Falling back to that
            # beats shipping a ticker with nothing behind it.
            recovered = 0
            for mint in dict.fromkeys([*hard_pass_mints, *peak_tape]):
                if chain_of.get(mint, "") not in unchecked_chains or mint in safety:
                    continue
                evidence = (gmgn_evidence.get(mint) or {})
                if not evidence:
                    continue
                safety[mint] = safety_from_evidence(mint, evidence)
                recovered += 1
            statuses.append(SourceStatus(
                "Contract safety (GMGN)", recovered > 0,
                f"{recovered} token(s) on {', '.join(sorted(unchecked_chains))} screened from GMGN; "
                "GoPlus does not cover these chains"
                if recovered
                else f"no safety source covers {', '.join(sorted(unchecked_chains))}; "
                "tokens there are labelled unverified",
            ))

        enrichments: dict[str, Enrichment] = {
            mint: Enrichment() for mint in {*hard_pass_mints, *kol_tape_mints, *peak_tape}
        }
        if helius.configured and solana_mints:
            try:
                batch = await helius.enrich_batch(solana_mints)
                enrichments.update(batch)
                helius_failures = len(solana_mints) - len(batch)
            except Exception as exc:
                log.warning("helius_batch_enrichment_failed error=%s", exc)
                helius_failures = len(solana_mints)
            statuses.append(SourceStatus(
                "Helius", helius_failures == 0,
                f"partial: {helius_failures}/{len(solana_mints)} token enrichments unavailable" if helius_failures else "batched authority cross-check available",
            ))
        else:
            statuses.append(SourceStatus("Helius", False, "not configured; holder and authority cross-checks unavailable"))

        # Holder-count precedence is deliberate: Helius reads Solana token
        # accounts directly; GMGN supplies a full-chain count everywhere else;
        # contract-security providers are the fallback. Some GoPlus responses
        # contain a small sampled count, which previously rejected a 1,300-
        # holder token as if it had only 104 holders.
        for mint, evidence in gmgn_evidence.items():
            report = safety.setdefault(mint, SafetyReport(mint))
            enrichment = enrichments.setdefault(mint, Enrichment())
            helius_holders = (
                int(enrichment.holder_count)
                if enrichment.source == "helius" and enrichment.holder_count
                else None
            )
            gmgn_holders = int(evidence["holders"]) if evidence.get("holders") else None
            if helius_holders is not None:
                report.holder_count = helius_holders
            elif gmgn_holders is not None:
                report.holder_count = gmgn_holders
            if report.top10_pct is None and evidence.get("top10Pct") is not None:
                report.top10_pct = float(evidence["top10Pct"])
            if helius_holders is None and gmgn_holders is not None:
                enrichment.holder_count = gmgn_holders

        # A morning snapshot cannot prove where a faded coin traded earlier.
        # Reconstruct the 30-hour high from free hourly candles. KOL profit by
        # itself never qualifies a small scalp as a runner.
        observed_peaks: dict[str, float] = {}
        faded_peak_mints = [
            mint for mint in kol_tape_mints
            if (token := token_by_mint.get(mint))
            and token.market_cap < float(settings.get("kol", "runner_min_market_cap", 200_000) or 200_000)
        ]
        if faded_peak_mints:
            gecko = GeckoTerminalSource(
                http,
                str(urls.get("geckoterminal_base_url", "https://api.geckoterminal.com/api/v2")),
                ttl=int(cache.get("ohlcv_ttl_seconds", 3600)),
                requests_per_minute=int(settings.get("kol", "ohlcv_requests_per_minute", 25)),
                request_interval_seconds=float(settings.get("kol", "ohlcv_request_interval_seconds", 2.5)),
            )
            peak_results = await _map_resilient(
                faded_peak_mints,
                lambda mint: gecko.peak_market_cap(token_by_mint[mint]),
                concurrency=3,
            )
            observed_peaks = {
                mint: float(value) for mint, value, _ in peak_results if value is not None
            }
            peak_floor = float(settings.get("kol", "runner_min_market_cap", 200_000) or 200_000)
            crossed = sum(peak >= peak_floor for peak in observed_peaks.values())
            statuses.append(SourceStatus(
                "GeckoTerminal peak verification",
                len(observed_peaks) == len(faded_peak_mints),
                f"{len(observed_peaks)}/{len(faded_peak_mints)} faded KOL charts checked; "
                f"{crossed} crossed the ${peak_floor:,.0f} peak floor",
            ))

        # Public X widget metadata is only a resolution/age check; no sentiment is scraped.
        social_inputs = {token.mint: token.socials for token in tokens if token.mint in hard_pass_mints}
        handles = {mint: x_handle(socials) for mint, socials in social_inputs.items()}
        if any(handles.values()):
            verifier = SocialVerifier(
                http,
                str(urls.get("x_public_metadata_url", "https://cdn.syndication.twimg.com/widgets/followbutton/info.json")),
            )
            verified = await verifier.verify(social_inputs, now)
            for mint, (resolves, age_days) in verified.items():
                enrichment = enrichments.setdefault(mint, Enrichment())
                enrichment.social_resolves = resolves
                enrichment.social_account_age_days = age_days
            resolved = sum(value[0] is True for value in verified.values())
            unavailable = sum(bool(value[0] is None and handles.get(mint)) for mint, value in verified.items())
            statuses.append(SourceStatus(
                "Social verification", unavailable == 0,
                f"{resolved}/{sum(bool(value) for value in handles.values())} linked X accounts resolved; {unavailable} unavailable",
            ))
        else:
            statuses.append(SourceStatus("Social verification", True, "no linked X handles in the screened universe"))

        intelligence = settings.section("intelligence")
        anomaly_threshold = float(intelligence.get("anomaly_zscore", 3))
        for token in tokens:
            if token.mint not in hard_pass_mints:
                continue
            prior_pair = ledger.prior_pair_observation(token.mint, token.pair_address)
            if commit:
                prior_pair = ledger.record_pair_observation(
                    token.mint, token.pair_address, token.dex_id, token.pair_created_at, now
                ) or prior_pair
            migration = migration_detail(prior_pair["dex_id"] if prior_pair else None, token)
            if migration:
                event_key = f"migration:{token.mint}:{token.pair_address}"
                if not commit or ledger.record_event_once(event_key, token.mint, "migration", now, migration):
                    _add_material(material, token.mint, 2, migration)

            metrics = {
                "turnover": token.volume_24h / token.market_cap if token.market_cap else 0,
                "volume_24h": token.volume_24h,
                "liquidity_usd": token.liquidity_usd,
                "buy_imbalance_6h": token.txns_6h.buy_ratio or 0,
            }
            zscores = ledger.metric_zscores(
                token.mint, metrics, now,
                int(intelligence.get("anomaly_trailing_days", 30)),
                int(intelligence.get("anomaly_min_samples", 7)),
                record=commit,
            )
            for line in anomaly_findings(token, zscores, anomaly_threshold):
                _add_material(material, token.mint, 4, line)

            state = ledger.feature_state(token.mint)
            if state and (state["retired"] or state["times_featured"] >= int(settings.get("thresholds", "retire_after_features"))):
                multiple = token.market_cap / state["mcap_at_last_feature"] if state["mcap_at_last_feature"] else 0
                if multiple >= float(intelligence.get("retired_reappearance_multiple", 1.5)):
                    line = f"retired token returned at {multiple:.1f}× its last feature market cap — organic reappearance cleared the material-change threshold"
                    key = f"reappearance:{token.mint}:{int(multiple)}"
                    if not commit or ledger.record_event_once(key, token.mint, "reappearance", now, line):
                        _add_material(material, token.mint, 4, line)

        current_snapshots = {}
        traces_by_mint = {}
        if bool(settings.get("holders", "enabled", True)):
            watch_limit = int(settings.get("holders", "watchlist_limit", 5))
            watched_rows = ledger.watched()
            missing_watched = [row["mint"] for row in watched_rows if row["mint"] not in token_by_mint]
            if missing_watched:
                try:
                    for token in await dex.token_pairs(missing_watched):
                        token_by_mint[token.mint] = token
                except Exception as exc:
                    statuses.append(SourceStatus("Watchlist pair lookup", False, str(exc)))
            auto_candidates = sorted(
                [
                    token_by_mint[mint] for mint in hard_pass_mints
                    if mint in token_by_mint and token_by_mint[mint].chain_id.lower() == "solana"
                ],
                key=lambda token: (
                    1 if token.mint in ctos else 0,
                    token.pair_created_at.timestamp() if token.pair_created_at else 0,
                    token.volume_24h / token.market_cap if token.market_cap else 0,
                ),
                reverse=True,
            )
            if commit:
                watched_rows = ledger.fill_auto_watchlist(auto_candidates, watch_limit, now)
            elif not watched_rows:
                watched_rows = [
                    {"mint": token.mint, "symbol": token.symbol, "reason": "dry-run"}
                    for token in auto_candidates[:watch_limit]
                ]
            watched_rows = watched_rows[:watch_limit]
            snapshotter = HolderSnapshotter(ledger, helius, settings)
            snapshot_failures = 0
            known_cex = set(str(value) for value in settings.get("holders", "known_cex_wallets", []))
            for row in watched_rows:
                token = token_by_mint.get(row["mint"])
                if token is None:
                    placeholder = TokenSnapshot(
                        mint=row["mint"], symbol=row["symbol"], name=row["symbol"], chain_id="solana",
                        pair_address="", url="", price_usd=0, market_cap=0, liquidity_usd=0,
                        volume_24h=0, volume_6h=0, price_change_24h=0, price_change_6h=0,
                        pair_created_at=None,
                    )
                    onchain.append(unavailable_finding(placeholder, "Dexscreener pair lookup unavailable; holder pull not attempted"))
                    snapshot_failures += 1
                    continue
                if not helius.configured:
                    onchain.append(unavailable_finding(token, "HELIUS_API_KEY is not configured"))
                    snapshot_failures += 1
                    continue
                try:
                    if token.mint not in safety:
                        try:
                            safety[token.mint] = await rug.report(token.mint)
                        except Exception:
                            safety[token.mint] = SafetyReport(token.mint)
                    report = safety[token.mint]
                    snapshot = await snapshotter.pull(token, report, now, commit=commit)
                    # The paginated DAS snapshot is the authoritative Solana
                    # holder count and concentration view for downstream gates.
                    report.holder_count = snapshot.holder_count
                    report.top10_pct = snapshot.top10_pct
                    current_snapshots[token.mint] = snapshot
                    prior = ledger.snapshot_at_or_before(
                        token.mint,
                        now - timedelta(hours=float(settings.get("holders", "daily_snapshot_min_age_hours", 20))),
                        exclude_taken_at=iso(now),
                    )
                    enrichment = enrichments.setdefault(token.mint, Enrichment(source="helius"))
                    enrichment.holder_count = snapshot.holder_count
                    if prior:
                        enrichment.holder_change_24h = snapshot.holder_count - prior["holder_count"]
                    traces, acquisitions, expected = await snapshotter.trace_top_wallets(snapshot, now)
                    traces_by_mint[token.mint] = traces
                    cluster = collapse_clusters(
                        snapshot.balances, traces,
                        int(settings.get("holders", "cluster_top_holders", 100)), known_cex,
                    )
                    if commit:
                        ledger.record_cluster_snapshot(
                            token.mint, now, cluster.effective_top10_pct, cluster.cluster_count, cluster.coverage
                        )
                    for group in cluster.groups:
                        history = ledger.cluster_prior_history(group.funder, group.wallets, token.mint)
                        _add_material(material, token.mint, -2, _cluster_history_line(history))
                        if commit:
                            ledger.register_cluster(group.funder, group.wallets, token.mint, now)

                    cex_line = cex_provenance_finding(
                        token, traces, known_cex,
                        float(settings.get("holders", "funding_cluster_window_minutes", 15)),
                    )
                    _add_material(material, token.mint, 1, cex_line)

                    if report.lp_vaults:
                        vault_balances = await helius.token_account_balances(list(report.lp_vaults))
                        proxy = pool_liquidity_proxy(vault_balances)
                        previous_pool = ledger.pool_at_or_before(token.mint, now - timedelta(hours=20))
                        if previous_pool is None:
                            previous_pool = ledger.latest_pool_snapshot(token.mint)
                        removal = detect_lp_removal(
                            previous_pool["liquidity_proxy"] if previous_pool else None,
                            proxy,
                            float(intelligence.get("lp_removal_alert_pct", 10)),
                        )
                        if removal is not None:
                            _add_material(material, token.mint, -3, f"pool balance proxy fell {removal:.1f}% since the prior poll — material liquidity was removed")
                        previous_proxy = previous_pool["liquidity_proxy"] if previous_pool else None
                        if previous_proxy and proxy and proxy > previous_proxy:
                            addition = (proxy / previous_proxy - 1) * 100
                            if addition >= float(intelligence.get("lp_add_report_pct", 10)):
                                _add_material(material, token.mint, 5, f"pool balance proxy rose {addition:.1f}% since the prior daily baseline — liquidity was added")
                        if commit:
                            ledger.record_pool_snapshot(token.mint, now, vault_balances, proxy, token.liquidity_usd)

                    creator = report.creator
                    if creator:
                        creator_trace = await snapshotter.trace_wallet(creator, now)
                        funder = creator_trace.first_funder if creator_trace else None
                        linked = [creator]
                        if funder:
                            linked.extend(owner for owner, trace in traces.items() if trace.first_funder == funder)
                        linked = list(dict.fromkeys(linked))
                        amounts = {balance.owner: balance.amount for balance in snapshot.balances}
                        creator_amount = sum(amounts.get(owner, 0) for owner in linked)
                        supply_pct = creator_amount / snapshot.total_amount * 100 if snapshot.total_amount else 0
                        if commit:
                            ledger.record_creator_snapshot(token.mint, now, creator, linked, creator_amount, supply_pct)
                        history = ledger.creator_history(
                            token.mint, int(intelligence.get("creator_sustained_days", 2)) + 2
                        )
                        _add_material(material, token.mint, 0, creator_outflow_finding(
                            history,
                            int(intelligence.get("creator_sustained_days", 2)),
                            float(intelligence.get("creator_outflow_alert_pp", .25)),
                        ))

                    early_window = timedelta(hours=float(intelligence.get("early_holder_hours", 3)))
                    early_owners = []
                    if token.pair_created_at:
                        for owner, acquisition in acquisitions.items():
                            acquired = acquisition.first_acquired_at
                            if acquired and token.pair_created_at <= acquired <= token.pair_created_at + early_window:
                                early_owners.append(owner)
                                if commit:
                                    ledger.record_early_wallet(token.mint, owner, acquired, now)
                    smart = ledger.smart_money_matches(
                        token.mint, early_owners,
                        float(intelligence.get("smart_money_winner_return_pct", 100)),
                        int(intelligence.get("smart_money_min_prior_wins", 2)),
                    )
                    if len(smart) >= int(intelligence.get("smart_money_alert_wallets", 3)):
                        _add_material(material, token.mint, 1, f"{len(smart)} early wallets each preceded multiple prior 7d winners — the self-built recurring-early cohort is present")

                    holder_metrics = {"holder_count": float(snapshot.holder_count), "top10_pct": snapshot.top10_pct}
                    zscores = ledger.metric_zscores(
                        token.mint, holder_metrics, now,
                        int(intelligence.get("anomaly_trailing_days", 30)),
                        int(intelligence.get("anomaly_min_samples", 7)),
                        record=commit,
                    )
                    for line in anomaly_findings(token, zscores, anomaly_threshold):
                        _add_material(material, token.mint, 4, line)

                    finding = analyze_changes(
                        token, snapshot, ledger, traces, acquisitions, expected, settings, now, ctos.get(token.mint)
                    )
                    if finding:
                        onchain.append(finding)
                except Exception as exc:
                    log.warning("holder_snapshot_failed mint=%s error=%s", token.mint, exc)
                    onchain.append(unavailable_finding(token, str(exc)))
                    snapshot_failures += 1
            statuses.append(SourceStatus(
                "Holder snapshots",
                helius.configured and snapshot_failures == 0,
                f"{len(watched_rows) - snapshot_failures}/{len(watched_rows)} watchlist tokens captured; wallet-history calls {snapshotter.history_calls}",
            ))

        # A cross-token top-holder overlap is stored daily and only reported on a new or material jump.
        snapshot_items = list(current_snapshots.items())
        for index, (mint_a, first) in enumerate(snapshot_items):
            for mint_b, second in snapshot_items[index + 1:]:
                overlap = holder_overlap(first, second)
                previous = ledger.previous_overlap(mint_a, mint_b, now - timedelta(hours=20))
                if overlap >= float(intelligence.get("holder_overlap_alert_pct", 20)) and (previous is None or overlap - previous >= 5):
                    other = token_by_mint.get(mint_b)
                    _add_material(material, mint_a, 5, f"top-holder overlap with ${other.symbol if other else mint_b[:6]} is {overlap:.0f}% — the same holder cohort is rotating between tokens")
                if commit:
                    ledger.record_overlap(mint_a, mint_b, now, overlap)

        candidates, exclusions, journal_pool = screen(
            tokens, safety, enrichments, ctos, ledger, settings, now
        )
        # The ordinary screen starts from the current $200k floor. Re-add every
        # independently proven 24h peak so a coin that touched $1m and returned
        # to $80k still appears in the recap with its current drawdown and risk.
        existing_journal_mints = {candidate.token.mint for candidate in journal_pool}
        for mint, peak in peak_tape.items():
            if mint in existing_journal_mints:
                continue
            token = token_by_mint.get(mint)
            if token is None:
                continue
            candidate = Candidate(
                token=token,
                signals=compute_signals(token, enrichments.get(mint, Enrichment()), ctos.get(mint), now),
                safety=safety.get(mint, SafetyReport(mint)),
                enrichment=enrichments.get(mint, Enrichment()),
                cto=ctos.get(mint),
            )
            candidate.observed_peak_market_cap = peak
            populate_editorial_reasons(candidate)
            journal_pool.append(candidate)
            existing_journal_mints.add(mint)
        scanned_wallets = kol_tracker.scanned if kol_tracker.enabled else 0
        for candidate in journal_pool:
            # The tracked wallets are Solana wallets. Recording zero coverage on
            # every other chain keeps the "nobody touched it" label off coins we
            # never looked for them on.
            candidate.kol_wallets_scanned = (
                scanned_wallets if candidate.token.chain_id.lower() == "solana" else 0
            )
            record = kol_activity.get(candidate.token.mint)
            if record:
                _attach_kol_record(candidate, record, scanned_wallets)
        top_limit = int(settings.get("editorial", "max_shortlist", settings.get("run", "top_tokens", 10)))
        def launched_in_window(candidate) -> bool:
            created = candidate.token.pair_created_at
            return created is not None and window_start <= created <= now

        follow_ups = [candidate for candidate in candidates if "FOLLOW-UP" in candidate.badges][:top_limit]
        fresh = [
            candidate
            for candidate in candidates
            if any(badge in {"NEW", "TODAY"} for badge in candidate.badges) and launched_in_window(candidate)
        ]
        new_and_moving = [candidate for candidate in fresh if is_editorial_pick(candidate, settings)][:top_limit]
        for candidate in new_and_moving:
            candidate.track = "NEW"
        taken = {candidate.token.mint for candidate in new_and_moving}

        # A community takeover is an old token by definition, so this track is
        # evaluated across the whole screened universe rather than the 24h
        # launch window that the fresh track uses.
        cto_candidates: list[Candidate] = []
        if bool(settings.get("cto", "enabled", True)):
            cto_candidates = sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.token.mint not in taken and is_live_cto(candidate, settings, now)
                ),
                key=mover_rank_key,
                reverse=True,
            )[:int(settings.get("cto", "max_ctos", 3))]
            for candidate in cto_candidates:
                candidate.track = "CTO"
            taken |= {candidate.token.mint for candidate in cto_candidates}

        # The strongest names of the day are usually not the ones born today.
        movers: list[Candidate] = []
        if bool(settings.get("movers", "enabled", True)):
            movers = sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.token.mint not in taken and is_mover(candidate, settings)
                ),
                key=mover_rank_key,
                reverse=True,
            )[:int(settings.get("movers", "max_movers", 5))]
            for candidate in movers:
                candidate.track = "MOVER"

        # The journal is the day's record: every coin that ran, ranked, with
        # risk shown on the row instead of being a reason to hide it.
        for candidate in journal_pool:
            record = kol_activity.get(candidate.token.mint)
            if record:
                _attach_kol_record(candidate, record, scanned_wallets)
        existing_pool_mints = {candidate.token.mint for candidate in journal_pool}
        kol_tape_extra = _kol_tape_candidates(
            tokens, safety, enrichments, kol_activity, settings, now, scanned_wallets,
            existing_pool_mints, observed_peaks,
        )
        if kol_tape_extra:
            journal_pool = [*journal_pool, *kol_tape_extra]
        if (
            bool(settings.get("gmgn", "runner_trader_enrichment", True))
            and journal_pool
            and not fixture_path
            and not replay_date
        ):
            # Broad ranking rows sometimes omit wallet-tag counts. Resolve the
            # exact mint before the KOL gate, then spend the heavier trader call
            # only on finalists the fallback proves are relevant.
            statuses.append(await gmgn.enrich_missing_wallet_counts(
                journal_pool,
                gmgn_evidence,
                limit=int(settings.get("gmgn", "wallet_count_fallback_limit", 30)),
            ))
            # The first candle pass only knows the broad discovery counts.
            # Recovered exact mints need their own pass or a coin that ran and
            # faded intraday can still disappear despite its KOL count being
            # corrected above. Cached broad-pass candles are reused here.
            if bool(settings.get("gmgn", "kline_verification_enabled", True)):
                statuses.append(await gmgn.enrich_runner_klines(
                    [candidate.token for candidate in journal_pool],
                    gmgn_evidence,
                    now=now,
                    limit=int(settings.get("gmgn", "recovered_kline_limit", 30)),
                    min_kol_count=int(settings.get("journal", "min_kol_trades_for_publish", 1) or 1),
                    exact_only=True,
                ))
            statuses.append(await gmgn.enrich_runner_traders(
                journal_pool,
                gmgn_evidence,
                limit=int(settings.get("gmgn", "runner_trader_limit", 40)),
                rows_per_token=int(settings.get("gmgn", "runner_trader_rows", 20) or 20),
            ))
        # GMGN and Birdeye describe a coin in daily totals; only Dexscreener
        # reports the six-hour and one-hour windows. The union keeps whichever
        # source claimed the deepest pool, so a GMGN snapshot could win and carry
        # empty intraday buckets into every rule that reads them -- three
        # separate "missing data read as zero" bugs traced back to this. Fill
        # the windows from Dexscreener before any rule runs, once, here.
        sparse = [
            c.token for c in journal_pool
            if c.token.volume_24h > 0 and (not c.token.intraday_known or c.token.txns_6h.total == 0)
        ]
        if sparse:
            try:
                hydrated = {
                    t.mint: t for t in await dex.token_pairs([(t.chain_id, t.mint) for t in sparse])
                }
                filled = 0
                for candidate in journal_pool:
                    fresh = hydrated.get(candidate.token.mint)
                    if fresh is None:
                        continue
                    token = candidate.token
                    _apply_intraday_snapshot(token, fresh, now)
                    if token.pair_created_at is not None:
                        candidate.signals.age_hours = max(
                            0.0, (now - token.pair_created_at).total_seconds() / 3600
                        )
                    filled += 1
                statuses.append(SourceStatus(
                    "Intraday window hydration", True,
                    f"{filled} of {len(sparse)} sparse snapshots given 6h/1h windows from Dexscreener",
                ))
            except Exception as exc:
                log.warning("intraday_hydration_failed error=%s", exc)
                statuses.append(SourceStatus("Intraday window hydration", False, str(exc)[:120]))

        for candidate in journal_pool:
            evidence = dict(gmgn_evidence.get(candidate.token.mint, {}))
            wallet_flow = aggregate_wallet_evidence(
                gmgn_wallet_events, candidate.token.mint, candidate.token.chain_id.lower()
            )
            wallet_flow["coverageAvailable"] = (
                candidate.token.chain_id.lower() in gmgn_discovery.wallet_flow_chains
            )
            evidence["walletFlow"] = wallet_flow
            candidate.provider_evidence["gmgn"] = evidence
            candidate.kol_buyers = sorted(set(candidate.kol_buyers) | set(wallet_flow.get("kolBuyerNames", [])))
            candidate.kol_sellers = sorted(set(candidate.kol_sellers) | set(wallet_flow.get("kolSellerNames", [])))
        attach_lifecycles(journal_pool, ledger, now)
        score_candidates(journal_pool, settings)
        if bool(settings.get("holders", "enabled", True)):
            statuses.append(await _apply_holder_cluster_precheck(
                journal_pool, safety, ledger, helius, settings, now, commit=commit
            ))
        runners, blocked_runners = build_journal(journal_pool, settings, ledger, now)

        # The day's tape: what the market actually spent its money on. These are
        # ranked on 24h volume rather than on the move, because the coin everyone
        # traded is the story even when it only closed up a few percent. Safety
        # still applies -- a rug is never the story we tell.
        tape_size = int(settings.get("journal", "headline_tape_size", 5) or 5)
        tape_seen: set[str] = set()
        tape_pool: list[Candidate] = []
        for candidate in sorted(journal_pool, key=lambda c: c.token.volume_24h, reverse=True):
            if candidate.token.mint in tape_seen:
                continue
            if rug_or_bundle(candidate, settings) or inorganic_reasons(candidate, settings):
                continue
            # The tape is what the market bought, so a coin that dumped all day
            # is not the story, and a feed artifact printing +231,984% is not a
            # market at all.
            if implausible_run(candidate, settings):
                continue
            if candidate.token.price_change_24h < float(
                settings.get("journal", "headline_tape_min_change_pct", 0.0) or 0.0
            ):
                continue
            if candidate.token.volume_24h < float(
                settings.get("journal", "headline_tape_min_volume", 0.0) or 0.0
            ):
                continue
            # A Solana coin no tracked wallet traded does not open the recap
            # either. The tape answers a different question from the runner
            # list, but it answers it under the same rule.
            if kol_touch_required(candidate, settings) and kol_trade_count(candidate) < int(
                settings.get("journal", "min_kol_trades_for_publish", 1) or 1
            ):
                continue
            tape_max_age = float(settings.get("journal", "headline_tape_max_age_hours", 0) or 0)
            candidate_age = candidate.signals.age_hours
            if tape_max_age and candidate_age is not None and candidate_age > tape_max_age:
                continue
            tape_seen.add(candidate.token.mint)
            tape_pool.append(candidate)
            if len(tape_pool) >= tape_size:
                break
        headline_tape = tape_pool
        runners, blocked_runners, kol_promoted = _add_kol_flow_runners(
            runners, blocked_runners, journal_pool, settings, now
        )
        statuses.append(SourceStatus(
            "KOL-flow runner lane",
            True,
            f"{kol_promoted} tracked-wallet coins promoted after Dex/RugCheck/Helius checks",
        ))
        if bool(settings.get("journal", "include_intraday_pulse_runners", True)):
            try:
                pulse_passes = pulse_passes_for_day
                include_all_alerted = bool(
                    settings.get("journal", "include_all_alerted_pulse_runners", False)
                )
                if pulse_passes:
                    missing_pulse_mints = [
                        mint for mint in pulse_passes
                        if mint not in token_by_mint
                    ]
                    if missing_pulse_mints:
                        for token in await dex.token_pairs([
                            (str((pulse_passes[mint] or {}).get("chain") or "solana"), mint)
                            for mint in missing_pulse_mints
                        ]):
                            token_by_mint.setdefault(token.mint, token)
                    current_blocked = {candidate.token.mint: candidate for candidate in blocked_runners}
                    existing = {candidate.token.mint for candidate in runners}
                    recovered: list[Candidate] = []
                    for mint, entry in pulse_passes.items():
                        if mint in existing or not pass_crosses_intraday_floor(entry, settings):
                            continue
                        blocked_now = current_blocked.get(mint)
                        if blocked_now and hard_blocked_pass({"riskLabels": blocked_now.risk_labels}, settings):
                            continue
                        pass_candidate = candidate_from_pass(entry, token_by_mint.get(mint), settings)
                        if not include_all_alerted and kol_touch_required(pass_candidate, settings):
                            record = kol_activity.get(mint)
                            touched = bool(
                                record
                                and (
                                    getattr(record, "buyers", None)
                                    or getattr(record, "sellers", None)
                                    or getattr(record, "holders", None)
                                )
                            )
                            if not touched:
                                continue
                        candidate = pass_candidate
                        record = kol_activity.get(mint)
                        if record:
                            _attach_kol_record(candidate, record, scanned_wallets)
                        candidate.safety = safety.get(mint, candidate.safety)
                        candidate.enrichment = enrichments.get(mint, candidate.enrichment)
                        if not include_all_alerted and not _kol_flow_qualifies(candidate, settings):
                            continue
                        hard_reasons, soft_reasons = _kol_hard_reasons(candidate, settings, now)
                        if hard_reasons:
                            continue
                        if blocked_now:
                            candidate.risk_labels = list(dict.fromkeys([*candidate.risk_labels, *blocked_now.risk_labels]))
                        candidate.risk_labels = list(dict.fromkeys([*candidate.risk_labels, *soft_reasons]))
                        if "intraday runner: qualified during the window, may have faded by report time" not in candidate.risk_labels:
                            candidate.risk_labels.insert(
                                0,
                                "intraday runner: qualified during the window, may have faded by report time",
                            )
                        recovered.append(candidate)
                        existing.add(mint)
                    if recovered:
                        for candidate in recovered:
                            evidence = dict(gmgn_evidence.get(candidate.token.mint, {}))
                            wallet_flow = aggregate_wallet_evidence(
                                gmgn_wallet_events, candidate.token.mint, candidate.token.chain_id.lower()
                            )
                            wallet_flow["coverageAvailable"] = (
                                candidate.token.chain_id.lower() in gmgn_discovery.wallet_flow_chains
                            )
                            evidence["walletFlow"] = wallet_flow
                            candidate.provider_evidence["gmgn"] = evidence
                            candidate.kol_buyers = sorted(set(candidate.kol_buyers) | set(wallet_flow.get("kolBuyerNames", [])))
                            candidate.kol_sellers = sorted(set(candidate.kol_sellers) | set(wallet_flow.get("kolSellerNames", [])))
                        attach_lifecycles(recovered, ledger, now)
                        score_candidates(recovered, settings)
                        blocked_mints = {candidate.token.mint for candidate in recovered}
                        blocked_runners = [
                            candidate for candidate in blocked_runners
                            if candidate.token.mint not in blocked_mints
                        ]
                        runners = [*runners, *recovered]
                        runners.sort(key=journal_rank_key, reverse=True)
                    statuses.append(SourceStatus(
                        "Intraday pulse memory",
                        True,
                        f"{len(recovered)} faded/earlier runners recovered from {len(pulse_passes)} pulse-tracked mint(s)",
                    ))
                else:
                    statuses.append(SourceStatus(
                        "Intraday pulse memory",
                        False,
                        "no pulse passes found in the report window; run hourly pulse to capture peaks",
                    ))
            except Exception as exc:
                log.warning("intraday_pulse_recovery_failed error=%s", exc)
                statuses.append(SourceStatus("Intraday pulse memory", False, str(exc)))
        manually_excluded_mints = {
            str(mint).strip()
            for mint in (settings.get("journal", "excluded_mints", []) or [])
            if str(mint).strip()
        }
        if manually_excluded_mints:
            runners = [
                candidate for candidate in runners
                if candidate.token.mint not in manually_excluded_mints
            ]
            blocked_runners = [
                candidate for candidate in blocked_runners
                if candidate.token.mint not in manually_excluded_mints
            ]
        # The first recap stays selective, while Discord's private browser keeps
        # every measured runner that clears the smaller confirmed-danger gate.
        # Missing metadata is shown as unknown instead of silently deleting a
        # real move from the full-day index.
        universe_by_mint: dict[str, Candidate] = {}
        for candidate in [*runners, *blocked_runners]:
            if not runner_universe_reasons(candidate, settings):
                universe_by_mint.setdefault(candidate.token.mint, candidate)
        runner_universe = sorted(
            universe_by_mint.values(), key=journal_rank_key, reverse=True
        )
        runners = limit_runner_board(runners, settings)
        lore_groups = assign_lore(runners, settings)
        if bool(settings.get("journal", "gate_editorial_tracks", False)):
            runner_mints = {candidate.token.mint for candidate in runners}
            # The older editorial tracks are useful structure, but in production
            # they must not outrank the actual runner gate. If a name fails the
            # journal's rug/bundle/organic/KOL checks, it can stay in diagnostics
            # as blocked evidence, not in the public "picks" rails.
            new_and_moving = [
                candidate for candidate in new_and_moving
                if candidate.token.mint in runner_mints
            ]
            cto_candidates = [
                candidate for candidate in cto_candidates
                if candidate.token.mint in runner_mints
            ]
            movers = [
                candidate for candidate in movers
                if candidate.token.mint in runner_mints
            ]
        min_kol = int(settings.get("kol", "min_buyers_to_flag", 2))
        kol_flagged = sorted(
            (c for c in runners if _kol_touch_count(c) >= min_kol),
            key=_kol_touch_count,
            reverse=True,
        )
        # Where the money was actually made, whether or not the coin ran today.
        symbol_by_mint = {t.mint: t.symbol for t in tokens}
        profit_rows = sorted(
            (
                (mint, record.realised_sol, record.participants)
                for mint, record in kol_activity.items()
                if record.realised_sol != 0
            ),
            key=lambda row: row[1],
            reverse=True,
        )[: int(settings.get("kol", "profit_table_size", 15))]
        # Most of these traded off-universe, so their tickers are unknown here.
        # One batched lookup turns a wall of mint prefixes into readable names.
        unknown = [mint for mint, _, _ in profit_rows if mint not in symbol_by_mint]
        if unknown:
            try:
                for token in await dex.token_pairs(unknown):
                    symbol_by_mint.setdefault(token.mint, token.symbol)
            except Exception as exc:
                log.warning("kol_symbol_lookup_failed error=%s", exc)
        kol_profit_table = [
            (mint, symbol_by_mint.get(mint) or f"{mint[:4]}..{mint[-4:]}", realised, traders)
            for mint, realised, traders in profit_rows
        ]

        # Every featured name is scored, whichever track surfaced it.
        selected = new_and_moving + cto_candidates + movers

        # Turn the quantitative journal into a source-linked desk rundown. X is
        # optional and never blocks delivery. The pair evidence is available on
        # every run; social associations are labelled by match confidence.
        evidence_candidates: list[Candidate] = []
        seen_evidence: set[str] = set()
        peak_context_candidates = [
            candidate for candidate in blocked_runners
            if candidate.runner_tier in {"S", "A", "B"}
        ]
        for candidate in [*runners, *peak_context_candidates, *selected]:
            if candidate.token.mint not in seen_evidence:
                evidence_candidates.append(candidate)
                seen_evidence.add(candidate.token.mint)
            candidate.dex_evidence = build_dex_evidence(candidate)

        x_settings = settings.section("x")
        x_source = XSource(
            http,
            str(urls.get("x_recent_search_url", "https://api.x.com/2/tweets/search/recent")),
            os.getenv("X_BEARER_TOKEN"),
            [str(handle) for handle in x_settings.get("accounts", [])],
            ttl=int(cache.get("x_ttl_seconds", 300)),
            requests_per_minute=int(x_settings.get("requests_per_minute", 60)),
            accounts_per_query=int(x_settings.get("accounts_per_query", 20)),
            max_pages_per_query=int(x_settings.get("max_pages_per_query", 5)),
        )
        if bool(x_settings.get("enabled", True)) and x_source.configured:
            try:
                x_posts = await x_source.posts(window_start)
                match_x_interactions(
                    evidence_candidates,
                    x_posts,
                    max_per_token=int(x_settings.get("max_matches_per_token", 6)),
                )
                matched_posts = len({item.url for c in evidence_candidates for item in c.x_interactions})
                matched_tokens = sum(bool(candidate.x_interactions) for candidate in evidence_candidates)
                statuses.append(SourceStatus(
                    "X monitored accounts",
                    True,
                    f"{len(x_posts)} posts from {len(x_source.accounts)} accounts; "
                    f"{matched_posts} source posts matched {matched_tokens} highlighted tokens",
                ))
            except Exception as exc:
                log.warning("x_monitoring_failed error=%s", exc)
                match_x_interactions(evidence_candidates, [])
                statuses.append(SourceStatus("X monitored accounts", False, str(exc)))
        else:
            match_x_interactions(evidence_candidates, [])
            detail = (
                "X_BEARER_TOKEN is unset; Dexscreener and on-chain evidence remain available"
                if x_source.accounts
                else "no monitored accounts configured"
            )
            statuses.append(SourceStatus("X monitored accounts", False, detail))

        open_intel = OpenIntelSource(
            http,
            str(urls.get("openintel_base_url", "https://ai.6551.io")),
            ttl=int(cache.get("openintel_ttl_seconds", 900)),
            product=str(settings.get("openintel", "twitter_product", "Top")),
            min_followers=int(settings.get("openintel", "min_followers", 500) or 0),
            min_engagement=int(settings.get("openintel", "min_engagement", 3) or 0),
            min_quality=int(settings.get("openintel", "min_post_quality", 45) or 0),
            min_reach=int(settings.get("openintel", "min_account_followers", 10000) or 0),
            min_news_score=int(settings.get("openintel", "min_news_score", 40) or 0),
            pause_seconds=float(settings.get("openintel", "pause_seconds", 2.0) or 0),
        )
        if bool(settings.get("openintel", "enabled", True)):
            _, free_status = await open_intel.free_market_context()
            statuses.append(free_status)
            open_status = await open_intel.enrich(
                evidence_candidates,
                now,
                limit=int(settings.get("openintel", "finalist_limit", 10)),
            )
        else:
            open_status = SourceStatus("OpenNews/OpenTwitter token evidence", False, "disabled in [openintel]")
        statuses.append(open_status)
        if commit:
            ledger.record_provider_health(
                "opennews-opentwitter", open_status.available, now, open_status.detail
            )

        def rundown_rank(candidate: Candidate) -> tuple[float, ...]:
            confidence = {"confirmed": 3.0, "probable": 2.0, "possible": 1.0}
            social = max(
                (confidence.get(item.confidence, 0.0) for item in candidate.x_interactions),
                default=0.0,
            )
            return (
                candidate.scores.get("runner", 0.0),
                social,
                float(len(candidate.x_interactions)),
                float(len(candidate.kol_buyers)),
                candidate.token.volume_24h,
                float(candidate.token.txns_24h.total or candidate.token.txns_6h.total),
                float(candidate.safety.holder_count or 0),
                candidate.signals.turnover,
                candidate.token.price_change_24h,
            )

        runners.sort(key=rundown_rank, reverse=True)
        kol_flagged.sort(key=rundown_rank, reverse=True)

        exclusion_by_mint = {item.token.mint: item for item in exclusions}
        candidate_by_mint = {candidate.token.mint: candidate for candidate in candidates}
        candidate_mints = {candidate.token.mint for candidate in candidates}
        selected_mints = {candidate.token.mint for candidate in selected}
        launches_last_24h: list[LaunchRecord] = []
        for token in tokens:
            created = token.pair_created_at
            if token.chain_id.lower() not in chains or created is None or not (window_start <= created <= now):
                continue
            exclusion = exclusion_by_mint.get(token.mint)
            if exclusion:
                status = "FILTERED"
                reasons = [*exclusion.reasons]
            elif token.mint in selected_mints:
                status = "SHORTLIST"
                candidate = candidate_by_mint[token.mint]
                reasons = [
                    f"editorial cut: {len(candidate.strength_reasons)} strength and {len(candidate.interest_reasons)} interest signals"
                ]
            elif token.mint in candidate_mints:
                status = "CLEARED"
                candidate = candidate_by_mint[token.mint]
                reasons = ["cleared hard filters and safety gate"]
                if candidate.editorial_gaps:
                    reasons.extend(candidate.editorial_gaps)
                else:
                    reasons.append("met editorial bar but fell outside the five-name daily limit")
            else:
                status = "MONITORED"
                reasons = ["cleared hard filters and safety gate; withheld by novelty rules"]
            launches_last_24h.append(LaunchRecord(
                token=token,
                status=status,
                reasons=reasons,
                signals=_launch_signals(token, is_cto=token.mint in ctos),
            ))
        launches_last_24h.sort(
            key=lambda launch: (
                launch.token.pair_created_at.timestamp() if launch.token.pair_created_at else 0,
                launch.token.market_cap,
            ),
            reverse=True,
        )
        cleared_launch_count = sum(launch.status != "FILTERED" for launch in launches_last_24h)
        if collector_started_at:
            complete = collector_started_at <= window_start
            discovery_note = (
                f"The Helius collector stored {len(collector_rows):,} Pump.fun create instructions "
                f"{'across the full window' if complete else 'since it was started; this first window is partial'}. "
                "The tape below shows launches that also resolved to a Dexscreener market."
            )
        else:
            discovery_note = (
                "The on-chain launch collector has not started. This tape is only the Dexscreener discovery sample "
                "and is not an exhaustive Solana launch count."
            )
        strongest_definition = (
            "The highest market cap verifiably reached inside the report window, followed by volume, liquidity, "
            "holders, KOL or smart-money activity, and whether the move held or faded."
        )
        interesting_definition = (
            "A genuinely fresh pair, new profile discovery, measurable CTO activity, linked context, or holder growth; "
            "reused tickers are withheld by a transparent originality proxy."
        )
        chain_labels = {
            "solana": "Solana",
            "base": "Base",
            "bsc": "BNB Chain",
            "ethereum": "Ethereum",
            "robinhood": "Robinhood Chain",
        }
        recap_chains = ", ".join(chain_labels.get(chain, chain.title()) for chain in chains)
        selection_rule = (
            f"Security-cleared recap across {recap_chains}: fresh launches that crossed $250K inside 24h, "
            "plus older coins that made the configured size-adjusted move inside the same trailing window. "
            "Every published contract requires confirmed authorities, locked or burned liquidity, holder count, "
            "top-10 concentration, clean bundle/wash checks, and exact KOL participation."
        )
        statuses.append(SourceStatus(
            "24h market-indexed launches",
            collector_started_at is not None and collector_started_at <= window_start,
            f"{len(launches_last_24h):,} launches resolved to market data; {len(collector_rows):,} raw Pump.fun creates stored",
        ))

        for candidate in selected:
            split = candidate.token.volume_by_dex
            total_volume = sum(split.values())
            if total_volume and len(split) > 1:
                breakdown = ", ".join(
                    f"{dex_name} {dex_volume / total_volume:.0%}"
                    for dex_name, dex_volume in sorted(split.items(), key=lambda item: item[1], reverse=True)
                )
                dex_name, dex_volume = max(split.items(), key=lambda item: item[1])
                dominance = dex_volume / total_volume * 100
                if dominance >= float(intelligence.get("dex_dominance_pct", 80)):
                    candidate.warnings.append(f"24h volume split {breakdown}; reported activity depends on {dex_name}")
                else:
                    candidate.warnings.append(f"24h volume split {breakdown}")

        jupiter = JupiterSource(
            http,
            str(urls.get("jupiter_quote_url", "https://lite-api.jup.ag/swap/v1/quote")),
            str(urls.get("jupiter_price_url", "https://lite-api.jup.ag/price/v3")),
        )
        exit_failures = 0

        async def exit_depth(candidate):
            nonlocal exit_failures
            supply = candidate.enrichment.supply_raw
            if not supply:
                exit_failures += 1
                return
            try:
                candidate.exit_liquidity_sol = await jupiter.sellable_sol_under_impact(
                    candidate.token.mint, supply,
                    max_impact_pct=float(intelligence.get("jupiter_price_impact_pct", 5)),
                    steps=int(intelligence.get("jupiter_binary_search_steps", 14)),
                )
                if candidate.exit_liquidity_sol is None:
                    exit_failures += 1
            except Exception:
                exit_failures += 1

        await asyncio.gather(*(exit_depth(candidate) for candidate in selected))
        statuses.append(SourceStatus(
            "Jupiter exit liquidity", exit_failures == 0,
            f"{len(selected) - exit_failures}/{len(selected)} featured tokens quoted below {float(intelligence.get('jupiter_price_impact_pct', 5)):.0f}% impact",
        ))
        try:
            sol_price = await jupiter.sol_price_usd()
            if commit:
                ledger.record_market_context(now, sol_price)
            statuses.append(SourceStatus("SOL market context", sol_price is not None, "SOL reference price captured" if sol_price else "SOL price unavailable"))
        except Exception:
            statuses.append(SourceStatus("SOL market context", False, "SOL price unavailable"))

        due = ledger.due_observations(now)
        due_mints = list(dict.fromkeys(row["mint"] for row in due))
        current_mcaps = {token.mint: token.market_cap for token in tokens if token.market_cap > 0}
        missing_due = [mint for mint in due_mints if mint not in current_mcaps]
        if missing_due:
            try:
                # Old observations predate chain-aware rows. Never send an EVM
                # address to the Solana route: probe only configured EVM chains
                # for 0x addresses and Solana for base58 mints.
                due_lookups: list[tuple[str, str]] = []
                evm_chains = [chain for chain in chains if chain != "solana"]
                for mint in missing_due:
                    if mint.lower().startswith("0x"):
                        due_lookups.extend((chain, mint) for chain in evm_chains)
                    else:
                        due_lookups.append(("solana", mint))
                for token in await dex.token_pairs(due_lookups):
                    current_mcaps[token.mint] = max(current_mcaps.get(token.mint, 0), token.market_cap)
            except Exception as exc:
                statuses.append(SourceStatus("Forward-return lookup", False, str(exc)))
        if commit:
            ledger.record_forward_returns(due, current_mcaps, now)
            for rank, candidate in enumerate(selected, start=1):
                ledger.record_feature(candidate.token.mint, candidate.token.symbol, candidate.token.market_cap, rank, now)
            for exclusion in exclusions:
                if exclusion.stage == "safety gate":
                    ledger.record_exclusion(exclusion.token.mint, exclusion.token.symbol, exclusion.token.market_cap, now)
            quality_alert_list = data_quality_alerts(ledger, tokens, now.date().isoformat())
        weekly_notes = ledger.weekly_retrospective(now)
        for mint in current_snapshots:
            correlation = ledger.sol_correlation(mint, now)
            if correlation is not None:
                token = token_by_mint.get(mint)
                weekly_notes.append(
                    f"30d daily-return correlation for ${token.symbol if token else mint[:6]} versus SOL is {correlation:+.2f} — "
                    + ("moves have mostly tracked market beta" if abs(correlation) >= .65 else "moves have been comparatively idiosyncratic")
                )
        onchain = _merge_material(onchain, material, token_by_mint)

        recap_candidates: list[Candidate] = []
        recap_seen: set[str] = set()
        for candidate in runners:
            candidate.provider_evidence["editorial"] = {"published": True}
        for candidate in blocked_runners:
            candidate.provider_evidence["editorial"] = {"published": False}
        for candidate in [*runners, *blocked_runners]:
            if candidate.token.mint in recap_seen:
                continue
            recap_seen.add(candidate.token.mint)
            recap_candidates.append(candidate)
        recap = build_structured_recap(recap_candidates, now)
        if commit:
            ledger.save_daily_recap(now.date().isoformat(), now, window_start, recap)

        # The written recap is the last thing built, so it sees the final
        # runner set. It never blocks delivery: if the model is off, slow or
        # wrong, `narrative` stays empty and the template renders as before.
        narrative: dict = {}
        try:
            recap_pool = recap_coins(
                runners, headline_tape,
                newsletter_coin_limit(settings),
            )
            # A long run means the numbers gathered at the start are stale by
            # the time the recap is written: one coin was published 178% away
            # from its live market cap. Refresh the coins that will be named.
            if recap_pool and bool(settings.get("newsletter", "refresh_before_write", True)):
                try:
                    fresh = await dex.token_pairs([(c.token.chain_id, c.token.mint) for c in recap_pool])
                    latest = {token.mint: token for token in fresh}
                    refreshed = 0
                    for candidate in recap_pool:
                        current = latest.get(candidate.token.mint)
                        if current is None or current.market_cap <= 0:
                            continue
                        # The peak is a high-water mark and only ever rises.
                        candidate.peak_market_cap = max(
                            float(candidate.peak_market_cap or 0), current.market_cap
                        )
                        candidate.token.market_cap = current.market_cap
                        candidate.token.volume_24h = current.volume_24h
                        candidate.token.liquidity_usd = current.liquidity_usd
                        candidate.token.price_change_24h = current.price_change_24h
                        candidate.token.price_change_1h = current.price_change_1h
                        single = (candidate.provider_evidence.get("lifecycle", {}) or {}).get("peakIsSingleObservation")
                        if not single and candidate.peak_market_cap:
                            candidate.drawdown_from_peak_pct = max(
                                0.0, (candidate.peak_market_cap - current.market_cap) / candidate.peak_market_cap * 100.0
                            )
                        refreshed += 1
                    if refreshed:
                        statuses.append(SourceStatus(
                            "Recap price refresh", True,
                            f"{refreshed} of {len(recap_pool)} coins repriced immediately before writing",
                        ))
                except Exception as exc:
                    log.warning("recap_refresh_failed error=%s", exc)

            # Free lore first: it needs no key and no credits, so it is the
            # layer that always runs. Paid web research adds to it when funded.
            storied = await attach_lore(recap_pool, settings)
            if storied:
                statuses.append(SourceStatus(
                    "Coin lore", True,
                    f"{storied} of {len(recap_pool)} coins with a sourced story from free search",
                ))
            researched = await research_day(recap_pool, settings)
            if researched:
                statuses.append(SourceStatus(
                    "Coin research",
                    True,
                    f"{researched} coins with a searched, cited story behind the move",
                ))
            # Accounts the operator trusts, read directly. Matched against the
            # coins that will actually be published: run earlier it matched
            # against the whole candidate pool, so 56 posts landed on coins
            # nobody was going to read about and none reached the page.
            trusted = [
                str(handle).strip().lstrip("@")
                for handle in (settings.get("openintel", "trusted_accounts", []) or [])
                if str(handle).strip()
            ]
            per_run = int(settings.get("openintel", "accounts_per_run", 0) or 0)
            if per_run and len(trusted) > per_run:
                # Deterministic slice keyed on the day, so every account is read
                # regularly without any one run paying for the whole list.
                offset = (now.timetuple().tm_yday * per_run) % len(trusted)
                trusted = (trusted + trusted)[offset:offset + per_run]
            if trusted and bool(settings.get("openintel", "enabled", True)):
                try:
                    hits, misses = await open_intel.trusted_timeline(
                        recap_pool,
                        trusted,
                        now,
                        max_per_account=int(settings.get("openintel", "max_posts_per_account", 30) or 30),
                        window_hours=float(settings.get("openintel", "trusted_window_hours", 36) or 36),
                    )
                    statuses.append(SourceStatus(
                        "Trusted accounts",
                        not misses,
                        f"{hits} posts from {len(trusted) - len(misses)}/{len(trusted)} accounts matched today's coins"
                        + (
                        "; unreachable: " + ", ".join(
                            m.split(":")[0] for m in misses[:12]
                        ) + (" and more" if len(misses) > 12 else "")
                        if misses else ""
                    ),
                    ))
                except Exception as exc:
                    log.warning("trusted_timeline_failed error=%s", exc)
                    statuses.append(SourceStatus("Trusted accounts", False, str(exc)[:120]))

            explained = await explain_runs(recap_pool, settings)
            if explained:
                statuses.append(SourceStatus(
                    "Cause of the run", True,
                    f"{explained} of {len(recap_pool)} coins with a stated cause from their own evidence",
                ))
            written = await write_recap(recap_pool, now, settings)
            if written:
                narrative = written
                statuses.append(SourceStatus(
                    "Written recap",
                    True,
                    f"{len(written.get('sections', []))} sections written by "
                    f"{written.get('writer', 'unknown')}",
                ))
        except Exception as exc:  # never let the writer stop the report
            log.warning("newsletter_write_failed error=%s", exc)
            statuses.append(SourceStatus("Written recap", False, str(exc)[:120]))

        return Brief(
            generated_at=now,
            scorecard=ledger.scorecard(now),
            metas=sorted(metas, key=lambda meta: meta.change_24h, reverse=True)[:8],
            new_and_moving=new_and_moving,
            ctos=cto_candidates,
            follow_ups=follow_ups,
            movers=movers,
            runners=runners,
            runner_universe=runner_universe,
            blocked_runners=blocked_runners,
            headline_tape=headline_tape,
            narrative=narrative,
            lore_groups=lore_groups,
            kol_flagged=kol_flagged,
            kol_wallet_count=(
                kol_tracker.scanned
                + len({str(event.get("wallet")) for event in gmgn_wallet_events if event.get("wallet")})
            ),
            kol_profit_table=kol_profit_table,
            onchain=sorted(onchain, key=lambda finding: (finding.priority, finding.symbol)),
            excluded=exclusions,
            source_statuses=statuses,
            quality_alerts=quality_alert_list,
            weekly_notes=weekly_notes,
            window_start=window_start,
            launches_last_24h=launches_last_24h,
            discovered_solana_count=sum(token.chain_id.lower() in chains for token in tokens),
            cleared_launch_count=cleared_launch_count,
            discovery_note=discovery_note,
            strongest_definition=strongest_definition,
            interesting_definition=interesting_definition,
            selection_rule=selection_rule,
            raw_launch_count=len(collector_rows),
            indexed_launch_count=len(indexed_launch_mints),
            collector_started_at=collector_started_at,
            recap=recap,
        )
    finally:
        await http.close()
