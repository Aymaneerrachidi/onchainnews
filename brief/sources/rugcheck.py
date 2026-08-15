from __future__ import annotations

from typing import Any

from brief.models import SafetyReport, number
from brief.sources.http import CachedHttpClient


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Tokens sent here are provably unspendable, so they are supply removed from
# circulation rather than a concentrated holder.
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


def _authority_disabled(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return not value
    return str(value).lower() in {"", "none", "null", "false", "disabled", "renounced"}


def parse_report(mint: str, payload: dict[str, Any]) -> SafetyReport:
    token = payload.get("token") or payload.get("tokenMeta") or {}
    markets = payload.get("markets") or []
    holders = payload.get("topHolders") or payload.get("top_holders") or []
    risks = payload.get("risks") or []

    mint_authority = payload.get("mintAuthority", token.get("mintAuthority"))
    freeze_authority = payload.get("freezeAuthority", token.get("freezeAuthority"))
    lp_values: list[float] = []
    for market in markets if isinstance(markets, list) else []:
        lp = market.get("lp") or {}
        locked = number(lp.get("lpLockedPct") or lp.get("lockedPct") or market.get("lpLockedPct"))
        lp_values.append(locked * 100 if 0 < locked <= 1 else locked)
        burned = number(lp.get("lpBurnedPct") or lp.get("burnedPct") or market.get("lpBurnedPct"))
        if burned:
            lp_values.append(burned * 100 if burned <= 1 else burned)

    flag_names = [
        str(risk.get("name") or risk.get("description") or risk.get("level"))
        for risk in risks if isinstance(risk, dict)
    ]
    excluded_accounts: set[str] = set()
    excluded_owners: set[str] = set()
    lp_vaults: list[str] = []
    for market in markets if isinstance(markets, list) else []:
        for side, side_mint in (("A", market.get("mintA")), ("B", market.get("mintB"))):
            if side_mint != mint:
                continue
            account = market.get(f"liquidity{side}")
            details = market.get(f"liquidity{side}Account") or {}
            if account:
                excluded_accounts.add(str(account))
            if details.get("owner"):
                excluded_owners.add(str(details["owner"]))
        for key in ("liquidityA", "liquidityB"):
            if market.get(key):
                lp_vaults.append(str(market[key]))
    known = payload.get("knownAccounts") or {}
    if isinstance(known, dict):
        for address, details in known.items():
            account_type = str((details or {}).get("type", "")).upper()
            # LOCKER holds LP that is already counted as locked, and an AMM
            # vault is the pool itself. Neither is holder concentration.
            if account_type in {"AMM", "DEX", "CEX", "EXCHANGE", "BURN", "LOCKER", "LP"}:
                excluded_accounts.add(str(address))
                excluded_owners.add(str(address))

    # Concentration must be measured on real holders. RugCheck's topHolders
    # list includes the pool vault, lockers and burn addresses, so summing it
    # raw reported established tokens at 70-86% and rejected every one of them.
    top10: float | None = None
    if isinstance(holders, list) and holders:
        circulating = [
            holder for holder in holders
            if isinstance(holder, dict)
            and str(holder.get("address", "")) not in excluded_accounts
            and str(holder.get("owner", "")) not in excluded_owners
            and str(holder.get("address", "")) not in BURN_ADDRESSES
            and str(holder.get("owner", "")) not in BURN_ADDRESSES
        ]
        percentages = [number(h.get("pct") or h.get("percentage")) for h in circulating[:10]]
        # RugCheck has emitted both fractions and percentages across response versions.
        top10 = sum(percentages)
        if top10 <= 1.01:
            top10 *= 100

    return SafetyReport(
        mint=mint,
        mint_authority_renounced=_authority_disabled(mint_authority),
        freeze_authority_disabled=_authority_disabled(freeze_authority),
        lp_locked_or_burned_pct=max(lp_values) if lp_values else None,
        top10_pct=top10,
        risk_flags=flag_names,
        excluded_accounts=excluded_accounts,
        excluded_owners=excluded_owners,
        lp_vaults=tuple(dict.fromkeys(lp_vaults)),
        creator=str(payload.get("creator")) if payload.get("creator") else None,
        holder_count=integer_or_none(payload.get("totalHolders")),
        rugged=bool(payload.get("rugged")),
        source="rugcheck",
    )


class RugCheckSource:
    def __init__(self, http: CachedHttpClient, base_url: str, ttl: int) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl

    async def report(self, mint: str) -> SafetyReport:
        payload = await self.http.get_json(
            f"{self.base_url}/tokens/{mint}/report",
            family="rugcheck", limit=60, ttl=self.ttl,
        )
        return parse_report(mint, payload if isinstance(payload, dict) else {})
