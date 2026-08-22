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
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from brief.config import Settings
from brief.models import KolWalletFlow


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
    flows: list[KolWalletFlow] = field(default_factory=list)
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
    def __init__(self, helius, settings: Settings, extra_wallets: dict[str, str] | None = None) -> None:
        self.helius = helius
        self.settings = settings
        self.wallets = configured_wallets(settings)
        for address, label in (extra_wallets or {}).items():
            if address and address not in self.wallets:
                self.wallets[address] = label or f"{address[:4]}...{address[-4:]}"
        section = settings.section("kol")
        self.page_size = int(
            section.get("transactions_page_size", section.get("max_transactions_per_wallet", 100))
        )
        self.max_pages = int(section.get("max_transaction_pages_per_wallet", 100) or 100)
        self.concurrency = int(section.get("concurrency", 2))
        self.window_hours = float(section.get("window_hours", 24))
        # Helius free tier answers a burst of heavy wallet-history calls with
        # 429s, so this work is paced and capped independently of the rest.
        self.request_interval = float(section.get("request_interval_seconds", 1.0))
        self.requests_per_minute = int(section.get("requests_per_minute", 30))
        # A positive token balance delta alone is not a buy. Meme deployers
        # routinely airdrop dust to visible wallets so dashboards can claim
        # fake KOL participation. Require an actual SOL leg before a wallet is
        # counted as a buyer or seller.
        self.min_trade_sol = float(section.get("min_trade_sol", 0.05) or 0.05)
        max_wallets = int(section.get("max_wallets_per_run", 0))
        if max_wallets > 0:
            self.wallets = dict(list(self.wallets.items())[:max_wallets])
        self.cache_ttl_seconds = int(section.get("cache_ttl_seconds", 0) or 0)
        raw_cache_path = str(section.get("cache_path", "") or "").strip()
        self.cache_path: Path | None = None
        if raw_cache_path:
            path = Path(raw_cache_path)
            self.cache_path = path if path.is_absolute() else settings.root / path
        self.scanned = 0
        self.failed = 0
        self.pages_scanned = 0
        self.transactions_scanned = 0

    @property
    def enabled(self) -> bool:
        return bool(self.wallets) and bool(self.settings.get("kol", "enabled", True))

    async def activity(self, now: datetime) -> dict[str, MintActivity]:
        """Per-mint aggregate of what every tracked wallet did in the window."""
        if not self.enabled or not getattr(self.helius, "configured", False):
            return {}
        cached = self._read_cache(now)
        if cached is not None:
            self.scanned = len(self.wallets)
            log.info(
                "kol_scan_cache_hit wallets=%s mints=%s path=%s",
                len(self.wallets), len(cached), self.cache_path,
            )
            return cached
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
                        limit=self.page_size,
                        max_pages=self.max_pages,
                        since_unix=int(cutoff.timestamp()),
                        requests_per_minute=self.requests_per_minute,
                    )
                except Exception as exc:
                    self.failed += 1
                    log.warning("kol_scan_failed wallet=%s error=%s", label, exc)
                    return
            self.pages_scanned += int(
                getattr(self.helius, "wallet_history_pages", {}).get(address, 0) or 0
            )
            self.transactions_scanned += len(transactions)
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
                bought = flow.tokens_in > 0 and flow.sol_spent >= self.min_trade_sol
                sold = flow.tokens_out > 0 and flow.sol_received >= self.min_trade_sol
                if not bought and not sold:
                    continue
                record = activity.setdefault(mint, MintActivity(mint=mint))
                if bought:
                    record.buyers.append(label)
                    record.sol_spent += flow.sol_spent
                    if flow.first_buy_at and (
                        record.first_buy_at is None or flow.first_buy_at < record.first_buy_at
                    ):
                        record.first_buy_at = flow.first_buy_at
                if sold:
                    record.sellers.append(label)
                # A position opened in the window has negative realised cash
                # flow until it is sold. This field is net realised flow for
                # the observed window, not mark-to-market PnL.
                record.realised_sol += (
                    (flow.sol_received if sold else 0.0)
                    - (flow.sol_spent if bought else 0.0)
                )
                if bought and flow.still_holding:
                    record.holders.append(label)
                if bought or sold:
                    record.flows.append(KolWalletFlow(
                        name=label,
                        bought=bought,
                        sold=sold,
                        holding=bought and flow.still_holding,
                        realised_sol=(flow.sol_received if sold else 0.0) - (flow.sol_spent if bought else 0.0),
                        sol_spent=flow.sol_spent if bought else 0.0,
                    ))
        for record in activity.values():
            record.buyers.sort()
            record.sellers.sort()
            record.holders.sort()
            record.flows.sort(
                key=lambda flow: (
                    flow.realised_sol,
                    flow.holding,
                    flow.bought,
                    flow.name.lower(),
                ),
                reverse=True,
            )
        log.info(
            "kol_scan wallets=%s scanned=%s failed=%s pages=%s transactions=%s mints=%s",
            len(self.wallets), self.scanned, self.failed, self.pages_scanned,
            self.transactions_scanned, len(activity),
        )
        self._write_cache(now, activity)
        return activity

    async def buyers_by_mint(self, now: datetime) -> dict[str, list[str]]:
        return {mint: record.buyers for mint, record in (await self.activity(now)).items()}

    def _cache_key(self) -> str:
        payload = {
            "wallets": list(self.wallets.items()),
            "window_hours": self.window_hours,
            "page_size": self.page_size,
            "max_pages": self.max_pages,
            "min_trade_sol": self.min_trade_sol,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _read_cache(self, now: datetime) -> dict[str, MintActivity] | None:
        if not self.cache_path or self.cache_ttl_seconds <= 0 or not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("key") != self._cache_key():
                return None
            created_raw = str(data.get("created_at") or "")
            created = datetime.fromisoformat(created_raw)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = now.astimezone(timezone.utc) - created.astimezone(timezone.utc)
            if age.total_seconds() < 0 or age.total_seconds() > self.cache_ttl_seconds:
                return None
            self.pages_scanned = int(data.get("pages_scanned") or 0)
            self.transactions_scanned = int(data.get("transactions_scanned") or 0)
            return {
                mint: self._record_from_json(mint, raw)
                for mint, raw in (data.get("activity") or {}).items()
                if isinstance(raw, dict)
            }
        except Exception as exc:
            log.warning("kol_cache_read_failed path=%s error=%s", self.cache_path, exc)
            return None

    def _write_cache(self, now: datetime, activity: dict[str, MintActivity]) -> None:
        if not self.cache_path or self.cache_ttl_seconds <= 0:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "created_at": now.astimezone(timezone.utc).isoformat(),
                "key": self._cache_key(),
                "wallet_count": len(self.wallets),
                "pages_scanned": self.pages_scanned,
                "transactions_scanned": self.transactions_scanned,
                "activity": {
                    mint: self._record_to_json(record)
                    for mint, record in activity.items()
                },
            }
            self.cache_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        except Exception as exc:
            log.warning("kol_cache_write_failed path=%s error=%s", self.cache_path, exc)

    @staticmethod
    def _record_to_json(record: MintActivity) -> dict[str, Any]:
        return {
            "buyers": record.buyers,
            "sellers": record.sellers,
            "holders": record.holders,
            "realised_sol": record.realised_sol,
            "sol_spent": record.sol_spent,
            "first_buy_at": record.first_buy_at.isoformat() if record.first_buy_at else None,
            "flows": [
                {
                    "name": flow.name,
                    "bought": flow.bought,
                    "sold": flow.sold,
                    "holding": flow.holding,
                    "realised_sol": flow.realised_sol,
                    "sol_spent": flow.sol_spent,
                }
                for flow in record.flows
            ],
        }

    @staticmethod
    def _record_from_json(mint: str, raw: dict[str, Any]) -> MintActivity:
        first_buy_at = None
        if raw.get("first_buy_at"):
            try:
                first_buy_at = datetime.fromisoformat(str(raw["first_buy_at"]))
            except ValueError:
                first_buy_at = None
        return MintActivity(
            mint=mint,
            buyers=[str(item) for item in raw.get("buyers", [])],
            sellers=[str(item) for item in raw.get("sellers", [])],
            holders=[str(item) for item in raw.get("holders", [])],
            realised_sol=float(raw.get("realised_sol") or 0.0),
            sol_spent=float(raw.get("sol_spent") or 0.0),
            first_buy_at=first_buy_at,
            flows=[
                KolWalletFlow(
                    name=str(flow.get("name") or ""),
                    bought=bool(flow.get("bought")),
                    sold=bool(flow.get("sold")),
                    holding=bool(flow.get("holding")),
                    realised_sol=float(flow.get("realised_sol") or 0.0),
                    sol_spent=float(flow.get("sol_spent") or 0.0),
                )
                for flow in raw.get("flows", [])
                if isinstance(flow, dict)
            ],
        )
