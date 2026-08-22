from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from brief.ledger import Ledger
from brief.models import Candidate, TokenSnapshot


TIER_FLOORS: tuple[tuple[str, float], ...] = (
    ("S", 1_000_000.0),
    ("A", 500_000.0),
    ("B", 250_000.0),
)


def tier_for_peak(peak_market_cap: float) -> str:
    for tier, floor in TIER_FLOORS:
        if peak_market_cap >= floor:
            return tier
    return "BELOW"


def inferred_window_baseline(token: TokenSnapshot) -> float | None:
    """Estimate the 24h starting cap only when a provider gives that change.

    This is a labelled fallback for the first-ever snapshot. Once the local
    tape has an actual earlier observation, the real stored value wins.
    """
    current = float(token.market_cap or 0)
    change = float(token.price_change_24h or 0)
    denominator = 1.0 + change / 100.0
    if current <= 0 or denominator <= 0.01:
        return None
    return current / denominator


def persist_market_tape(
    ledger: Ledger,
    tokens: Iterable[TokenSnapshot],
    now: datetime,
    *,
    provider: str = "provider-union",
    commit: bool = True,
) -> dict[str, list[str]]:
    crossed: dict[str, list[str]] = {}
    if not commit:
        return crossed
    for token in tokens:
        if not token.mint or token.market_cap <= 0:
            continue
        levels = ledger.record_market_snapshot(
            token,
            now,
            provider=provider,
            raw={"dex": token.dex_id, "change24h": token.price_change_24h},
        )
        if levels:
            crossed[token.mint] = levels
    return crossed


def attach_lifecycle(
    candidate: Candidate,
    ledger: Ledger,
    now: datetime,
    *,
    window_hours: float = 24.0,
) -> None:
    gmgn_ath = float((candidate.provider_evidence.get("gmgn", {}) or {}).get("athMarketCap") or 0)
    # The product is restricted to <=24h-old launches, so a GMGN lifetime ATH
    # for those tokens necessarily falls inside the recap window. Never apply
    # lifetime ATH to an older token as if it happened today.
    window_ath = gmgn_ath if candidate.signals.age_hours is not None and candidate.signals.age_hours <= window_hours else 0.0
    lifecycle = ledger.lifecycle(candidate.token.mint, now - timedelta(hours=window_hours), now)
    fallback = inferred_window_baseline(candidate.token)
    if lifecycle is None:
        start = fallback or float(candidate.token.market_cap or 0)
        peak = max(float(candidate.observed_peak_market_cap or 0), window_ath, float(candidate.token.market_cap or 0))
        candidate.first_seen_at = now
        candidate.last_seen_at = now
        candidate.start_market_cap = start or None
        candidate.peak_market_cap = peak or None
        candidate.peak_at = now
        candidate.lifecycle_events = []
        candidate.provider_evidence.setdefault("lifecycle", {})["baseline"] = (
            "provider-24h-inferred" if fallback else "first-current-snapshot"
        )
    else:
        candidate.first_seen_at = datetime.fromisoformat(lifecycle["first_seen_at"])
        candidate.last_seen_at = datetime.fromisoformat(lifecycle["last_seen_at"])
        stored_start = float(lifecycle["start_market_cap"] or 0)
        # On the first observed run, the inferred 24h opening value is more
        # informative than current=current. It is explicitly labelled and will
        # naturally be replaced once multiple real hourly snapshots exist.
        providers = lifecycle.get("providers", [])
        if candidate.first_seen_at == candidate.last_seen_at and fallback:
            start = fallback
            baseline_source = "provider-24h-inferred"
        else:
            start = stored_start
            baseline_source = "local-hourly-snapshot"
        candidate.start_market_cap = start or None
        candidate.peak_market_cap = max(
            float(lifecycle["peak_market_cap"] or 0),
            float(candidate.observed_peak_market_cap or 0),
            window_ath,
            float(candidate.token.market_cap or 0),
        ) or None
        candidate.peak_at = datetime.fromisoformat(lifecycle["peak_at"])
        candidate.lifecycle_events = list(lifecycle.get("events", []))
        candidate.provider_evidence["lifecycle"] = {
            "baseline": baseline_source,
            "snapshotProviders": providers,
            "snapshotCountKnown": True,
        }

    start = float(candidate.start_market_cap or 0)
    peak = float(candidate.peak_market_cap or candidate.token.market_cap or 0)
    current = float(candidate.token.market_cap or 0)
    candidate.peak_multiple = peak / start if start > 0 else None
    candidate.run_multiple = candidate.peak_multiple or candidate.run_multiple
    candidate.drawdown_from_peak_pct = ((peak - current) / peak * 100.0) if peak > 0 else None
    candidate.faded_from_peak = candidate.drawdown_from_peak_pct
    candidate.runner_tier = tier_for_peak(peak)
    candidate.round_trip = (
        candidate.runner_tier in {"S", "A", "B"}
        and candidate.drawdown_from_peak_pct is not None
        and candidate.drawdown_from_peak_pct >= 65.0
    )


