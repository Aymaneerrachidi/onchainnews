"""Enrich a fixed saved runner set without running market discovery again.

Usage:
    uv run python scripts/enrich_existing.py

The command is deliberately delivery-free. It refreshes exact contracts from
Dexscreener, scans the configured monitored X accounts, and uses sourced web
research only where no legitimate X evidence matched that token.

By default every contract in ``runnerUniverse`` is enriched. ``--limit`` is
only an explicit troubleshooting override; publication code must never use it
for a normal daily run.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brief.config import Settings, load_settings
from brief.ledger import open_ledger
from brief.lore import attach_lore
from brief.lore_style import humanize_lore
from brief.models import Candidate, Enrichment, SafetyReport, Signals, TokenSnapshot, XInteraction, integer
from brief.sources.dexscreener import DexscreenerSource, merge_token_snapshots
from brief.sources.http import CachedHttpClient
from brief.sources.linked_socials import attach_linked_x_posts
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
        coin["lore"] = humanize_lore(research.get("lore") or coin.get("lore") or "")
        coin["researchStatus"] = str(research.get("researchStatus") or "")
        coin["researchSources"] = sources
        coin["webResearch"] = {
            "summary": coin["lore"],
            "status": coin["researchStatus"],
            "sources": sources,
        }
        applied += 1
    # Old artifacts may already contain a raw X post or scraped page as lore.
    # Curated rows above are safe; force every other unsafe row back through
    # the non-quoting fallback instead of preserving yesterday's bad copy.
    for coin in result.get("coins") or []:
        mint = str(coin.get("mint") or "").lower()
        if mint not in curated and _looks_like_unsynthesized_evidence(coin.get("lore")):
            coin["lore"] = ""
    result["codexWebResearchedCoins"] = applied
    _fill_evidence_recaps(result)
    return applied


def _clean_evidence_text(value: Any, limit: int = 280) -> str:
    text = re.sub(r"https?://\S+", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" -\n\t")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return text


def _looks_like_unsynthesized_evidence(value: Any) -> bool:
    text = str(value or "")
    lowered = text.casefold()
    return any(marker in lowered for marker in (
        "'s move came with a linked post",
        "title:",
        "published time:",
        "just aped",
        "signal group",
        "from my call",
    )) or bool(re.search(r"[1-9A-HJ-NP-Za-km-z]{40,64}", text))


def _thin_evidence_lore(symbol: str, mint: str, attribution: str) -> str:
    variants = (
        f"{symbol} had a real source attached to the contract{attribution}, but it amounted to trading chatter rather than a verifiable story. The runner stays on the board without a made-up catalyst.",
        f"The public trail for {symbol}{attribution} confirmed people were discussing the right contract, not why it existed or moved. There was not enough substance to call a catalyst.",
        f"{symbol} reached the tape with contract-matched attention{attribution}. Nothing in that material established an original meme, creator event, or product update, so the desk left the narrative open.",
        f"A contract-specific mention put {symbol} on the social radar{attribution}, although the post itself was a trade reaction rather than usable lore. No stronger trigger was verified.",
        f"What surfaced for {symbol}{attribution} proved the market was being watched, but did not explain the coin beyond that activity. Its underlying story remains unconfirmed.",
        f"The linked evidence around {symbol}{attribution} was relevant to this exact mint but too promotional or thin to support a clean narrative. It qualified on trading strength, not a confirmed news event.",
    )
    return variants[sum(ord(char) for char in mint) % len(variants)]


def _fill_evidence_recaps(result: dict[str, Any]) -> None:
    """Give every qualified runner an honest, readable desk note.

    Curated research always wins. Remaining coins use their exact linked X or
    web evidence; a final market-only note makes the absence of a verified
    story explicit instead of silently leaving the runner blank.
    """
    evidence_count = 0
    market_only_count = 0
    for coin in result.get("coins") or []:
        if str(coin.get("lore") or "").strip():
            continue
        symbol = str(coin.get("symbol") or coin.get("name") or "This runner")
        interactions = [x for x in (coin.get("xInteractions") or []) if isinstance(x, dict)]
        evidence = [x for x in (coin.get("newsEvidence") or []) if isinstance(x, dict)]
        item = interactions[0] if interactions else (evidence[0] if evidence else None)
        if item:
            # Source summaries may contain a complete scraped X post, account
            # counters, contracts, and search boilerplate. Those are research
            # inputs, never publishable recap copy. Until an editorial pass
            # synthesizes the context, disclose that the evidence was thin.
            author = _clean_evidence_text(item.get("author") or item.get("source"), 60)
            attribution = f" from {author}" if author else ""
            lore = _thin_evidence_lore(symbol, str(coin.get("mint") or ""), attribution)
            coin["lore"] = humanize_lore(lore)
            coin["researchStatus"] = coin.get("researchStatus") or "linked_evidence"
            url = str(item.get("url") or "")
            coin["researchSources"] = [url] if url else []
            coin["webResearch"] = {
                "summary": coin["lore"],
                "status": coin["researchStatus"],
                "sources": coin["researchSources"],
            }
            evidence_count += 1
            continue
        coin["lore"] = humanize_lore(
            f"{symbol} qualified on the 24-hour market tape, but no trustworthy linked post, creator story, or outside catalyst was available."
        )
        coin["researchStatus"] = coin.get("researchStatus") or "not_found"
        coin["researchSources"] = []
        coin["webResearch"] = {
            "summary": coin["lore"],
            "status": coin["researchStatus"],
            "sources": [],
        }
        market_only_count += 1
    result["evidenceEnrichedCoins"] = evidence_count
    result["marketOnlyRecapCoins"] = market_only_count


def _runner_rows(payload: dict[str, Any], limit: int = 0) -> list[dict[str, Any]]:
    """Return the complete qualified universe unless a debug limit is explicit."""
    rows = list(payload.get("runnerUniverse") or payload.get("runners") or [])
    return rows[:limit] if limit > 0 else rows


async def run(
    limit: int,
    source_path: Path,
    output_path: Path,
    curated_path: Path | None = None,
) -> None:
    settings = load_settings(ROOT / "config.toml")
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    rows = _runner_rows(payload, limit)
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

        # The monitored-account matcher replaces its candidate interaction
        # list, so attach exact-contract profile posts afterwards. This also
        # lets them suppress generic lore fallback when stronger X evidence
        # is available.
        linked_x_posts = await attach_linked_x_posts(
            candidates, api_key=os.getenv("TWITTERAPI_IO_KEY")
        )

        # Automated enrichment uses deterministic free sources. Deep Codex
        # web findings are merged separately from curated_lore.json.
        settings.values.setdefault("lore", {})["max_coins"] = len(candidates)
        await attach_lore(candidates, settings)
        researched = 0

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
            "linkedXPostsRead": linked_x_posts,
            "xMatchedCoins": sum(bool(candidate.x_interactions) for candidate in candidates),
            "internalXLeadCoins": sum(bool(candidate.internal_x_leads) for candidate in candidates),
            "browserResearchedCoins": researched,
            "codexResearchQueue": [
                {
                    "chain": candidate.token.chain_id,
                    "mint": candidate.token.mint,
                    "symbol": candidate.token.symbol,
                    "name": candidate.token.name,
                    "dexUrl": candidate.token.url,
                    "linkedSources": [
                        str((item or {}).get("url") or "")
                        for item in candidate.token.socials or []
                        if isinstance(item, dict) and (item or {}).get("url")
                    ],
                }
                for candidate in candidates
                if not candidate.x_interactions and not candidate.news_evidence
            ],
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
    parser.add_argument(
        "--limit", type=int, default=0,
        help="debug-only cap; 0 (default) enriches the full qualified runner universe",
    )
    parser.add_argument("--source", type=Path, default=ROOT / "web" / "data" / "latest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "enriched-current-all.json")
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
        source_rows = _runner_rows(source, args.limit)
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
