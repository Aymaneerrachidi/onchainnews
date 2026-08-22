from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from brief.config import Settings
from brief.ledger import Ledger, iso
from brief.models import (
    AcquisitionTrace,
    CTORecord,
    HolderBalance,
    HolderSnapshot,
    OnChainFinding,
    SafetyReport,
    TokenSnapshot,
    WalletTrace,
)
from brief.sources.helius import HeliusSource


UTC = timezone.utc
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


def gini(values: Iterable[float]) -> float:
    ordered = sorted(value for value in values if value > 0)
    count = len(ordered)
    total = sum(ordered)
    if not count or not total:
        return 0.0
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    return (2 * weighted) / (count * total) - (count + 1) / count


def concentration(balances: list[HolderBalance], count: int) -> float:
    total = sum(item.amount for item in balances)
    return sum(item.amount for item in balances[:count]) / total * 100 if total else 0.0


def build_snapshot(mint: str, now: datetime, balances: list[HolderBalance], excluded_count: int = 0) -> HolderSnapshot:
    total = sum(item.amount for item in balances)
    return HolderSnapshot(
        mint=mint,
        taken_at=now,
        holder_count=len(balances),
        top10_pct=concentration(balances, 10),
        top50_pct=concentration(balances, 50),
        gini=gini(item.amount for item in balances),
        total_amount=total,
        balances=balances,
        excluded_accounts=excluded_count,
    )


def _pct_change(current: float, previous: float) -> float | None:
    return (current / previous - 1) * 100 if previous else None


def _short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}" if len(address) > 12 else address


class _UnionFind:
    def __init__(self, owners: Iterable[str]) -> None:
        self.parent = {owner: owner for owner in owners}

    def find(self, owner: str) -> str:
        while self.parent[owner] != owner:
            self.parent[owner] = self.parent[self.parent[owner]]
            owner = self.parent[owner]
        return owner

    def union(self, first: str, second: str) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[b] = a


@dataclass(slots=True)
class FundingCluster:
    funder: str
    wallets: list[str]
    supply_pct: float
    funding_window_minutes: float | None
    first_funded_at: datetime | None


@dataclass(slots=True)
class ClusterSummary:
    effective_top10_pct: float | None
    cluster_count: int
    largest_members: int
    largest_pct: float
    largest_funder: str | None
    largest_window_minutes: float | None
    largest_funded_at: datetime | None
    coverage: int
    groups: list[FundingCluster]


def collapse_clusters(
    balances: list[HolderBalance],
    traces: dict[str, WalletTrace],
    top_n: int,
    excluded_funders: set[str] | None = None,
) -> ClusterSummary:
    excluded_funders = excluded_funders or set()
    selected = balances[:top_n]
    owners = [item.owner for item in selected]
    union = _UnionFind(owners)
    by_funder: dict[str, list[str]] = {}
    for owner in owners:
        trace = traces.get(owner)
        if trace and trace.first_funder and trace.first_funder not in excluded_funders:
            by_funder.setdefault(trace.first_funder, []).append(owner)
    for funded in by_funder.values():
        for owner in funded[1:]:
            union.union(funded[0], owner)
    groups: dict[str, list[str]] = {}
    for owner in owners:
        groups.setdefault(union.find(owner), []).append(owner)
    multi = [members for members in groups.values() if len(members) > 1]
    total = sum(item.amount for item in balances)
    if not total or not traces:
        return ClusterSummary(None, 0, 0, 0.0, None, None, None, len(traces), [])
    root_by_owner = {owner: union.find(owner) for owner in owners}
    entity_amounts: dict[str, float] = {}
    for balance in balances:
        entity = root_by_owner.get(balance.owner, balance.owner)
        entity_amounts[entity] = entity_amounts.get(entity, 0.0) + balance.amount
    effective = sum(sorted(entity_amounts.values(), reverse=True)[:10]) / total * 100
    if not multi:
        return ClusterSummary(effective, 0, 0, 0.0, None, None, None, len(traces), [])
    amounts = {item.owner: item.amount for item in balances}
    largest = max(multi, key=lambda members: sum(amounts.get(owner, 0) for owner in members))
    largest_pct = sum(amounts.get(owner, 0) for owner in largest) / total * 100
    funder = traces[largest[0]].first_funder
    times = [traces[owner].first_funded_at for owner in largest if traces[owner].first_funded_at]
    window = (max(times) - min(times)).total_seconds() / 60 if len(times) >= 2 else None
    funding_groups: list[FundingCluster] = []
    for members in multi:
        group_funder = traces[members[0]].first_funder
        group_times = [traces[owner].first_funded_at for owner in members if traces[owner].first_funded_at]
        funding_groups.append(FundingCluster(
            funder=group_funder or "unknown",
            wallets=list(members),
            supply_pct=sum(amounts.get(owner, 0) for owner in members) / total * 100,
            funding_window_minutes=(max(group_times) - min(group_times)).total_seconds() / 60 if len(group_times) >= 2 else None,
            first_funded_at=min(group_times) if group_times else None,
        ))
    return ClusterSummary(
        effective, len(multi), len(largest), largest_pct, funder, window,
        min(times) if times else None, len(traces), funding_groups,
    )


