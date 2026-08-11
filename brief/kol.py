"""What the tracked wallets actually did, and which coins they made money on.

Two questions this answers:

1. Which coins did many of these wallets buy? A coin several independent
   profitable traders bought inside the window is the crowd forming before the
   chart shows it.
2. Which coins did they take profit on? Realised SOL is reconstructed per mint
   from the wallet's own balance deltas, so "they made money here" is measured
   rather than assumed.

Buys and sells are read from balance deltas rather than by classifying swap
instructions, which keeps the arithmetic correct across every DEX, aggregator
and bot router without needing to know any of them.

The wallet list lives in `[kol].wallets`. With no wallets configured this module
does nothing and costs nothing.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from brief.config import Settings


log = logging.getLogger("brief.kol")

LAMPORTS = 1_000_000_000
# Wrapped SOL moves through the token balances on most routes; counting it as a
# position would double-count the SOL leg of every swap.
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
IGNORED_MINTS = {WSOL, *STABLES}


def configured_wallets(settings: Settings) -> dict[str, str]:
    """Map of address to display label.

    Accepts plain address strings or ``{address, name}`` tables so a leaderboard
    can be pasted in quickly and named later.
    """
    raw = settings.get("kol", "wallets", []) or []
    ranked: list[tuple[float, str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            address, name, pnl = entry.strip(), "", 0.0
        elif isinstance(entry, dict):
            address = str(entry.get("address", "")).strip()
            name = str(entry.get("name") or "")
            pnl = float(entry.get("pnl_sol") or 0)
        else:
            continue
        if not address or address in seen:
            continue
        seen.add(address)
        ranked.append((pnl, address, name or f"{address[:4]}...{address[-4:]}"))
    # Best-performing wallets first: a rate-limited partial scan should still
    # carry the strongest signal rather than an arbitrary slice of the list.
    ranked.sort(key=lambda row: row[0], reverse=True)
    return {address: name for _, address, name in ranked}


@dataclass
class MintFlow:
    """One wallet's activity in one mint over the window."""

    sol_spent: float = 0.0
    sol_received: float = 0.0
    tokens_in: float = 0.0
    tokens_out: float = 0.0
    first_buy_at: datetime | None = None

    @property
    def realised_sol(self) -> float:
        return self.sol_received - self.sol_spent

    @property
    def still_holding(self) -> bool:
        # Anything under a rounding crumb of the position counts as closed.
        return self.tokens_in - self.tokens_out > max(1e-9, self.tokens_in * 0.02)


@dataclass
class MintActivity:
    """Every tracked wallet's activity in one mint."""

    mint: str
    buyers: list[str] = field(default_factory=list)
    sellers: list[str] = field(default_factory=list)
    realised_sol: float = 0.0
    sol_spent: float = 0.0
    holders: list[str] = field(default_factory=list)
    first_buy_at: datetime | None = None

    @property
    def winners(self) -> int:
        return len(self.sellers)

    @property
    def participants(self) -> int:
        """Wallets that touched this mint at all.

        A wallet that opened its position before the window and closed it inside
        the window has realised profit but no buy to count, so buyers alone
        under-reports who actually traded it.
        """
        return len(set(self.buyers) | set(self.sellers))


