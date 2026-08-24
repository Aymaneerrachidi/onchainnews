"""Fresh exact-contract checks immediately before outbound delivery."""
from __future__ import annotations

import asyncio
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from brief.config import Settings
from brief.holders import BURN_ADDRESSES, build_snapshot
from brief.models import Candidate, SafetyReport, SourceStatus
from brief.sources.gmgn import GmgnSource, safety_from_evidence
from brief.sources.goplus import GoPlusSource, supports as goplus_supports
from brief.sources.helius import HeliusSource
from brief.sources.http import CachedHttpClient
from brief.sources.rugcheck import RugCheckSource


async def refresh_delivery_evidence(
    candidates: Iterable[Candidate],
    settings: Settings,
    ledger,
) -> list[SourceStatus]:
    """Re-query only contracts that will be named, with no broad discovery work."""
    unique = {candidate.token.mint: candidate for candidate in candidates}
    if not unique:
        return []

    run = settings.section("run")
    urls = settings.section("sources")
    cache = settings.section("cache")
    http = CachedHttpClient(ledger, timeout=float(run.get("request_timeout_seconds", 15)))
    statuses: list[SourceStatus] = []
    try:
        chains = tuple(dict.fromkeys(c.token.chain_id.lower() for c in unique.values()))
        gmgn = GmgnSource(
            timeout=float(settings.get("gmgn", "command_timeout_seconds", 30) or 30),
            ledger=ledger,
            # A pre-delivery audit is a recheck, not a replay of a stale rank.
            cache_ttl=0,
            min_interval_seconds=float(settings.get("gmgn", "min_interval_seconds", 1.25) or 1.25),
            chains=chains,
        )
        evidence = {
            mint: dict((candidate.provider_evidence.get("gmgn", {}) or {}))
            for mint, candidate in unique.items()
        }
        candidates_list = list(unique.values())
        missing_structure = [
            candidate for candidate in candidates_list
            if evidence.get(candidate.token.mint, {}).get("holders") is None
            or evidence.get(candidate.token.mint, {}).get("top10Pct") is None
        ]
        if missing_structure:
            statuses.append(await gmgn.enrich_missing_wallet_counts(
                missing_structure, evidence, limit=0, force_all=True,
            ))

        # The free endpoint can pause after a burst of exact trader histories.
        # Resume only the unfinished contracts after a full backoff window.
        # Empty histories are still marked checked and correctly fail the KOL
        # requirement without being queried forever.
        pending = list(candidates_list)
        trader_statuses: list[SourceStatus] = []
        max_attempts = max(1, math.ceil(len(pending) / 10) + 1)
        for attempt in range(max_attempts):
            source = gmgn if attempt == 0 else GmgnSource(
                timeout=float(settings.get("gmgn", "command_timeout_seconds", 30) or 30),
                ledger=ledger,
                cache_ttl=0,
                min_interval_seconds=float(settings.get("gmgn", "min_interval_seconds", 1.25) or 1.25),
                chains=chains,
            )
            trader_statuses.append(await source.enrich_runner_traders(
                pending,
                evidence,
                limit=0,
                rows_per_token=int(settings.get("gmgn", "runner_trader_rows", 20) or 20),
            ))
            pending = [
                candidate for candidate in pending
                if not evidence.get(candidate.token.mint, {}).get("exactTraderHistoryChecked")
            ]
            if not pending:
                break
            await asyncio.sleep(float(settings.get("gmgn", "rate_limit_backoff_seconds", 65) or 65))
        checked_traders = len(candidates_list) - len(pending)
        statuses.append(SourceStatus(
            "Preflight GMGN renowned traders",
            not pending,
            f"{checked_traders}/{len(candidates_list)} exact contracts checked"
            + (f" after {len(trader_statuses)} paced pass(es)" if len(trader_statuses) > 1 else ""),
        ))
        for mint, candidate in unique.items():
            candidate.provider_evidence["gmgn"] = evidence.get(mint, {})

        by_chain: dict[str, list[str]] = defaultdict(list)
        for candidate in unique.values():
            by_chain[candidate.token.chain_id.lower()].append(candidate.token.mint)

        reports: dict[str, SafetyReport] = {}
        solana = by_chain.get("solana", [])
        if solana:
            rug = RugCheckSource(
                http,
                str(urls.get("rugcheck_base_url", "https://api.rugcheck.xyz/v1")),
                0,
                requests_per_minute=int(settings.get("rugcheck", "requests_per_minute", 45)),
            )
            results = await asyncio.gather(
                *(rug.report(mint) for mint in solana), return_exceptions=True
            )
            for mint, result in zip(solana, results):
                if isinstance(result, SafetyReport):
                    reports[mint] = result
            statuses.append(SourceStatus(
                "Preflight RugCheck", len(reports) >= len(solana),
                f"{sum(mint in reports for mint in solana)}/{len(solana)} exact Solana contracts rechecked",
            ))

        goplus = GoPlusSource(
            http,
            str(urls.get("goplus_base_url", "https://api.gopluslabs.io")),
            0,
            requests_per_minute=int(settings.get("goplus", "requests_per_minute", 30)),
        )
        for chain, mints in by_chain.items():
            if chain == "solana" or not goplus_supports(chain):
                continue
            found = await goplus.reports(chain, mints)
            reports.update(found)
            statuses.append(SourceStatus(
                f"Preflight GoPlus ({chain})", len(found) == len(mints),
                f"{len(found)}/{len(mints)} exact contracts rechecked",
            ))

        # Chains outside GoPlus use GMGN's contract fields. Missing answers stay
        # None and are rejected by the fail-closed preflight.
        for chain, mints in by_chain.items():
            if chain == "solana" or goplus_supports(chain):
                continue
            for mint in mints:
                reports[mint] = safety_from_evidence(mint, evidence.get(mint, {}))

        helius = HeliusSource(
            http,
            str(urls.get("helius_base_url", "https://mainnet.helius-rpc.com")),
            os.getenv("HELIUS_API_KEY"),
            0,
            requests_per_minute=int(settings.get("holders", "helius_requests_per_minute", 100)),
            holder_page_limit=int(settings.get("holders", "holder_page_limit", 1000)),
            max_holder_pages=int(settings.get("holders", "max_holder_pages", 100)),
        )
        enrichments = await helius.enrich_batch(solana) if solana and helius.configured else {}
        authorities = await helius.mint_authorities_batch(solana) if solana and helius.configured else {}
        for mint, authority in authorities.items():
            enrichment = enrichments.setdefault(mint, authority)
            enrichment.mint_authority_renounced = authority.mint_authority_renounced
            enrichment.freeze_authority_disabled = authority.freeze_authority_disabled
            if enrichment.supply_raw is None:
                enrichment.supply_raw = authority.supply_raw
            if enrichment.decimals is None:
                enrichment.decimals = authority.decimals
        statuses.append(SourceStatus(
            "Preflight Helius", len(authorities) == len(solana),
            f"{len(authorities)}/{len(solana)} SPL mint authorities rechecked directly",
        ))

        for mint, candidate in unique.items():
            if mint in reports:
                report = reports[mint]
                gmgn_evidence = evidence.get(mint, {})
                if report.holder_count is None and gmgn_evidence.get("holders"):
                    report.holder_count = int(gmgn_evidence["holders"])
                if report.top10_pct is None and gmgn_evidence.get("top10Pct") is not None:
                    report.top10_pct = float(gmgn_evidence["top10Pct"])
                candidate.safety = report
            if mint in enrichments:
                candidate.enrichment = enrichments[mint]
                if candidate.enrichment.holder_count:
                    candidate.safety.holder_count = candidate.enrichment.holder_count

        # A missing aggregate holder count is recoverable from the full DAS
        # account list. This is deliberately finalist-only and excludes the
        # pool, lockers, burn addresses and configured CEX wallets.
        missing_holders = [
            candidate for candidate in unique.values()
            if candidate.token.chain_id.lower() == "solana"
            and not (candidate.enrichment.holder_count or candidate.safety.holder_count)
        ]
        holder_failures = 0
        known_cex = {
            str(value) for value in (settings.get("holders", "known_cex_wallets", []) or [])
        }
        for candidate in missing_holders:
            try:
                balances, excluded = await helius.token_holders(
                    candidate.token.mint,
                    excluded_accounts=set(candidate.safety.excluded_accounts) | set(BURN_ADDRESSES),
                    excluded_owners=set(candidate.safety.excluded_owners) | set(BURN_ADDRESSES) | known_cex,
                    ttl=0,
                )
                snapshot = build_snapshot(
                    candidate.token.mint,
                    candidate.token.pair_created_at or datetime.now(timezone.utc),
                    balances,
                    excluded,
                )
                candidate.safety.holder_count = snapshot.holder_count
                candidate.enrichment.holder_count = snapshot.holder_count
                if candidate.safety.top10_pct is None:
                    candidate.safety.top10_pct = snapshot.top10_pct
            except Exception:
                holder_failures += 1
        if missing_holders:
            statuses.append(SourceStatus(
                "Preflight full holder fallback", holder_failures == 0,
                f"{len(missing_holders) - holder_failures}/{len(missing_holders)} missing holder counts reconstructed",
            ))
        return statuses
    finally:
        await http.close()