class HolderSnapshotter:
    def __init__(self, ledger: Ledger, helius: HeliusSource, settings: Settings) -> None:
        self.ledger = ledger
        self.helius = helius
        self.settings = settings
        self.values = settings.section("holders")
        self.history_budget = int(self.values.get("max_wallet_history_calls_per_run", 1000))
        self.history_calls = 0
        self.semaphore = asyncio.Semaphore(int(self.values.get("history_concurrency", 4)))

    def exclusions(self, safety: SafetyReport) -> tuple[set[str], set[str]]:
        accounts = set(safety.excluded_accounts) | BURN_ADDRESSES
        owners = set(safety.excluded_owners) | BURN_ADDRESSES
        accounts.update(str(value) for value in self.values.get("excluded_accounts", []))
        owners.update(str(value) for value in self.values.get("excluded_owners", []))
        owners.update(str(value) for value in self.values.get("known_cex_wallets", []))
        return accounts, owners

    async def pull(self, token: TokenSnapshot, safety: SafetyReport, now: datetime, *, commit: bool) -> HolderSnapshot:
        accounts, owners = self.exclusions(safety)
        balances, excluded_count = await self.helius.token_holders(
            token.mint,
            excluded_accounts=accounts,
            excluded_owners=owners,
            ttl=int(self.values.get("snapshot_cache_seconds", 60)),
        )
        snapshot = build_snapshot(token.mint, now, balances, excluded_count)
        if commit:
            self.ledger.record_holder_snapshot(
                snapshot,
                price_usd=token.price_usd,
                market_cap=token.market_cap,
                pair_created_at=token.pair_created_at,
            )
        return snapshot

    async def _wallet_trace(self, owner: str, now: datetime) -> WalletTrace | None:
        cached = self.ledger.wallet_trace(owner, int(self.values.get("wallet_trace_cache_days", 30)), now)
        if cached:
            return cached
        if self.history_calls >= self.history_budget:
            return None
        self.history_calls += 1
        async with self.semaphore:
            trace = await self.helius.trace_wallet(
                owner, ttl=int(self.values.get("wallet_trace_cache_days", 30)) * 86400
            )
        self.ledger.save_wallet_trace(trace, now)
        return trace

    async def _acquisition(self, mint: str, owner: str, now: datetime) -> AcquisitionTrace | None:
        cached = self.ledger.acquisition_trace(mint, owner)
        if cached:
            return cached
        if self.history_calls >= self.history_budget:
            return None
        self.history_calls += 1
        async with self.semaphore:
            trace = await self.helius.trace_acquisition(
                mint, owner, ttl=int(self.values.get("acquisition_cache_days", 90)) * 86400
            )
        self.ledger.save_acquisition_trace(trace, now)
        return trace

    async def trace_wallet(self, owner: str, now: datetime) -> WalletTrace | None:
        try:
            return await self._wallet_trace(owner, now)
        except Exception:
            return None

    async def trace_top_wallets(
        self, snapshot: HolderSnapshot, now: datetime, *, top_n: int | None = None
    ) -> tuple[dict[str, WalletTrace], dict[str, AcquisitionTrace], int]:
        limit = int(top_n or self.values.get("cluster_top_holders", 100))
        owners = [balance.owner for balance in snapshot.balances[:limit]]

        async def trace(owner: str):
            try:
                wallet, acquisition = await asyncio.gather(
                    self._wallet_trace(owner, now), self._acquisition(snapshot.mint, owner, now)
                )
                return owner, wallet, acquisition
            except Exception:
                return owner, None, None

        results = await asyncio.gather(*(trace(owner) for owner in owners))
        wallets = {owner: wallet for owner, wallet, _ in results if wallet is not None}
        acquisitions = {owner: acquisition for owner, _, acquisition in results if acquisition is not None}
        return wallets, acquisitions, len(owners)