def _sol_delta(transaction: dict[str, Any], owner: str) -> float:
    """The wallet's own native SOL change, fee excluded, in SOL."""
    meta = transaction.get("meta") or {}
    message = (transaction.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    index = None
    for position, key in enumerate(keys):
        address = key.get("pubkey") if isinstance(key, dict) else key
        if str(address) == owner:
            index = position
            break
    if index is None:
        return 0.0
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if index >= len(pre) or index >= len(post):
        return 0.0
    delta = (post[index] - pre[index]) / LAMPORTS
    if index == 0:
        # The fee payer's balance also drops by the fee, which is not a trade.
        delta += (meta.get("fee") or 0) / LAMPORTS
    return delta


def _token_deltas(transaction: dict[str, Any], owner: str) -> dict[str, float]:
    """Per-mint token balance change for this wallet, in UI units."""
    meta = transaction.get("meta") or {}
    before: dict[str, float] = defaultdict(float)
    after: dict[str, float] = defaultdict(float)
    for entry in meta.get("preTokenBalances") or []:
        if str(entry.get("owner")) == owner:
            before[str(entry.get("mint"))] += float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    for entry in meta.get("postTokenBalances") or []:
        if str(entry.get("owner")) == owner:
            after[str(entry.get("mint"))] += float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    deltas: dict[str, float] = {}
    for mint in set(before) | set(after):
        if mint in IGNORED_MINTS:
            continue
        change = after[mint] - before[mint]
        if change:
            deltas[mint] = change
    return deltas


def _mints_bought(transaction: dict[str, Any], owner: str) -> set[str]:
    """Mints this wallet gained in one transaction. Failed transactions buy nothing."""
    if (transaction.get("meta") or {}).get("err"):
        return set()
    return {mint for mint, change in _token_deltas(transaction, owner).items() if change > 0}


def apply_transaction(flows: dict[str, MintFlow], transaction: dict[str, Any], owner: str) -> None:
    """Attribute one transaction's SOL movement to the mints it traded.

    When a transaction touches several mints the SOL is split across them by
    share of absolute token movement, which keeps multi-hop routes from
    assigning a whole swap's cost to one leg.
    """
    if (transaction.get("meta") or {}).get("err"):
        return
    deltas = _token_deltas(transaction, owner)
    if not deltas:
        return
    sol = _sol_delta(transaction, owner)
    stamp = transaction.get("blockTime")
    when = datetime.fromtimestamp(float(stamp), tz=timezone.utc) if stamp else None
    total_movement = sum(abs(change) for change in deltas.values()) or 1.0

    for mint, change in deltas.items():
        flow = flows.setdefault(mint, MintFlow())
        share = abs(change) / total_movement
        attributed = sol * share
        if change > 0:
            flow.tokens_in += change
            if attributed < 0:
                flow.sol_spent += -attributed
            if when and (flow.first_buy_at is None or when < flow.first_buy_at):
                flow.first_buy_at = when
        else:
            flow.tokens_out += -change
            if attributed > 0:
                flow.sol_received += attributed


class KolTracker:
    def __init__(self, helius, settings: Settings) -> None:
        self.helius = helius
        self.settings = settings
        self.wallets = configured_wallets(settings)
        section = settings.section("kol")
        self.max_transactions = int(section.get("max_transactions_per_wallet", 40))
        self.concurrency = int(section.get("concurrency", 2))
        self.window_hours = float(section.get("window_hours", 24))
        # Helius free tier answers a burst of heavy wallet-history calls with
        # 429s, so this work is paced and capped independently of the rest.
        self.request_interval = float(section.get("request_interval_seconds", 1.0))
        self.requests_per_minute = int(section.get("requests_per_minute", 30))
        max_wallets = int(section.get("max_wallets_per_run", 0))
        if max_wallets > 0:
            self.wallets = dict(list(self.wallets.items())[:max_wallets])
        self.scanned = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.wallets) and bool(self.settings.get("kol", "enabled", True))

    async def activity(self, now: datetime) -> dict[str, MintActivity]:
        """Per-mint aggregate of what every tracked wallet did in the window."""
        if not self.enabled or not getattr(self.helius, "configured", False):
            return {}
        cutoff = now.astimezone(timezone.utc) - timedelta(hours=self.window_hours)
        semaphore = asyncio.Semaphore(max(1, self.concurrency))
        per_wallet: dict[str, dict[str, MintFlow]] = {}

        async def scan(index: int, address: str, label: str) -> None:
            async with semaphore:
                if self.request_interval:
                    await asyncio.sleep(self.request_interval * (index % max(1, self.concurrency) + 1))
                try:
                    transactions = await self.helius.wallet_transactions(
                        address,
                        limit=self.max_transactions,
                        requests_per_minute=self.requests_per_minute,
                    )
                except Exception as exc:
                    self.failed += 1
                    log.warning("kol_scan_failed wallet=%s error=%s", label, exc)
                    return
            flows: dict[str, MintFlow] = {}
            for transaction in transactions:
                stamp = transaction.get("blockTime")
                if stamp and datetime.fromtimestamp(float(stamp), tz=timezone.utc) < cutoff:
                    # Newest first, so the first old transaction ends this wallet.
                    break
                apply_transaction(flows, transaction, address)
            if flows:
                per_wallet[label] = flows
            self.scanned += 1

        await asyncio.gather(
            *(scan(index, address, label)
              for index, (address, label) in enumerate(self.wallets.items()))
        )

        activity: dict[str, MintActivity] = {}
        for label, flows in per_wallet.items():
            for mint, flow in flows.items():
                record = activity.setdefault(mint, MintActivity(mint=mint))
                if flow.tokens_in > 0:
                    record.buyers.append(label)
                    record.sol_spent += flow.sol_spent
                    if flow.first_buy_at and (
                        record.first_buy_at is None or flow.first_buy_at < record.first_buy_at
                    ):
                        record.first_buy_at = flow.first_buy_at
                if flow.tokens_out > 0:
                    record.sellers.append(label)
                record.realised_sol += flow.realised_sol
                if flow.still_holding:
                    record.holders.append(label)
        for record in activity.values():
            record.buyers.sort()
            record.sellers.sort()
            record.holders.sort()
        log.info(
            "kol_scan wallets=%s scanned=%s failed=%s mints=%s",
            len(self.wallets), self.scanned, self.failed, len(activity),
        )
        return activity

    async def buyers_by_mint(self, now: datetime) -> dict[str, list[str]]:
        return {mint: record.buyers for mint, record in (await self.activity(now)).items()}
