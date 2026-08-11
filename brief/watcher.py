from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from brief.config import Settings
from brief.delivery import send_telegram
from brief.holders import BURN_ADDRESSES
from brief.intelligence import detect_lp_removal, pool_liquidity_proxy
from brief.ledger import Ledger
from brief.sources.dexscreener import DexscreenerSource
from brief.sources.helius import HeliusSource
from brief.sources.http import CachedHttpClient
from brief.sources.rugcheck import RugCheckSource


log = logging.getLogger("brief.watcher")


@dataclass(slots=True)
class WatcherResult:
    checked: int = 0
    alerts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def deliver_alerts(alerts: list[str], sender=send_telegram) -> None:
    if alerts:
        await sender([f"SOLANA WATCHER\n{alert}" for alert in alerts])


async def poll_once(
    settings: Settings,
    ledger: Ledger,
    *,
    now: datetime | None = None,
    sender=send_telegram,
) -> WatcherResult:
    now = now or datetime.now(ZoneInfo(str(settings.get("run", "timezone", "UTC"))))
    watched = ledger.watched()[:int(settings.get("holders", "watchlist_limit", 5))]
    result = WatcherResult()
    if not watched:
        return result
    api_key = os.getenv("HELIUS_API_KEY")
    if not api_key:
        result.errors.append("HELIUS_API_KEY is not configured; watcher did not poll")
        return result
    urls = settings.section("sources")
    cache = settings.section("cache")
    http = CachedHttpClient(ledger, timeout=float(settings.get("run", "request_timeout_seconds", 15)))
    try:
        dex = DexscreenerSource(
            http,
            str(urls.get("dexscreener_base_url", "https://api.dexscreener.com")),
            int(cache.get("discovery_ttl_seconds", 600)),
            int(settings.get("intelligence", "watcher_pair_ttl_seconds", 30)),
        )
        rug = RugCheckSource(
            http,
            str(urls.get("rugcheck_base_url", "https://api.rugcheck.xyz/v1")),
            int(settings.get("intelligence", "watcher_rug_ttl_seconds", 900)),
        )
        helius = HeliusSource(
            http,
            str(urls.get("helius_base_url", "https://mainnet.helius-rpc.com")),
            api_key,
            int(cache.get("keyed_ttl_seconds", 900)),
            requests_per_minute=int(settings.get("holders", "helius_requests_per_minute", 100)),
            holder_page_limit=int(settings.get("holders", "holder_page_limit", 1000)),
            max_holder_pages=int(settings.get("holders", "max_holder_pages", 100)),
        )
        pairs = {token.mint: token for token in await dex.token_pairs([row["mint"] for row in watched])}
        known_cex = set(str(value) for value in settings.get("holders", "known_cex_wallets", []))
        for row in watched:
            mint, symbol = row["mint"], row["symbol"]
            token = pairs.get(mint)
            if not token:
                result.errors.append(f"${symbol}: pair unavailable")
                continue
            try:
                report = await rug.report(mint)
                excluded_accounts = set(report.excluded_accounts) | BURN_ADDRESSES
                excluded_owners = set(report.excluded_owners) | BURN_ADDRESSES | known_cex
                holders, _ = await helius.token_holders(
                    mint,
                    excluded_accounts=excluded_accounts,
                    excluded_owners=excluded_owners,
                    ttl=int(settings.get("intelligence", "watcher_holder_cache_seconds", 30)),
                )
                balances = {holder.owner: holder.amount for holder in holders}
                total = sum(balances.values()) or 1
                previous = ledger.latest_watcher_sample(mint)

                if report.lp_vaults:
                    vaults = await helius.token_account_balances(
                        list(report.lp_vaults),
                        ttl=int(settings.get("intelligence", "watcher_holder_cache_seconds", 30)),
                    )
                    proxy = pool_liquidity_proxy(vaults)
                    prior_pool = ledger.latest_pool_snapshot(mint)
                    removal = detect_lp_removal(
                        prior_pool["liquidity_proxy"] if prior_pool else None,
                        proxy,
                        float(settings.get("intelligence", "lp_removal_alert_pct", 10)),
                    )
                    if removal is not None:
                        result.alerts.append(f"${symbol} pool balance proxy fell {removal:.1f}% since the prior poll - material liquidity removal")
                    ledger.record_pool_snapshot(mint, now, vaults, proxy, token.liquidity_usd)

                cluster_wallets = ledger.cluster_wallets_for_token(mint)
                cluster_amount = sum(balances.get(owner, 0) for owner in cluster_wallets)
                latest_creator = ledger.creator_history(mint, 1)
                linked_creator_wallets = set(json.loads(latest_creator[0]["linked_wallets"])) if latest_creator else set()
                creator_amount = sum(balances.get(owner, 0) for owner in linked_creator_wallets)

                if previous:
                    previous_balances = json.loads(previous["balances"])
                    previous_total = sum(float(value) for value in previous_balances.values()) or 1
                    previous_holder_count = previous["holder_count"]
                    if previous_holder_count and len(holders) < previous_holder_count:
                        daily = ledger.db.execute(
                            "SELECT holder_count FROM snapshots WHERE mint=? ORDER BY taken_at DESC LIMIT 2", (mint,)
                        ).fetchall()
                        if len(daily) >= 2 and daily[0][0] > daily[1][0]:
                            result.alerts.append(f"${symbol} holder count inverted from growth to {previous_holder_count:,} -> {len(holders):,} - holder expansion has rolled over")
                    cluster_before = previous["cluster_amount"]
                    if cluster_before and cluster_wallets:
                        cut = (1 - (cluster_amount / total) / (cluster_before / previous_total)) * 100
                        if cut >= float(settings.get("intelligence", "watcher_cluster_sell_pct", 5)):
                            result.alerts.append(f"${symbol} registry-cluster share fell {cut:.1f}% since the prior poll - linked wallets began selling")
                    creator_before = previous["creator_amount"]
                    if creator_before and linked_creator_wallets:
                        outflow_pp = creator_before / previous_total * 100 - creator_amount / total * 100
                        if outflow_pp >= float(settings.get("intelligence", "watcher_creator_outflow_pp", .1)):
                            result.alerts.append(f"${symbol} creator-linked supply fell {outflow_pp:.2f}pp since the prior poll - creator-linked outflow is active")

                ledger.record_watcher_sample(
                    mint, now, len(holders), balances,
                    creator_amount if linked_creator_wallets else None,
                    cluster_amount if cluster_wallets else None,
                )
                result.checked += 1
            except Exception as exc:
                log.warning("watcher_token_failed mint=%s error=%s", mint, exc)
                result.errors.append(f"${symbol}: {exc}")
        await deliver_alerts(result.alerts, sender)
        return result
    finally:
        await http.close()


async def run_watcher(
    settings: Settings,
    ledger: Ledger,
    *,
    interval_seconds: int,
    max_cycles: int | None = None,
    sender=send_telegram,
) -> None:
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        result = await poll_once(settings, ledger, sender=sender)
        log.info("watcher_poll checked=%s alerts=%s errors=%s", result.checked, len(result.alerts), len(result.errors))
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        await asyncio.sleep(interval_seconds)