def analyze_changes(
    token: TokenSnapshot,
    current: HolderSnapshot,
    ledger: Ledger,
    traces: dict[str, WalletTrace],
    acquisitions: dict[str, AcquisitionTrace],
    expected_trace_count: int,
    settings: Settings,
    now: datetime,
    cto: CTORecord | None = None,
) -> OnChainFinding | None:
    values = settings.section("holders")
    current_stamp = iso(current.taken_at)
    previous = ledger.snapshot_at_or_before(
        token.mint,
        now - timedelta(hours=float(values.get("daily_snapshot_min_age_hours", 20))),
        exclude_taken_at=current_stamp,
    )
    week = ledger.snapshot_at_or_before(
        token.mint,
        now - timedelta(hours=float(values.get("weekly_snapshot_min_age_hours", 164))),
        exclude_taken_at=current_stamp,
    )
    cluster = collapse_clusters(
        current.balances,
        traces,
        int(values.get("cluster_top_holders", 100)),
        set(str(value) for value in values.get("known_cex_wallets", [])),
    )
    trace_note = None
    if expected_trace_count and cluster.coverage < expected_trace_count:
        trace_note = f"wallet provenance partial ({cluster.coverage}/{expected_trace_count} top holders traced)"

    if previous is None:
        details: list[str] = []
        if cluster.effective_top10_pct is not None and cluster.effective_top10_pct - current.top10_pct >= float(values.get("cluster_alert_pp", 5)):
            details.append(
                f"effective top10 {cluster.effective_top10_pct:.1f}% across {cluster.cluster_count} shared-funder clusters (nominal {current.top10_pct:.1f}%) — nominal concentration understates common provenance"
            )
        if token.pair_created_at:
            sniper_window = timedelta(minutes=float(values.get("sniper_window_minutes", 10)))
            baseline_snipers = [
                (owner, trace) for owner, trace in acquisitions.items()
                if trace.first_acquired_at and token.pair_created_at <= trace.first_acquired_at <= token.pair_created_at + sniper_window
            ]
            if baseline_snipers:
                current_amounts = {item.owner: item.amount for item in current.balances}
                sniper_amount = sum(current_amounts.get(owner, 0) for owner, _ in baseline_snipers)
                initial_amount = sum(trace.initial_amount or 0 for _, trace in baseline_snipers)
                retained = sniper_amount / initial_amount * 100 if initial_amount else None
                retained_text = f"; current balance is {retained:.0f}% of original acquisition" if retained is not None else ""
                details.append(
                    f"{len(baseline_snipers)} traced top holders acquired in the first {int(sniper_window.total_seconds() / 60)}m and hold {sniper_amount / current.total_amount * 100:.1f}%{retained_text} — launch-cohort exposure remains visible"
                )
                origins: dict[str, int] = {}
                excluded_funders = set(str(value) for value in values.get("known_cex_wallets", []))
                for owner, _ in baseline_snipers:
                    funder = traces.get(owner).first_funder if traces.get(owner) else None
                    if funder and funder not in excluded_funders:
                        origins[funder] = origins.get(funder, 0) + 1
                bundled = sum(count for count in origins.values() if count > 1)
                if bundled:
                    details.append(f"{bundled} launch-window wallets share a funding origin — probable bundled launch cohort")
        if trace_note:
            details.append(trace_note)
        if not details or details == [trace_note]:
            return None
        details.append(
            f"baseline captured: {current.holder_count:,} holders; top10 {current.top10_pct:.1f}%; top50 {current.top50_pct:.1f}%; Gini {current.gini:.3f}"
        )
        return OnChainFinding(token.mint, token.symbol, 3, details[0], details[1:])

    previous_balances = ledger.balances_for(token.mint, previous["taken_at"])
    current_balances = {item.owner: item.amount for item in current.balances}
    holder_delta = current.holder_count - previous["holder_count"]
    holder_rate = _pct_change(current.holder_count, previous["holder_count"])
    price_rate = _pct_change(token.price_usd, previous["price_usd"] or 0)
    top10_delta = current.top10_pct - previous["top10_pct"]
    top50_delta = current.top50_pct - previous["top50_pct"]
    gini_delta = current.gini - previous["gini"]
    retention = sum(owner in current_balances for owner in previous_balances) / len(previous_balances) * 100 if previous_balances else None
    new_owners = set(current_balances) - set(previous_balances)
    fresh_cutoff = now.astimezone(UTC) - timedelta(hours=float(values.get("fresh_wallet_hours", 24)))
    traced_new = [traces[owner] for owner in new_owners if owner in traces and traces[owner].wallet_created_at]
    fresh_new = sum(trace.wallet_created_at >= fresh_cutoff for trace in traced_new)
    fresh_rate = fresh_new / len(traced_new) * 100 if traced_new else None

    week_rate = _pct_change(current.holder_count, week["holder_count"]) if week else None
    acceleration = holder_rate - week_rate / 7 if holder_rate is not None and week_rate is not None else None
    significant_holder = abs(holder_rate or 0) >= float(values.get("min_holder_change_pct", 2))
    flat_price = abs(price_rate or 0) <= float(values.get("flat_price_pct", 5))
    rising_price = (price_rate or 0) >= float(values.get("rising_price_pct", 15))

    priority = 7
    details: list[str] = []
    if holder_rate is not None and holder_rate < -float(values.get("min_holder_change_pct", 2)) and rising_price:
        headline = f"holders {previous['holder_count']:,} → {current.holder_count:,} ({holder_rate:+.1f}%) while market price moved {price_rate:+.1f}%"
        details.append("distribution into strength")
        priority = 0
    elif holder_rate is not None and holder_rate > float(values.get("min_holder_change_pct", 2)) and flat_price:
        headline = f"holders {previous['holder_count']:,} → {current.holder_count:,} ({holder_rate:+.1f}%) while market price moved {price_rate:+.1f}%"
        details.append("holder accumulation outpaced market price")
        priority = 1
    elif significant_holder:
        headline = f"holders {previous['holder_count']:,} → {current.holder_count:,} ({holder_rate:+.1f}%) in 24h"
        if acceleration is not None:
            details.append("holder growth is accelerating" if acceleration > 0 else "holder growth is rolling over")
        priority = 4
    else:
        headline = ""

    total = current.total_amount or 1
    whale_threshold = float(values.get("min_whale_delta_supply_pct", 0.5))
    ranked_owners = [owner for owner, _ in sorted(previous_balances.items(), key=lambda item: item[1], reverse=True)[:20]]
    ranked_owners += [item.owner for item in current.balances[:20] if item.owner not in ranked_owners]
    whale_changes: list[tuple[float, str]] = []
    for owner in ranked_owners:
        before, after = previous_balances.get(owner, 0.0), current_balances.get(owner, 0.0)
        supply_delta = (after - before) / total * 100
        if abs(supply_delta) < whale_threshold:
            continue
        if before and not after:
            text = f"top wallet {_short(owner)} left entirely ({before / total * 100:.1f}% of current ex-pool supply) — large-holder exit"
        elif after > before:
            text = f"top wallet {_short(owner)} added {supply_delta:.1f}% of ex-pool supply — large-holder accumulation"
        else:
            cut = (before - after) / before * 100 if before else 0
            text = f"top wallet {_short(owner)} cut {cut:.0f}% of its position — distribution by a large holder"
        whale_changes.append((abs(supply_delta), text))
    if whale_changes:
        whale_changes.sort(reverse=True)
        details.extend(text for _, text in whale_changes[:2])
        priority = min(priority, 2)

    concentration_changed = max(abs(top10_delta), abs(top50_delta)) >= float(values.get("min_concentration_change_pp", 1))
    if concentration_changed:
        direction = "fell" if top10_delta < 0 else "rose"
        implication = "ownership broadened" if top10_delta < 0 else "ownership consolidated"
        details.append(
            f"top10 concentration {direction} {abs(top10_delta):.1f}pp to {current.top10_pct:.1f}%; top50 {top50_delta:+.1f}pp; Gini {gini_delta:+.3f} — {implication}"
        )
        priority = min(priority, 3)
    previous_cluster = ledger.cluster_at_or_before(
        token.mint,
        now - timedelta(hours=float(values.get("daily_snapshot_min_age_hours", 20))),
        exclude_taken_at=current_stamp,
    )
    cluster_changed = bool(
        previous_cluster
        and cluster.effective_top10_pct is not None
        and previous_cluster["effective_top10_pct"] is not None
        and (
            abs(cluster.effective_top10_pct - previous_cluster["effective_top10_pct"]) >= float(values.get("min_concentration_change_pp", 1))
            or cluster.cluster_count != previous_cluster["cluster_count"]
        )
    )
    cluster_new = previous_cluster is None and cluster.cluster_count > 0
    if (cluster_changed or cluster_new) and cluster.effective_top10_pct is not None and cluster.effective_top10_pct - current.top10_pct >= float(values.get("cluster_alert_pp", 5)):
        details.append(
            f"effective top10 {cluster.effective_top10_pct:.1f}% across {cluster.cluster_count} shared-funder clusters (nominal {current.top10_pct:.1f}%) — nominal concentration understates common provenance"
        )
        if cluster.largest_funder:
            timing = (
                f" within {cluster.largest_window_minutes:.0f}m"
                if cluster.largest_window_minutes is not None
                and cluster.largest_window_minutes <= float(values.get("funding_cluster_window_minutes", 15))
                else ""
            )
            launch_timing = ""
            if cluster.largest_funded_at and token.pair_created_at:
                offset_hours = (token.pair_created_at - cluster.largest_funded_at).total_seconds() / 3600
                if offset_hours >= 0:
                    launch_timing = f", first funded {offset_hours:.1f}h before launch"
                else:
                    launch_timing = f", first funded {abs(offset_hours):.1f}h after launch"
            details.append(
                f"largest cluster holds {cluster.largest_pct:.1f}% across {cluster.largest_members} wallets, funded by {_short(cluster.largest_funder)}{timing}{launch_timing} — coordinated provenance"
            )
        priority = min(priority, 3)

    sniper_window = timedelta(minutes=float(values.get("sniper_window_minutes", 10)))
    if token.pair_created_at:
        snipers = [
            (owner, trace) for owner, trace in acquisitions.items()
            if trace.first_acquired_at and token.pair_created_at <= trace.first_acquired_at <= token.pair_created_at + sniper_window
        ]
        if snipers:
            current_sniper = sum(current_balances.get(owner, 0) for owner, _ in snipers)
            previous_total = previous["total_amount"] or 1
            previous_sniper = sum(previous_balances.get(owner, 0) for owner, _ in snipers)
            share_change = current_sniper / total * 100 - previous_sniper / previous_total * 100
            initial = sum(trace.initial_amount or 0 for _, trace in snipers)
            retained = current_sniper / initial * 100 if initial else None
            if abs(share_change) >= float(values.get("min_concentration_change_pp", 1)):
                text = f"launch-window holders changed exposure {share_change:+.1f}pp and now hold {current_sniper / total * 100:.1f}%"
                if retained is not None:
                    text += f" (current balance is {retained:.0f}% of initial acquisition)"
                text += " — launch-cohort exposure changed"
                details.append(text)
                origins: dict[str, int] = {}
                excluded_funders = set(str(value) for value in values.get("known_cex_wallets", []))
                for owner, _ in snipers:
                    funder = traces.get(owner).first_funder if traces.get(owner) else None
                    if funder and funder not in excluded_funders:
                        origins[funder] = origins.get(funder, 0) + 1
                bundled = sum(count for count in origins.values() if count > 1)
                if bundled:
                    details.append(f"{bundled} launch-window wallets share a funding origin — probable bundled launch cohort")

    if retention is not None and abs(retention - 100) >= float(values.get("min_retention_change_pct", 2)):
        details.append(f"{retention:.0f}% of prior-day holders still hold a balance — {('holder base stayed sticky' if retention >= 85 else 'holder base churned')}")
    if fresh_rate is not None and fresh_rate >= float(values.get("fresh_wallet_alert_pct", 50)):
        details.append(f"{fresh_rate:.0f}% of {len(traced_new)} traced new top holders use wallets created within 24h — apparent growth is dominated by fresh wallets")
    if cto and cto.claim_date and (holder_delta or concentration_changed):
        claim_snapshot = ledger.snapshot_at_or_before(token.mint, cto.claim_date)
        if claim_snapshot:
            since_claim = current.holder_count - claim_snapshot["holder_count"]
            details.append(f"CTO holders {since_claim:+,} since claim; concentration {current.top10_pct - claim_snapshot['top10_pct']:+.1f}pp")
    if week_rate is not None and abs(week_rate) >= float(values.get("min_holder_change_pct", 2)):
        week_delta = current.holder_count - week["holder_count"]
        acceleration_text = f"; acceleration {acceleration:+.1f}pp/day" if acceleration is not None else ""
        details.append(f"7d holders {week['holder_count']:,} → {current.holder_count:,} ({week_delta:+,}, {week_rate:+.1f}%){acceleration_text}")
    if trace_note and (headline or details):
        details.append(trace_note)

    if not headline and not details:
        return None
    if not headline:
        headline = details.pop(0)
    return OnChainFinding(token.mint, token.symbol, priority, headline, details)


def unavailable_finding(token: TokenSnapshot, detail: str) -> OnChainFinding:
    return OnChainFinding(
        token.mint,
        token.symbol,
        9,
        "holder snapshot unavailable",
        [detail],
        status="unavailable",
    )