def attach_lifecycles(candidates: Iterable[Candidate], ledger: Ledger, now: datetime) -> None:
    for candidate in candidates:
        attach_lifecycle(candidate, ledger, now)


def lifecycle_category(candidate: Candidate) -> str:
    editorial = candidate.provider_evidence.get("editorial", {}) or {}
    if editorial.get("published") is False or candidate.scores.get("manipulation", 0.0) >= 65.0:
        return "questionable"
    if candidate.round_trip:
        return "round_trip"
    if candidate.runner_tier == "S":
        return "major"
    if candidate.runner_tier == "A":
        return "mid"
    if candidate.runner_tier == "B":
        return "emerging"
    return "below_runner_floor"


def build_structured_recap(candidates: Iterable[Candidate], now: datetime) -> dict[str, Any]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            {"S": 3, "A": 2, "B": 1}.get(item.runner_tier, 0),
            float(item.peak_market_cap or 0),
            item.scores.get("runner", 0.0),
        ),
        reverse=True,
    )

    def item(candidate: Candidate) -> dict[str, Any]:
        return {
            "mint": candidate.token.mint,
            "symbol": candidate.token.symbol,
            "name": candidate.token.name,
            "tier": candidate.runner_tier,
            "category": lifecycle_category(candidate),
            "startMarketCap": candidate.start_market_cap,
            "peakMarketCap": candidate.peak_market_cap,
            "currentMarketCap": candidate.token.market_cap,
            "runMultiple": candidate.peak_multiple,
            "drawdownFromPeakPct": candidate.drawdown_from_peak_pct,
            "roundTrip": candidate.round_trip,
            "firstSeenAt": candidate.first_seen_at.isoformat() if candidate.first_seen_at else None,
            "peakAt": candidate.peak_at.isoformat() if candidate.peak_at else None,
            "scores": candidate.scores,
            "scoreConfidence": candidate.score_confidence,
            "classification": candidate.classification,
            "riskLabels": candidate.risk_labels,
            "providerEvidence": candidate.provider_evidence,
            "newsEvidence": candidate.news_evidence,
        }

    def recap_worthy(candidate: Candidate) -> bool:
        return candidate.runner_tier in {"S", "A", "B"}

    included = [candidate for candidate in ordered if recap_worthy(candidate)]
    # `included` is the analyst tape and intentionally retains manipulated or
    # unsafe threshold prints for audit. Public recap bands must contain only
    # candidates that passed the organic publisher gate; otherwise a bogus
    # billion-dollar FDV can become "runner of the day" simply by sorting first.
    clean = [
        candidate
        for candidate in included
        if lifecycle_category(candidate) != "questionable"
    ]
    questionable = [
        candidate
        for candidate in included
        if lifecycle_category(candidate) == "questionable"
    ]
    rows = [item(candidate) for candidate in clean]
    questionable_rows = [item(candidate) for candidate in questionable]
    return {
        "schemaVersion": 4,
        "generatedAt": now.isoformat(),
        "windowHours": 24,
        "runnerOfDay": item(clean[0]) if clean else None,
        "bestOrganic": item(max(clean, key=lambda c: c.scores.get("organic", 0))) if clean else None,
        "tiers": {
            tier: [row for row in rows if row["tier"] == tier]
            for tier in ("S", "A", "B")
        },
        "questionable": questionable_rows,
        "observedAll": [item(candidate) for candidate in included],
        "roundTrips": [row for row in rows if row["roundTrip"]],
        "all": rows,
    }
