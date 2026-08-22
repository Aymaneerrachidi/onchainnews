from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from brief.config import Settings


log = logging.getLogger("brief.dune")


@dataclass(frozen=True, slots=True)
class DuneAlphaWallet:
    address: str
    name: str
    rank: int
    realised_profit_usd: float
    last_tx: datetime | None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _parse_dune_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace(" UTC", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _clean_wallet(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    return text if _SOLANA_ADDRESS.match(text) else ""


class DuneSource:
    """Small wrapper around public Dune saved-query results.

    The Adam Tehc PumpFun wallet leaderboard is useful as a discovery pool, but
    not as truth. Its top rows can be bot-heavy, so we only turn rows into extra
    wallets for the existing Helius wallet-flow scanner. Dex/RugCheck/Helius
    still decide whether any token is publishable.
    """

    def __init__(self, http, settings: Settings) -> None:
        section = settings.section("dune")
        self.http = http
        self.api_key = os.getenv("DUNE_API_KEY")
        self.enabled = bool(section.get("enabled", False))
        self.base_url = str(section.get("base_url", "https://api.dune.com/api/v1")).rstrip("/")
        self.alpha_query_id = int(section.get("alpha_wallet_query_id", 4032586) or 4032586)
        self.request_limit = int(section.get("requests_per_minute", 12) or 12)
        self.cache_ttl = int(section.get("cache_ttl_seconds", 3600) or 3600)
        self.fetch_limit = int(section.get("alpha_wallet_fetch_limit", 200) or 200)
        self.skip_top_n = int(section.get("alpha_wallet_skip_top_n", 25) or 0)
        self.max_wallets = int(section.get("alpha_wallets_to_add", 50) or 50)
        self.active_days = float(section.get("alpha_wallet_active_days", 30) or 30)
        self.min_profit = float(section.get("alpha_min_profit_usd", 0) or 0)
        self.max_profit = float(section.get("alpha_max_profit_usd", 25_000_000) or 25_000_000)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    async def alpha_wallets(self, now: datetime) -> list[DuneAlphaWallet]:
        if not self.configured:
            return []
        url = f"{self.base_url}/query/{self.alpha_query_id}/results"
        payload = await self.http.get_json(
            url,
            family="dune",
            limit=self.request_limit,
            ttl=self.cache_ttl,
            headers={"X-DUNE-API-KEY": self.api_key or ""},
            params={"limit": self.fetch_limit},
        )
        rows = (((payload or {}).get("result") or {}).get("rows") or [])
        cutoff = now.astimezone(timezone.utc) - timedelta(days=self.active_days)
        wallets: list[DuneAlphaWallet] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            address = _clean_wallet(row.get("wallet"))
            rank = _integer(row.get("rank") or row.get("position"))
            realised = _number(row.get("realized_profit") or row.get("realised_profit"))
            last_tx = _parse_dune_time(row.get("last_tx"))
            if not address or address in seen:
                continue
            if rank and rank <= self.skip_top_n:
                continue
            if last_tx is not None and last_tx < cutoff:
                continue
            if self.min_profit and realised < self.min_profit:
                continue
            if self.max_profit and realised > self.max_profit:
                continue
            seen.add(address)
            wallets.append(DuneAlphaWallet(
                address=address,
                name=f"Dune #{rank or len(wallets) + 1} {address[:4]}...{address[-4:]}",
                rank=rank or len(wallets) + 1,
                realised_profit_usd=realised,
                last_tx=last_tx,
            ))
            if len(wallets) >= self.max_wallets:
                break
        log.info(
            "dune_alpha_wallets query=%s fetched=%s accepted=%s",
            self.alpha_query_id,
            len(rows),
            len(wallets),
        )
        return wallets
