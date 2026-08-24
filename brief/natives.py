"""The established memes, watched every run and named only when they move.

The recap is built around coins that ran today, which by construction skips the
names the audience already holds. A reader who owns $PEPE wants to know the day
it moved 40%, and that day never shows up in a discovery pipeline tuned for new
market caps.

So this is a fixed list rather than a search. Two reasons it has to be fixed:
these coins do not trend on GMGN, because trending measures fresh activity and
a four-year-old meme is not fresh; and a ticker search cannot resolve them
safely. Searching "CATE" returns a spoof holding a right-to-left override in
its name and a pool with sixty million dollars of stated liquidity and no
trades. Every entry here is a contract address that was checked against GMGN
before it was written down.

Nothing is published for merely existing. A name earns its line by moving,
and the two directions are not symmetrical: a 30% day up is a rally worth
telling, while a 30% day down on an established coin is ordinary chop. A drop
has to reach 50% before it means the same thing a rally means at 30%.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from brief.config import Settings

log = logging.getLogger("brief.natives")

# Dexscreener takes up to 30 comma-separated addresses per request.
BATCH = 30


@dataclass(slots=True)
class NativeMove:
    """One established meme that moved enough to be worth a line."""
    symbol: str
    chain: str
    mint: str
    market_cap: float
    change_24h: float
    volume_24h: float = 0.0
    url: str = ""

    @property
    def direction(self) -> str:
        return "up" if self.change_24h >= 0 else "down"


def watchlist(settings: Settings) -> list[dict[str, str]]:
    """The configured names, deduplicated by contract."""
    rows = settings.get("natives", "tokens", []) or []
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mint = str(row.get("mint") or "").strip()
        chain = str(row.get("chain") or "").strip().lower()
        if not mint or not chain or mint.lower() in seen:
            continue
        seen.add(mint.lower())
        out.append({"symbol": str(row.get("symbol") or "?"), "chain": chain, "mint": mint})
    return out


def _pairs_for(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        return [p for p in (payload.get("pairs") or []) if isinstance(p, dict)]
    return []


def _deepest(pairs: list[dict[str, Any]], mint: str) -> dict[str, Any] | None:
    """The real market for this contract, not the first one returned.

    A major trades in many pools and some of them are stale or single-sided.
    Depth is the only honest tiebreak; picking by order returns whichever pool
    the API felt like listing.
    """
    owned = [
        p for p in pairs
        if str((p.get("baseToken") or {}).get("address") or "").lower() == mint.lower()
    ]
    if not owned:
        return None
    return max(owned, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def qualifies(change: float, min_gain: float, min_drop: float) -> bool:
    """A rally at `min_gain`, or a collapse at `min_drop`. Never symmetrical."""
    return change >= min_gain or change <= -abs(min_drop)


def movers_from_pairs(
    entries: list[dict[str, str]],
    pairs_by_chain: dict[str, list[dict[str, Any]]],
    min_gain_pct: float,
    min_drop_pct: float | None = None,
    min_market_cap: float = 0.0,
) -> list[NativeMove]:
    """Keep only the names that actually moved. Pure, so the rule is testable."""
    if min_drop_pct is None:
        min_drop_pct = min_gain_pct
    moves: list[NativeMove] = []
    for entry in entries:
        pair = _deepest(pairs_by_chain.get(entry["chain"], []), entry["mint"])
        if pair is None:
            continue
        change = (pair.get("priceChange") or {}).get("h24")
        if change is None:
            # No reported change is not a flat day; it is an unanswered
            # question, and a silent zero here would quietly drop the name.
            continue
        try:
            change = float(change)
        except (TypeError, ValueError):
            continue
        if not qualifies(change, min_gain_pct, min_drop_pct):
            continue
        market_cap = float(pair.get("marketCap") or 0)
        # A big move on a small survivor is not a major-meme story. The floor is
        # on market cap rather than on the move, because the move is already
        # real; it is the name that stopped being one people watch.
        if min_market_cap and market_cap < min_market_cap:
            continue
        moves.append(NativeMove(
            symbol=str((pair.get("baseToken") or {}).get("symbol") or entry["symbol"]),
            chain=entry["chain"],
            mint=entry["mint"],
            market_cap=market_cap,
            change_24h=change,
            volume_24h=float((pair.get("volume") or {}).get("h24") or 0),
            url=str(pair.get("url") or ""),
        ))
    moves.sort(key=lambda m: abs(m.change_24h), reverse=True)
    return moves


async def check_natives(settings: Settings, http) -> list[NativeMove]:
    """Price the watchlist and return only what moved. Never raises."""
    if not bool(settings.get("natives", "enabled", False)):
        return []
    entries = watchlist(settings)
    if not entries:
        return []
    min_gain = float(settings.get("natives", "min_gain_pct", 30.0) or 30.0)
    min_drop = float(settings.get("natives", "min_drop_pct", 50.0) or 50.0)
    min_cap = float(settings.get("natives", "min_market_cap", 0.0) or 0.0)

    by_chain: dict[str, list[str]] = {}
    for entry in entries:
        by_chain.setdefault(entry["chain"], []).append(entry["mint"])

    pairs_by_chain: dict[str, list[dict[str, Any]]] = {}
    for chain, mints in by_chain.items():
        collected: list[dict[str, Any]] = []
        for start in range(0, len(mints), BATCH):
            chunk = ",".join(mints[start:start + BATCH])
            try:
                payload = await http.get_json(
                    f"https://api.dexscreener.com/tokens/v1/{chain}/{chunk}",
                    family="dex-pairs",
                )
            except Exception as exc:  # a watchlist must not cost the report
                log.warning("natives_chain_failed chain=%s error=%s", chain, exc)
                continue
            collected.extend(_pairs_for(payload))
        pairs_by_chain[chain] = collected

    moves = movers_from_pairs(entries, pairs_by_chain, min_gain, min_drop, min_cap)
    log.info(
        "natives_checked watched=%s moved=%s gain=%.0f%% drop=-%.0f%% min_mcap=%.0f",
        len(entries), len(moves), min_gain, abs(min_drop), min_cap,
    )
    return moves
