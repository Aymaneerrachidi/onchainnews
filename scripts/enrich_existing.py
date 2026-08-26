"""Enrich a fixed saved runner set without running market discovery again.

Usage:
    uv run python scripts/enrich_existing.py --limit 40

The command is deliberately delivery-free. It refreshes exact contracts from
Dexscreener, scans the configured monitored X accounts, and uses sourced web
research only where no legitimate X evidence matched that token.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brief.config import Settings, load_settings
from brief.ledger import open_ledger
from brief.lore import attach_lore
from brief.models import Candidate, Enrichment, SafetyReport, Signals, TokenSnapshot, XInteraction, integer
from brief.newsletter import research_day
from brief.sources.dexscreener import DexscreenerSource, merge_token_snapshots
from brief.sources.http import CachedHttpClient
from brief.sources.social import match_x_interactions
from brief.sources.x import TwitterApiIoSource, candidate_search_terms, load_x_accounts


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _optional_number(value: Any) -> float | None:
    return None if value is None else _number(value)


def _candidate(row: dict[str, Any], token: TokenSnapshot | None) -> Candidate:
    if token is None:
        token = TokenSnapshot(
            mint=str(row.get("mint") or ""),
            symbol=str(row.get("symbol") or "?"),
            name=str(row.get("name") or row.get("symbol") or "unknown"),
            chain_id=str(row.get("chain") or ""),
            pair_address="",
            url=str(row.get("url") or ""),
            price_usd=0.0,
            market_cap=_number(row.get("marketCap")),
            liquidity_usd=_number(row.get("liquidity")),
            volume_24h=_number(row.get("volume24h")),
            volume_6h=_number(row.get("volume6h")),
            price_change_24h=_number(row.get("change24h")),
            price_change_6h=_number(row.get("change6h")),
            price_change_1h=_number(row.get("change1h")),
            pair_created_at=None,
            socials=list(row.get("socials") or []),
        )
    signals = Signals(
        turnover=_number(row.get("turnover")),
        acceleration=0.0,
        buy_imbalance_1h=None,
        buy_imbalance_6h=_optional_number(row.get("buyRatio6h")),
        liquidity_depth=(token.liquidity_usd / token.market_cap if token.market_cap else 0.0),
        holder_growth_24h=None,
        maker_quality=None,
        age_hours=_optional_number(row.get("ageHours")),
    )
    risks = [str(value) for value in (row.get("securityFlags") or [])]
    candidate = Candidate(
        token=token,
        signals=signals,
        safety=SafetyReport(
            mint=token.mint,
            mint_authority_renounced=row.get("mintAuthorityRenounced"),
            freeze_authority_disabled=row.get("freezeAuthorityDisabled"),
            lp_locked_or_burned_pct=_optional_number(row.get("lpLockedPct")),
            top10_pct=_optional_number(row.get("top10Pct")),
            risk_flags=risks,
            holder_count=int(row.get("holders")) if row.get("holders") is not None else None,
            rugged=bool(row.get("rugged", False)),
            source=str(row.get("safetySource") or "saved-scan"),
        ),
        enrichment=Enrichment(
            holder_count=int(row.get("holders")) if row.get("holders") is not None else None,
        ),
        risk_labels=[str(value) for value in (row.get("riskLabels") or [])],
        recycled_label_count=(
            1 if any("ticker also used" in str(value).lower() for value in (row.get("riskLabels") or [])) else 0
        ),
        provider_evidence=dict(row.get("providerEvidence") or {}),
        scores=dict(row.get("scores") or {}),
        classification=str(row.get("classification") or ""),
    )
    for item in row.get("xInteractions") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        try:
            created_at = datetime.fromisoformat(
                str(item.get("createdAt") or "").replace("Z", "+00:00")
            )
        except ValueError:
            created_at = datetime.now(timezone.utc)
        candidate.x_interactions.append(XInteraction(
            author_handle=str(item.get("handle") or ""),
            author_name=str(item.get("author") or item.get("handle") or ""),
            interaction=str(item.get("interaction") or "posted"),
            summary=str(item.get("summary") or ""),
            url=str(item.get("url") or ""),
            created_at=created_at,
            confidence=str(item.get("confidence") or "possible"),
            matched_on=str(item.get("matchedOn") or "saved audit"),
            like_count=integer(item.get("likes")),
            repost_count=integer(item.get("reposts")),
            reply_count=integer(item.get("replies")),
            quote_count=integer(item.get("quotes")),
        ))
    return candidate


def _interaction_rows(candidate: Candidate) -> list[dict[str, Any]]:
    return [{
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
    } for item in candidate.x_interactions]


def _curated_by_mint(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("mint") or "").lower(): row
        for row in rows
        if isinstance(row, dict) and row.get("mint")
    }


def _apply_curated(result: dict[str, Any], curated_path: Path | None) -> int:
    curated = _curated_by_mint(curated_path)
    applied = 0
    for coin in result.get("coins") or []:
        research = curated.get(str(coin.get("mint") or "").lower())
        if not research:
            continue
        sources = [str(url) for url in (research.get("sources") or []) if str(url)]
        coin["lore"] = str(research.get("lore") or coin.get("lore") or "")
        coin["researchStatus"] = str(research.get("researchStatus") or "")
        coin["researchSources"] = sources
        coin["webResearch"] = {
            "summary": coin["lore"],
            "status": coin["researchStatus"],
            "sources": sources,
        }
        applied += 1
    result["codexWebResearchedCoins"] = applied
    return applied


async def run(
    limit: int,
    source_path: Path,
    output_path: Path,
    curated_path: Path | None = None,
) -> None:
    settings = load_settings(ROOT / "config.toml")
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    rows = list(payload.get("runnerUniverse") or [])[:limit]
    if not rows:
        raise RuntimeError(f"no runnerUniverse rows in {source_path}")

    ledger = open_ledger(settings)
    http = CachedHttpClient(
        ledger,
        timeout=float(settings.get("run", "request_timeout_seconds", 15)),
    )
    try:
        cache = settings.section("cache")
        urls = settings.section("sources")
        chains = tuple(dict.fromkeys(str(row.get("chain") or "") for row in rows))
        dex = DexscreenerSource(
            http,
            str(urls.get("dexscreener_base_url", "https://api.dexscreener.com")),
            int(cache.get("discovery_ttl_seconds", 600)),
            int(cache.get("pairs_ttl_seconds", 60)),
            chains=chains,
        )
        exact = [(str(row.get("chain") or ""), str(row.get("mint") or "")) for row in rows]
        refreshed = merge_token_snapshots(await dex.token_pairs(exact))
        by_mint = {token.mint.lower(): token for token in refreshed}
        candidates = [
            _candidate(row, by_mint.get(str(row.get("mint") or "").lower()))
            for row in rows
        ]

        x = settings.section("x")
        x_source = TwitterApiIoSource(
            http,
            str(urls.get("twitterapi_io_search_url", "https://api.twitterapi.io/twitter/tweet/advanced_search")),
            os.getenv("TWITTERAPI_IO_KEY"),
            load_x_accounts(
                [str(handle) for handle in (x.get("accounts") or [])],
                [str(path) for path in (x.get("account_files") or [])],
                root=settings.root,
            ),
            ttl=int(cache.get("x_ttl_seconds", 300)),
            requests_per_minute=int(x.get("requests_per_minute", 60)),
            accounts_per_query=int(x.get("accounts_per_query", 20)),
            max_pages_per_query=int(x.get("max_pages_per_query", 5)),
        )
        posts = await x_source.posts_for_terms(
            datetime.now(timezone.utc) - timedelta(hours=36),
            [candidate_search_terms(candidate) for candidate in candidates],
            max_pages_per_query=1,
        )
        match_x_interactions(
            candidates, posts,
            max_per_token=int(x.get("max_matches_per_token", 6)),
            informative_only=False,
            editorial_accounts=x.get("editorial_accounts", []),
            internal_only_accounts=x.get("internal_only_accounts", []),
        )

        # Free search gets the first pass. Paid browser research then runs on
        # every coin that still has no legitimate monitored-X evidence.
        settings.values.setdefault("lore", {})["max_coins"] = len(candidates)
        settings.values.setdefault("newsletter", {})["research_enabled"] = True
        settings.values["newsletter"]["research_only_without_x"] = True
        settings.values["newsletter"]["research_limit"] = 0
        await attach_lore(candidates, settings)
        researched = await research_day(candidates, settings)

        enriched_rows: list[dict[str, Any]] = []
        for original, candidate in zip(rows, candidates):
            row = dict(original)
            token = candidate.token
            row.update({
                "symbol": token.symbol,
                "name": token.name,
                "url": token.url,
                "marketCap": token.market_cap,
                "liquidity": token.liquidity_usd,
                "volume24h": token.volume_24h,
                "change24h": token.price_change_24h,
                "change6h": token.price_change_6h,
                "change1h": token.price_change_1h,
                "socials": token.socials,
                "lore": candidate.lore,
                "catalyst": candidate.catalyst,
                "xInteractions": _interaction_rows(candidate),
                "newsEvidence": candidate.news_evidence,
            })
            enriched_rows.append(row)

        result = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": str(source_path.relative_to(ROOT)),
            "fixedContracts": len(enriched_rows),
            "dexRefreshed": len(refreshed),
            "monitoredHandles": len(x_source.accounts),
            "postsRead": len(posts),
            "xMatchedCoins": sum(bool(candidate.x_interactions) for candidate in candidates),
            "internalXLeadCoins": sum(bool(candidate.internal_x_leads) for candidate in candidates),
            "browserResearchedCoins": researched,
            "coins": enriched_rows,
        }
        _apply_curated(result, curated_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key != "coins"}, ensure_ascii=False))
    finally:
        await http.close()
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--source", type=Path, default=ROOT / "web" / "data" / "latest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "enriched-current-40.json")
    parser.add_argument(
        "--curated", type=Path,
        default=ROOT / "brief" / "curated_lore.json",
    )
    parser.add_argument("--merge-curated-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    curated = args.curated.resolve() if args.curated else None
    if args.merge_curated_only:
        result = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
        source = json.loads(args.source.resolve().read_text(encoding="utf-8"))
        source_rows = list(source.get("runnerUniverse") or source.get("runners") or [])[:args.limit]
        saved_by_mint = {
            str(row.get("mint") or "").lower(): row
            for row in result.get("coins") or []
            if isinstance(row, dict) and row.get("mint")
        }
        # A fresh scan may have a completely different runner board. Seed the
        # editorial artifact from that board, retaining any prior enrichment
        # only for contracts that are still present.
        result["coins"] = [
            {**dict(row), **saved_by_mint.get(str(row.get("mint") or "").lower(), {})}
            for row in source_rows
        ]
        result["generatedAt"] = source.get("generatedAt")
        result["source"] = str(args.source)
        result["fixedContracts"] = len(result["coins"])
        applied = _apply_curated(result, curated)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), "codexWebResearchedCoins": applied}))
        return
    asyncio.run(run(args.limit, args.source.resolve(), output, curated))


if __name__ == "__main__":
    main()
