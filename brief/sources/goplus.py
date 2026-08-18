"""Token safety on EVM chains.

RugCheck only covers Solana, so every EVM coin would otherwise reach the report
with its safety layer blank. GoPlus answers the same questions for Ethereum,
BNB Chain and Base, free and without a key: who can still mint, who can pause
transfers, whether the thing can be sold at all, how the supply is spread, and
how many holders it actually has.

It also answers questions Solana does not have. A contract can tax a sale, claw
back ownership after renouncing it, or blacklist a wallet, and each of those is
a way to lose money that no Solana check would ever look for.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from brief.models import SafetyReport, number
from brief.sources.http import CachedHttpClient


log = logging.getLogger("brief.goplus")

# GoPlus keys its endpoint by EVM chain id.
CHAIN_IDS = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "arbitrum": "42161",
    "polygon": "137",
    "avalanche": "43114",
    "optimism": "10",
}

# Addresses a token is sent to in order to be destroyed. Supply parked here is
# gone, so it is neither circulating nor a holder worth counting.
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

# GoPlus only fills `tag` for an address it recognises: a pool, a locker, an
# exchange. A plain whale carries an empty tag, so a tag of any kind is the
# signal that the entry is infrastructure rather than a holder. Matching on
# words instead missed pools labelled with the DEX name, like "Uniswap V3",
# and reported a token's own pool as half its supply.


def supports(chain: str) -> bool:
    return chain.lower() in CHAIN_IDS


def _flag(value: Any) -> bool | None:
    """GoPlus returns "1"/"0" strings, and omits a field it could not decide."""
    if value is None or value == "":
        return None
    return str(value) == "1"


def _percent(value: Any) -> float:
    """Percent fields arrive as a fraction in a string: "0.1147" is 11.47%."""
    return number(value) * 100


def _is_infrastructure(entry: dict[str, Any]) -> bool:
    if str(entry.get("address") or "").lower() in BURN_ADDRESSES:
        return True
    if str(entry.get("is_locked")) == "1":
        return True
    return bool(str(entry.get("tag") or "").strip())


def parse_security(mint: str, payload: dict[str, Any]) -> SafetyReport:
    """Map one GoPlus record onto the same report the Solana path produces."""
    report = SafetyReport(mint=mint, source="goplus")
    if not payload:
        return report

    mintable = _flag(payload.get("is_mintable"))
    report.mint_authority_renounced = None if mintable is None else not mintable

    # The Solana notion of a freeze authority is spread across three EVM powers,
    # any one of which stops a holder selling.
    pausable = _flag(payload.get("transfer_pausable"))
    blacklist = _flag(payload.get("is_blacklisted"))
    cannot_sell = _flag(payload.get("cannot_sell_all"))
    frozen_powers = [p for p in (pausable, blacklist, cannot_sell) if p is not None]
    if frozen_powers:
        report.freeze_authority_disabled = not any(frozen_powers)

    lp_holders = payload.get("lp_holders") or []
    if isinstance(lp_holders, list) and lp_holders:
        locked = sum(
            _percent(entry.get("percent"))
            for entry in lp_holders
            if isinstance(entry, dict)
            and (str(entry.get("is_locked")) == "1" or str(entry.get("address", "")).lower() in BURN_ADDRESSES)
        )
        report.lp_locked_or_burned_pct = locked

    # Concentration on circulating holders only, matching the Solana path: the
    # pool, the locker and the burn address are not whales.
    holders = payload.get("holders") or []
    if isinstance(holders, list) and holders:
        circulating = [
            entry for entry in holders
            if isinstance(entry, dict) and not _is_infrastructure(entry)
        ]
        if circulating:
            report.top10_pct = sum(_percent(e.get("percent")) for e in circulating[:10])

    try:
        report.holder_count = int(payload.get("holder_count"))
    except (TypeError, ValueError):
        report.holder_count = None

    # A honeypot cannot be sold, which is the EVM version of already rugged.
    report.rugged = _flag(payload.get("is_honeypot")) is True

    creator = str(payload.get("creator_address") or "")
    report.creator = creator or None

    buy_tax, sell_tax = _percent(payload.get("buy_tax")), _percent(payload.get("sell_tax"))
    flags: list[str] = []
    if sell_tax >= 10:
        flags.append(f"{sell_tax:.0f}% sell tax")
    if buy_tax >= 10:
        flags.append(f"{buy_tax:.0f}% buy tax")
    if _flag(payload.get("can_take_back_ownership")):
        flags.append("ownership can be taken back after renouncing")
    if _flag(payload.get("hidden_owner")):
        flags.append("hidden owner")
    if _flag(payload.get("selfdestruct")):
        flags.append("contract can self-destruct")
    if _flag(payload.get("transfer_pausable")):
        flags.append("transfers can be paused")
    if _flag(payload.get("is_blacklisted")):
        flags.append("wallets can be blacklisted")
    if _flag(payload.get("is_open_source")) is False:
        flags.append("contract source is not published")
    if _flag(payload.get("is_proxy")):
        flags.append("upgradeable proxy: the code can change")
    report.risk_flags = flags
    return report


class GoPlusSource:
    def __init__(self, http: CachedHttpClient, base_url: str, ttl: int, *, requests_per_minute: int = 30) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl
        self.requests_per_minute = requests_per_minute

    async def reports(self, chain: str, mints: list[str], *, concurrency: int = 4) -> dict[str, SafetyReport]:
        """Security for many contracts on one chain, one request per contract.

        The endpoint takes a comma-separated list and answers HTTP 200 to a
        batch, which reads like it worked. It does not: the free tier returns a
        single record however many addresses are sent, so a batch of twenty
        silently arrived as one report and nineteen coins went out unverified.
        """
        chain_id = CHAIN_IDS.get(chain.lower())
        if not chain_id or not mints:
            return {}
        semaphore = asyncio.Semaphore(max(1, concurrency))
        found: dict[str, SafetyReport] = {}

        async def one(mint: str) -> None:
            async with semaphore:
                try:
                    payload = await self.http.get_json(
                        f"{self.base_url}/api/v1/token_security/{chain_id}",
                        family="goplus",
                        limit=self.requests_per_minute,
                        ttl=self.ttl,
                        params={"contract_addresses": mint},
                    )
                except Exception as exc:
                    log.warning("goplus_failed chain=%s mint=%s error=%s", chain, mint[:10], exc)
                    return
            result = (payload or {}).get("result") or {}
            if not isinstance(result, dict):
                return
            # GoPlus echoes the address lowercased.
            for address, record in result.items():
                if isinstance(record, dict) and str(address).lower() == mint.lower():
                    found[mint] = parse_security(mint, record)

        await asyncio.gather(*(one(m) for m in dict.fromkeys(m for m in mints if m)))
        log.info("goplus chain=%s requested=%s answered=%s", chain, len(mints), len(found))
        return found
