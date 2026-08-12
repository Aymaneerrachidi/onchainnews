"""The report as data, for the hosted site to render.

The HTML renderer produces a finished page. The website needs the same facts as
structured values so it can lay them out its own way, filter them, and stay
readable on a phone. Both come from one brief, so the site can never disagree
with the report.
"""
from __future__ import annotations

from typing import Any

from brief.models import Brief, Candidate


def _candidate(candidate: Candidate) -> dict[str, Any]:
    token = candidate.token
    signal = candidate.signals
    return {
        "symbol": token.symbol,
        "name": token.name,
        "mint": token.mint,
        "url": token.url,
        "dex": token.dex_id,
        "marketCap": token.market_cap,
        "liquidity": token.liquidity_usd,
        "volume24h": token.volume_24h,
        "change24h": token.price_change_24h,
        "change6h": token.price_change_6h,
        "change1h": token.price_change_1h,
        "runMultiple": candidate.run_multiple,
        "turnover": signal.turnover,
        "ageHours": signal.age_hours,
        "buyRatio6h": signal.buy_imbalance_6h,
        "trades6h": token.txns_6h.total,
        "top10Pct": candidate.safety.top10_pct,
        "lpLockedPct": candidate.safety.lp_locked_or_burned_pct,
        "riskLabels": candidate.risk_labels,
        "lore": candidate.lore,
        "loreIsFresh": candidate.lore_is_fresh,
        "faded": candidate.faded_from_peak,
        "kolBuyers": candidate.kol_buyers,
        "kolHolders": candidate.kol_holders,
        "kolRealisedSol": candidate.kol_realised_sol,
        "read": candidate.read,
        "track": candidate.track,
        "dexEvidence": candidate.dex_evidence,
        "catalyst": candidate.catalyst,
        "xInteractions": [
            {
                "author": item.author_name,
                "handle": item.author_handle,
                "interaction": item.interaction,
                "summary": item.summary,
                "url": item.url,
                "createdAt": item.created_at.isoformat(),
                "confidence": item.confidence,
                "matchedOn": item.matched_on,
                "likes": item.like_count,
                "reposts": item.repost_count,
                "replies": item.reply_count,
                "quotes": item.quote_count,
            }
            for item in candidate.x_interactions
        ],
        "sources": [
            {"label": "Dexscreener", "url": token.url},
            {"label": "Bubblemaps", "url": f"https://app.bubblemaps.io/sol/token/{token.mint}"},
            *[
                {"label": f"@{item.author_handle} on X", "url": item.url}
                for item in candidate.x_interactions
            ],
        ],
    }


def journal_rule(brief: Brief) -> str:
    """How a coin got into the journal, in the journal's own terms."""
    return (
        "Two doors: created inside the last 24 hours and up at least 30%, or any age doing a 5x "
        "or better. Both require $150,000 market cap and $50,000 of 24-hour volume. Safety problems "
        "are labelled on the row; only a live mint or freeze authority, unlocked liquidity, bundled "
        "supply or a manufactured tape removes a coin from the record."
    )


def build_payload(brief: Brief) -> dict[str, Any]:
    runners = [_candidate(candidate) for candidate in brief.runners]
    fresh = sum(
        1 for candidate in brief.runners
        if candidate.signals.age_hours is not None and candidate.signals.age_hours <= 24
    )
    return {
        "generatedAt": brief.generated_at.isoformat(),
        "windowStart": brief.window_start.isoformat() if brief.window_start else None,
        "timezone": brief.generated_at.tzname() or "local",
        "summary": {
            "runners": len(brief.runners),
            "launchedToday": fresh,
            "bigMultiples": sum(1 for c in brief.runners if c.run_multiple >= 5),
            "kolFlagged": len(brief.kol_flagged),
            "xMatched": sum(bool(candidate.x_interactions) for candidate in brief.runners),
            "loreGroups": len(brief.lore_groups),
            "disqualified": len(brief.blocked_runners),
            "walletsTracked": brief.kol_wallet_count,
        },
        "runners": runners,
        "disqualified": [
            {
                "symbol": candidate.token.symbol,
                "mint": candidate.token.mint,
                "url": candidate.token.url,
                "change24h": candidate.token.price_change_24h,
                "runMultiple": candidate.run_multiple,
                "reasons": candidate.risk_labels,
            }
            for candidate in brief.blocked_runners
        ],
        "loreGroups": [
            {
                "name": name,
                "members": [candidate.token.symbol for candidate in members],
            }
            for name, members in sorted(
                brief.lore_groups.items(), key=lambda item: len(item[1]), reverse=True
            )
        ],
        "kolConviction": [
            {
                "symbol": candidate.token.symbol,
                "mint": candidate.token.mint,
                "url": candidate.token.url,
                "buyers": candidate.kol_buyers,
                "holders": candidate.kol_holders,
                "realisedSol": candidate.kol_realised_sol,
                "runMultiple": candidate.run_multiple,
            }
            for candidate in brief.kol_flagged
        ],
        "kolProfit": [
            {"mint": mint, "symbol": symbol, "realisedSol": realised, "traders": traders}
            for mint, symbol, realised, traders in brief.kol_profit_table
        ],
        "sources": [
            {"name": status.name, "ok": status.available, "detail": status.detail}
            for status in brief.source_statuses
        ],
        "journalRule": journal_rule(brief),
        "selectionRule": brief.selection_rule,
    }
