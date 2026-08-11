from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from brief.config import Settings
from brief.ledger import Ledger
from brief.models import HolderSnapshot, OnChainFinding, TokenSnapshot, WalletTrace


UTC = timezone.utc
LAUNCHPAD_DEXES = {"pumpfun", "pump.fun", "moonshot", "bonkfun", "launchlab"}
MIGRATED_DEXES = {"raydium", "meteora", "orca"}


def pool_liquidity_proxy(vault_balances: dict[str, float]) -> float | None:
    positive = [value for value in vault_balances.values() if value > 0]
    if len(positive) < 2:
        return None
    return math.exp(sum(math.log(value) for value in positive) / len(positive))


def lp_removal_pct(previous_proxy: float | None, current_proxy: float | None) -> float | None:
    if not previous_proxy or current_proxy is None or current_proxy >= previous_proxy:
        return None
    return (1 - current_proxy / previous_proxy) * 100


def detect_lp_removal(previous_proxy: float | None, current_proxy: float | None, threshold_pct: float) -> float | None:
    removal = lp_removal_pct(previous_proxy, current_proxy)
    return removal if removal is not None and removal >= threshold_pct else None


def migration_detail(previous_dex: str | None, token: TokenSnapshot) -> str | None:
    current = token.dex_id.lower()
    previous = (previous_dex or "").lower()
    if previous in LAUNCHPAD_DEXES and current in MIGRATED_DEXES:
        when = token.pair_created_at.strftime("%d %b %H:%M UTC") if token.pair_created_at else "time unavailable"
        return f"migrated {previous} → {current} at {when} — liquidity venue and trading regime changed"
    return None


def anomaly_findings(token: TokenSnapshot, zscores: dict[str, float], threshold: float) -> list[str]:
    labels = {
        "turnover": "turnover",
        "volume_24h": "24h volume",
        "liquidity_usd": "liquidity",
        "buy_imbalance_6h": "6h buy share",
        "holder_count": "holder count",
        "top10_pct": "top10 concentration",
    }
    findings = []
    for metric, score in sorted(zscores.items(), key=lambda item: abs(item[1]), reverse=True):
        if abs(score) < threshold:
            continue
        direction = "above" if score > 0 else "below"
        findings.append(f"{labels.get(metric, metric)} is {abs(score):.1f}σ {direction} its own trailing baseline — behavior is unusual for this token")
    return findings


def creator_outflow_finding(history: list[Any], min_days: int, min_outflow_pp: float) -> str | None:
    if len(history) < min_days + 1:
        return None
    ordered = list(reversed(history[:min_days + 1]))
    changes = [ordered[index + 1]["supply_pct"] - ordered[index]["supply_pct"] for index in range(len(ordered) - 1)]
    cumulative = ordered[-1]["supply_pct"] - ordered[0]["supply_pct"]
    if all(change < 0 for change in changes) and cumulative <= -min_outflow_pp:
        return f"creator-linked supply fell every snapshot for {len(changes)} intervals ({cumulative:.2f}pp total) — sustained creator-linked distribution"
    return None


def cex_provenance_finding(
    token: TokenSnapshot,
    traces: dict[str, WalletTrace],
    known_cex: set[str],
    short_window_minutes: float,
) -> str | None:
    if not token.pair_created_at or not known_cex:
        return None
    by_funder: dict[str, list[WalletTrace]] = {}
    for trace in traces.values():
        if trace.first_funder in known_cex and trace.first_funded_at:
            by_funder.setdefault(trace.first_funder, []).append(trace)
    for funder, group in by_funder.items():
        if len(group) < 3:
            continue
        times = [trace.first_funded_at for trace in group if trace.first_funded_at]
        window = (max(times) - min(times)).total_seconds() / 60
        before_launch = (token.pair_created_at - max(times)).total_seconds() / 60
        if window <= short_window_minutes and 0 <= before_launch <= 24 * 60:
            return f"{len(group)} top wallets withdrew from known CEX {_short(funder)} within {window:.0f}m, {before_launch / 60:.1f}h before launch — coordinated entry has an exchange funding trail"
    return None


def holder_overlap(first: HolderSnapshot, second: HolderSnapshot, top_n: int = 100) -> float:
    owners_a = {balance.owner for balance in first.balances[:top_n]}
    owners_b = {balance.owner for balance in second.balances[:top_n]}
    smaller = min(len(owners_a), len(owners_b))
    return len(owners_a & owners_b) / smaller * 100 if smaller else 0.0


def data_quality_alerts(ledger: Ledger, tokens: list[TokenSnapshot], run_date: str) -> list[str]:
    fields = {
        "market_cap": [token.market_cap or None for token in tokens],
        "liquidity_usd": [token.liquidity_usd or None for token in tokens],
        "volume_24h": [token.volume_24h or None for token in tokens],
        "price_usd": [token.price_usd or None for token in tokens],
        "dex_id": [None if token.dex_id == "unknown" else 1.0 for token in tokens],
        "socials": [1.0 if token.socials else None for token in tokens],
    }
    alerts: list[str] = []
    for field, values in fields.items():
        numeric = [float(value) for value in values if value is not None]
        alerts.extend(ledger.record_quality(
            run_date, field, len(values) - len(numeric), len(values),
            sum(numeric) / len(numeric) if numeric else None,
        ))
    return alerts


def _short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}" if len(address) > 12 else address
