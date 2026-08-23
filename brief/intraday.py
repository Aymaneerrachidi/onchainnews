from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brief.config import Settings
from brief.models import Candidate, Enrichment, SafetyReport, Signals, TokenSnapshot, TransactionWindow


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def pulse_state_path(settings: Settings) -> Path:
    raw = str(settings.get("pulse", "state_path", "web/data/pulse-state.json"))
    path = Path(raw)
    return path if path.is_absolute() else settings.root / path


def load_pulse_passes(settings: Settings, window_start: datetime, now: datetime) -> dict[str, dict[str, Any]]:
    """Return each alerted mint's strongest runner pass inside the report window."""
    path = pulse_state_path(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    start = window_start.astimezone(timezone.utc)
    end = now.astimezone(timezone.utc)
    alerted_only = bool(settings.get("journal", "pulse_recap_alerted_only", True))
    posted = data.get("posted") or {}
    best: dict[str, dict[str, Any]] = {}
    for mint, entries in (data.get("passes") or {}).items():
        if not isinstance(entries, list):
            continue
        if alerted_only:
            alert_stamp = _parse_time(posted.get(str(mint)))
            if alert_stamp is None or alert_stamp < start or alert_stamp > end:
                continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            stamp = _parse_time(entry.get("takenAt"))
            if stamp is None or stamp < start or stamp > end:
                continue
            score = (
                _number(entry.get("marketCap")) * 0.000001
                + _number(entry.get("volume24h")) * 0.0000005
                + _number(entry.get("runMultiple")) * 10
                + _number((entry.get("scores") or {}).get("runner"))
            )
            prior = best.get(str(mint))
            prior_score = _number(prior.get("_score")) if prior else -1
            if score > prior_score:
                row = dict(entry)
                row["_score"] = score
                best[str(mint)] = row
    return best


def hard_blocked_pass(entry: dict[str, Any], settings: Settings) -> bool:
    terms = [
        str(term).casefold()
        for term in (settings.get("journal", "fill_hard_block_terms", []) or [])
        if str(term).strip()
    ]
    labels = " | ".join(str(label) for label in (entry.get("riskLabels") or [])).casefold()
    return any(term in labels for term in terms)


def pass_crosses_intraday_floor(entry: dict[str, Any], settings: Settings) -> bool:
    """A historical pass can qualify even if current market cap later faded."""
    if hard_blocked_pass(entry, settings):
        return False
    min_peak_mcap = float(settings.get("journal", "intraday_min_peak_market_cap", settings.get("thresholds", "min_market_cap", 200_000)) or 200_000)
    min_volume = float(settings.get("journal", "intraday_min_volume_24h", settings.get("journal", "min_volume_24h", 250_000)) or 250_000)
    min_liq = float(settings.get("journal", "intraday_min_liquidity", settings.get("journal", "min_liquidity", 40_000)) or 40_000)
    min_trades = int(settings.get("journal", "intraday_min_trades_24h", settings.get("journal", "min_trades_24h", 1000)) or 1000)
    min_runner = float(settings.get("journal", "intraday_min_runner_score", 25.0) or 25.0)
    max_manipulation = float(settings.get("journal", "intraday_max_manipulation", settings.get("journal", "fill_max_manipulation", 55.0)) or 55.0)
    # An hourly alert proves a coin qualified at some point; it does not prove
    # it ran. Without this floor a flat, week-old coin that tripped one scan
    # rejoins the recap and dilutes the page with 1.0x rows.
    min_change = float(settings.get("journal", "intraday_min_change_24h_pct", 0.0) or 0.0)
    scores = entry.get("scores") or {}
    return (
        _number(entry.get("change24h")) >= min_change
        and _number(entry.get("marketCap")) >= min_peak_mcap
        and _number(entry.get("volume24h")) >= min_volume
        and _number(entry.get("liquidity")) >= min_liq
        and _integer(entry.get("trades24h")) >= min_trades
        and _number(scores.get("runner"), 50.0) >= min_runner
        and _number(scores.get("manipulation"), 0.0) <= max_manipulation
    )


def candidate_from_pass(entry: dict[str, Any], current: TokenSnapshot | None, settings: Settings) -> Candidate:
    """Build a runner row from the best historical pass."""
    buys = _integer(entry.get("buys6h"))
    sells = _integer(entry.get("sells6h"))
    trades_6h = _integer(entry.get("trades6h"))
    if trades_6h and not (buys or sells):
        ratio = _number(entry.get("buyRatio6h"), 0.0)
        buys = round(trades_6h * ratio)
        sells = max(0, trades_6h - buys)
    trades_24h = _integer(entry.get("trades24h"), trades_6h)
    token = TokenSnapshot(
        mint=str(entry.get("mint") or (current.mint if current else "")),
        symbol=str(entry.get("symbol") or (current.symbol if current else "?")).strip(),
        name=str(entry.get("name") or (current.name if current else "")).strip(),
        chain_id=str(entry.get("chain") or (current.chain_id if current else "solana")),
        pair_address=current.pair_address if current else "",
        url=str(entry.get("url") or (current.url if current else "")),
        price_usd=current.price_usd if current else 0.0,
        market_cap=_number(entry.get("marketCap")),
        liquidity_usd=_number(entry.get("liquidity")),
        volume_24h=_number(entry.get("volume24h")),
        volume_6h=_number(entry.get("volume6h"), _number(entry.get("volume24h"))),
        price_change_24h=_number(entry.get("change24h")),
        price_change_6h=_number(entry.get("change6h"), _number(entry.get("change24h"))),
        pair_created_at=current.pair_created_at if current else None,
        dex_id=current.dex_id if current else "unknown",
        price_change_1h=_number(entry.get("change1h")),
        txns_6h=TransactionWindow(buys=buys, sells=sells),
        txns_24h=TransactionWindow(buys=trades_24h, sells=0),
        socials=current.socials if current else [],
        active_boosts=current.active_boosts if current else 0,
        raw=current.raw if current else {},
    )
    safety = SafetyReport(
        mint=token.mint,
        lp_locked_or_burned_pct=_number(entry.get("lpLockedPct"), None),
        top10_pct=_number(entry.get("top10Pct"), None),
        holder_count=_integer(entry.get("holders")) or None,
        source="intraday-pass",
    )
    signal = Signals(
        turnover=_number(entry.get("turnover"), token.volume_24h / token.market_cap if token.market_cap else 0.0),
        acceleration=0.0,
        buy_imbalance_1h=None,
        buy_imbalance_6h=_number(entry.get("buyRatio6h"), None),
        liquidity_depth=token.liquidity_usd / token.market_cap if token.market_cap else 0.0,
        holder_growth_24h=None,
        maker_quality=None,
        age_hours=_number(entry.get("ageHours"), None),
    )
    candidate = Candidate(token=token, signals=signal, safety=safety, enrichment=Enrichment())
    candidate.track = "INTRADAY"
    candidate.run_multiple = _number(entry.get("runMultiple"), 1.0)
    candidate.risk_labels = list(entry.get("riskLabels") or [])
    candidate.scores = dict(entry.get("scores") or {})
    candidate.score_components = dict(entry.get("scoreComponents") or {})
    candidate.classification = str(entry.get("classification") or "INTRADAY RUNNER")
    candidate.read = str(entry.get("read") or f"${token.symbol} ran intraday, then faded by the morning snapshot.")
    candidate.kol_buyers = [str(item) for item in (entry.get("kolBuyers") or [])]
    current_mcap = current.market_cap if current else token.market_cap
    peak = token.market_cap
    if current and peak and current_mcap < peak * 0.8:
        drop = (current_mcap / peak - 1.0) * 100
        candidate.risk_labels.append(f"faded after intraday peak, now {drop:.0f}% from observed high")
    return candidate
