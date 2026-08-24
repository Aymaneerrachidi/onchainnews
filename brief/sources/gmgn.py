from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from brief.models import SafetyReport, SourceStatus, TokenSnapshot, TransactionWindow, integer, number


SOL_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
CHAIN_ALIASES = {
    "sol": "solana",
    "solana": "solana",
    "bsc": "bsc",
    "base": "base",
    "eth": "ethereum",
    "ethereum": "ethereum",
}
CLI_CHAINS = {
    "solana": "sol",
    "bsc": "bsc",
    "base": "base",
    "ethereum": "eth",
    # Robinhood's L2 opened in July and memecoins took it: they are ~79% of
    # its DEX volume, so a meme recap that skips it has a hole in it.
    "robinhood": "robinhood",
}


class GmgnError(RuntimeError):
    pass


@dataclass(slots=True)
class GmgnDiscovery:
    tokens: list[TokenSnapshot] = field(default_factory=list)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    wallet_events: list[dict[str, Any]] = field(default_factory=list)
    wallet_flow_available: bool = False
    wallet_flow_chains: set[str] = field(default_factory=set)
    statuses: list[SourceStatus] = field(default_factory=list)


def _dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _timestamp(value: Any) -> datetime | None:
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp > 0 else None
    except (TypeError, ValueError, OSError):
        return None


def _ratio_pct(value: Any) -> float | None:
    if value in (None, ""):
        return None
    parsed = number(value)
    return parsed * 100.0 if abs(parsed) <= 1.0 else parsed


def _normal_chain(value: Any) -> str:
    return CHAIN_ALIASES.get(str(value or "").strip().lower(), str(value or "").strip().lower())


def _valid_token_address(address: str, chain: str) -> bool:
    return bool(SOL_ADDRESS.fullmatch(address)) if chain == "solana" else bool(EVM_ADDRESS.fullmatch(address))


def parse_rank_item(
    item: dict[str, Any], *, origin: str, chain: str | None = None,
) -> TokenSnapshot | None:
    mint = str(item.get("address") or item.get("token_address") or "")
    chain_id = _normal_chain(chain or item.get("chain") or item.get("chain_id") or "solana")
    if chain_id not in CLI_CHAINS or not _valid_token_address(mint, chain_id):
        return None
    market_cap = number(item.get("market_cap") or item.get("usd_market_cap"))
    if market_cap <= 0:
        return None
    buys = integer(item.get("buys") or item.get("buys_24h"))
    sells = integer(item.get("sells") or item.get("sells_24h"))
    swaps = integer(item.get("swaps") or item.get("swaps_24h"))
    if not buys and not sells and swaps:
        buys = swaps // 2
        sells = swaps - buys
    volume = number(item.get("volume") or item.get("volume_24h"))
    pool_address = str(item.get("pool_address") or item.get("biggest_pool_address") or "")
    raw = {"gmgn": item, "gmgnOrigin": origin}
    return TokenSnapshot(
        mint=mint,
        symbol=str(item.get("symbol") or "?").upper(),
        name=str(item.get("name") or item.get("symbol") or mint[:8]),
        chain_id=chain_id,
        pair_address=pool_address,
        # Every renderer labels this field as the chart link. A GMGN discovery
        # row used to put a GMGN URL behind “Open Dexscreener”, and the payload
        # then added a second, hard-coded Solana GMGN link even for EVM coins.
        url=f"https://dexscreener.com/{chain_id}/{pool_address or mint}",
        price_usd=number(item.get("price")),
        market_cap=market_cap,
        liquidity_usd=number(item.get("liquidity")),
        volume_24h=volume,
        volume_6h=number(item.get("volume_6h")),
        volume_1h=number(item.get("volume_1h")),
        intraday_known="volume_1h" in item or "swaps_1h" in item,
        price_change_24h=number(item.get("price_change_percent") or item.get("price_change_percent24h")),
        price_change_6h=number(item.get("price_change_percent6h")),
        price_change_1h=number(item.get("price_change_percent1h")),
        price_change_5m=number(item.get("price_change_percent5m")),
        pair_created_at=_timestamp(item.get("creation_timestamp") or item.get("created_timestamp") or item.get("open_timestamp")),
        dex_id=str(item.get("exchange") or item.get("launchpad_platform") or "gmgn").lower(),
        txns_24h=TransactionWindow(buys=buys, sells=sells),
        txns_6h=TransactionWindow(
            buys=integer(item.get("buys_6h")), sells=integer(item.get("sells_6h"))
        ),
        txns_1h=TransactionWindow(
            buys=integer(item.get("buys_1h")), sells=integer(item.get("sells_1h"))
        ),
        socials=[
            {"type": kind, "url": str(value)}
            for kind, value in (
                ("twitter", item.get("twitter") or item.get("twitter_username")),
                ("telegram", item.get("telegram")),
                ("website", item.get("website")),
            )
            if value
        ],
        active_boosts=integer(item.get("dexscr_boost_fee") or item.get("dexscr_trending_bar")),
        raw=raw,
    )


def _pct_of_ratio(value: Any) -> float | None:
    """GMGN reports rates as fractions; the rest of the app reads percents."""
    if value is None or value == "":
        return None
    try:
        return float(value) * 100.0
    except (TypeError, ValueError):
        return None


def tax_rate_pct(value: Any) -> float | None:
    """Normalise GMGN's explicit buy/sell-tax fields to percentage points.

    Rank responses normally encode ``0.03`` for 3%, while a few token-detail
    responses already return ``3``.  These fields describe transfer tax.  The
    separate ``total_fee`` / ``trade_fee`` fields describe market activity and
    must never be used as a proxy for contract tax.
    """
    if value is None or value == "":
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate < 0:
        return None
    return rate * 100.0 if rate <= 1 else rate


def transfer_tax_pct(evidence: dict[str, Any]) -> float | None:
    """Return the highest explicit buy/sell tax, or ``None`` if unanswered."""
    rates = [tax_rate_pct(evidence.get(key)) for key in ("buyTax", "sellTax")]
    known = [rate for rate in rates if rate is not None]
    return max(known) if known else None


def safety_from_evidence(mint: str, evidence: dict[str, Any]) -> SafetyReport:
    """A contract-safety report built from what GMGN already told us.

    GoPlus covers seven EVM chains and Robinhood is not one of them, so a
    Robinhood runner would otherwise reach the recap labelled unverified — a
    ticker in front of a reader with nothing behind it. GMGN returns the same
    facts that matter for naming a coin: whether it can be sold, what it taxes,
    how concentrated it is, and whether the authorities are gone. This is a
    narrower report than GoPlus gives, and it says so in `source`.
    """
    report = SafetyReport(mint, source="gmgn")
    report.holder_count = integer(evidence.get("holders")) or None
    report.top10_pct = number(evidence.get("top10Pct")) if evidence.get("top10Pct") is not None else None
    report.creator = str(evidence.get("creator") or "") or None

    honeypot = evidence.get("isHoneypot")
    # GMGN answers "no"/"yes"; anything else is an absent answer, not a pass.
    report.rugged = str(honeypot).strip().lower() in {"yes", "1", "true"}

    renounced_mint = evidence.get("renouncedMint")
    renounced_freeze = evidence.get("renouncedFreeze")
    report.mint_authority_renounced = None if renounced_mint is None else bool(integer(renounced_mint))
    report.freeze_authority_disabled = None if renounced_freeze is None else bool(integer(renounced_freeze))

    flags: list[str] = []
    if report.rugged:
        flags.append("honeypot: the token cannot be sold")
    for label, key in (("sell", "sellTax"), ("buy", "buyTax")):
        pct = tax_rate_pct(evidence.get(key))
        if pct is None:
            continue
        if pct >= 5:
            flags.append(f"{pct:.0f}% {label} tax")
    if evidence.get("washTrading") is True:
        flags.append("wash-trading shape")
    insider = _pct_of_ratio(evidence.get("insiderRate"))
    if insider is not None and insider >= 10:
        flags.append(f"{insider:.0f}% held by suspected insiders")
    dev_hold = _pct_of_ratio(evidence.get("devTeamHoldRate"))
    if dev_hold is not None and dev_hold >= 10:
        flags.append(f"dev team holds {dev_hold:.0f}%")
    report.risk_flags = flags
    return report


def evidence_from_rank(item: dict[str, Any], origin: str) -> dict[str, Any]:
    return {
        "origin": origin,
        "athMarketCap": number(item.get("history_highest_market_cap")) or None,
        # A tax on every transfer. Material enough to change whether a coin is
        # worth touching, and invisible in price data.
        "totalFee": number(item.get("total_fee")) if item.get("total_fee") is not None else None,
        "burnStatus": str(item.get("burn_status") or "") or None,
        "burnRatio": number(item.get("burn_ratio")) if item.get("burn_ratio") is not None else None,
        "tradeFee": number(item.get("trade_fee")) if item.get("trade_fee") is not None else None,
        "holders": integer(item.get("holder_count")) or None,
        "top10Pct": _ratio_pct(item.get("top_10_holder_rate")),
        "rugRatio": number(item.get("rug_ratio")) if item.get("rug_ratio") not in (None, "") else None,
        "washTrading": item.get("is_wash_trading"),
        # GoPlus does not cover every chain we now read. Where it cannot, these
        # are the honeypot and tax facts a reader is entitled to before a
        # ticker is put in front of them.
        "isHoneypot": item.get("is_honeypot"),
        "buyTax": number(item.get("buy_tax")) if item.get("buy_tax") is not None else None,
        "sellTax": number(item.get("sell_tax")) if item.get("sell_tax") is not None else None,
        "renouncedMint": item.get("renounced_mint"),
        "renouncedFreeze": item.get("renounced_freeze_account"),
        "insiderRate": number(item.get("rat_trader_amount_rate") or item.get("suspected_insider_hold_rate")) if (item.get("rat_trader_amount_rate") is not None or item.get("suspected_insider_hold_rate") is not None) else None,
        "bundlerRate": number(item.get("bundler_rate") or item.get("bundler_trader_amount_rate")) if (item.get("bundler_rate") is not None or item.get("bundler_trader_amount_rate") is not None) else None,
        "sniperCount": integer(item.get("sniper_count")) if item.get("sniper_count") is not None else None,
        "freshWalletRate": number(item.get("fresh_wallet_rate")) if item.get("fresh_wallet_rate") is not None else None,
        "devTeamHoldRate": number(item.get("dev_team_hold_rate")) if item.get("dev_team_hold_rate") is not None else None,
        "creatorTokenStatus": str(item.get("creator_token_status") or "") or None,
        "entrapmentRatio": number(item.get("entrapment_ratio")) if item.get("entrapment_ratio") is not None else None,
        "top70SniperHoldRate": number(item.get("top70_sniper_hold_rate")) if item.get("top70_sniper_hold_rate") is not None else None,
        "botDegenRate": number(item.get("bot_degen_rate")) if item.get("bot_degen_rate") is not None else None,
        "bluechipOwnerPct": _ratio_pct(item.get("bluechip_owner_percentage")),
        "burnStatus": str(item.get("burn_status") or "") or None,
        "smartMoneyCount": integer(item.get("smart_degen_count")),
        "kolCount": integer(item.get("renowned_count")),
        "hotLevel": number(item.get("hot_level")),
        "searchHeat": number(item.get("visiting_count")),
        "cto": bool(integer(item.get("cto_flag"))),
        "promotion": {
            "ad": bool(integer(item.get("dexscr_ad"))),
            "boostFee": number(item.get("dexscr_boost_fee")),
            "trendingBar": bool(integer(item.get("dexscr_trending_bar"))),
        },
    }


class GmgnSource:
    """Read-only wrapper around the official gmgn-cli executable."""

    def __init__(
        self, *, timeout: float = 30.0, ledger: Any | None = None,
        cache_ttl: int = 900, min_interval_seconds: float = 1.25,
        chains: Iterable[str] = ("solana",),
    ) -> None:
        self.executable = shutil.which("gmgn-cli") or shutil.which("gmgn-cli.cmd")
        # Locally the official config command writes ~/.config/gmgn/.env;
        # unattended runners inject GMGN_API_KEY directly.
        self.api_key_present = bool(
            os.getenv("GMGN_API_KEY") or (Path.home() / ".config" / "gmgn" / ".env").exists()
        )
        self.timeout = timeout
        self.ledger = ledger
        self.cache_ttl = cache_ttl
        self.min_interval_seconds = min_interval_seconds
        self.chains = tuple(
            dict.fromkeys(
                chain for value in chains
                if (chain := _normal_chain(value)) in CLI_CHAINS
            )
        ) or ("solana",)
        self._last_live_call = 0.0
        self._rate_limited = False

    @property
    def configured(self) -> bool:
        return bool(self.executable and self.api_key_present)

    @property
    def unavailable_reason(self) -> str:
        if not self.executable:
            return "gmgn-cli is not installed"
        if not self.api_key_present:
            return "GMGN_API_KEY is not configured"
        return "unavailable"

    async def _run(self, *args: str) -> Any:
        if not self.configured:
            raise GmgnError(self.unavailable_reason)
        cache_key = None
        if self.ledger is not None:
            cache_key = self.ledger.cache_key("CLI", "gmgn-cli", list(args), None)
            cached = self.ledger.cache_get(cache_key, self.cache_ttl)
            if cached is not None:
                return cached
        if self._rate_limited:
            raise GmgnError("GMGN rate limit reached; provider paused for this run")
        wait_for = self.min_interval_seconds - (time.monotonic() - self._last_live_call)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        process = await asyncio.create_subprocess_exec(
            str(self.executable), *args, "--raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise GmgnError(f"gmgn-cli timed out after {self.timeout:.0f}s")
        finally:
            self._last_live_call = time.monotonic()
        if process.returncode:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            safe = detail[-1] if detail else f"exit {process.returncode}"
            if "429" in safe or "RATE_LIMIT" in safe:
                if cache_key is not None:
                    stale = self.ledger.cache_get(cache_key, None)
                    if stale is not None:
                        return stale
                self._rate_limited = True
                raise GmgnError("GMGN rate limit reached; provider paused for this run")
            raise GmgnError(safe[:240])
        text = stdout.decode("utf-8", "replace").strip()
        try:
            payload = json.loads(text)
            if cache_key is not None:
                self.ledger.cache_put(cache_key, payload)
            return payload
        except json.JSONDecodeError as exc:
            raise GmgnError("gmgn-cli returned invalid JSON") from exc

    async def _safe(self, label: str, *args: str) -> tuple[str, Any | None, Exception | None]:
        try:
            return label, await self._run(*args), None
        except Exception as exc:
            return label, None, exc

    @staticmethod
    def _rank_rows(payload: Any) -> list[dict[str, Any]]:
        data = _unwrap(payload)
        if isinstance(data, dict):
            if isinstance(data.get("rank"), list):
                return _dicts(data["rank"])
            rows: list[dict[str, Any]] = []
            for key in ("new_creation", "pump", "near_completion", "completed", "tokens", "list"):
                rows.extend(_dicts(data.get(key)))
            return rows
        rows = _dicts(data)
        flattened: list[dict[str, Any]] = []
        for row in rows:
            nested = _dicts(row.get("tokens") or row.get("list") or row.get("rank"))
            flattened.extend(nested or [row])
        return flattened

    @staticmethod
    def _signal_rows(payload: Any) -> list[dict[str, Any]]:
        data = _unwrap(payload)
        if isinstance(data, dict):
            return _dicts(data.get("list") or data.get("signals"))
        return _dicts(data)

    @staticmethod
    def _trade_rows(payload: Any) -> list[dict[str, Any]]:
        data = _unwrap(payload)
        return _dicts(data.get("list")) if isinstance(data, dict) else _dicts(data)

    @staticmethod
    def trader_evidence(payload: Any) -> dict[str, Any]:
        """Summarise GMGN's token-specific renowned/KOL trader ledger.

        Ranking metadata says renowned wallets exist. This endpoint proves who
        traded, whether they held or exited, and what the trade produced. Keep
        suspicious/wash/bundler tags visible rather than counting every tagged
        address as clean social proof.
        """
        rows = GmgnSource._trade_rows(payload)
        traders: list[dict[str, Any]] = []
        for row in rows:
            tags = [str(value) for value in (row.get("tags") or [])]
            token_tags = [str(value) for value in (row.get("maker_token_tags") or [])]
            holding = number(row.get("balance") or row.get("amount_cur")) > 0
            suspicious = bool(row.get("is_suspicious")) or "wash_trader" in tags
            traders.append({
                "address": str(row.get("address") or ""),
                "name": str(row.get("name") or row.get("twitter_username") or str(row.get("address") or "")[:8]),
                "twitter": str(row.get("twitter_username") or ""),
                "holding": holding,
                "holdingPct": _ratio_pct(row.get("amount_percentage")) or 0.0,
                "buyUsd": number(row.get("buy_volume_cur")),
                "sellUsd": number(row.get("sell_volume_cur")),
                "profitUsd": number(row.get("profit")),
                "realizedProfitUsd": number(row.get("realized_profit")),
                "unrealizedProfitUsd": number(row.get("unrealized_profit")),
                "pnlPct": _ratio_pct(row.get("profit_change")),
                "suspicious": suspicious,
                "tags": tags,
                "tokenTags": token_tags,
                "startedAt": started.isoformat() if (started := _timestamp(row.get("start_holding_at"))) else None,
                "endedAt": ended.isoformat() if (ended := _timestamp(row.get("end_holding_at"))) else None,
                "lastActiveAt": active.isoformat() if (active := _timestamp(row.get("last_active_timestamp"))) else None,
            })
        trusted = [row for row in traders if not row["suspicious"]]
        profitable = [row for row in trusted if row["profitUsd"] > 0]
        holding = [row for row in trusted if row["holding"]]
        return {
            "renownedTraders": traders,
            "renownedTraderCount": len(traders),
            "renownedTrustedCount": len(trusted),
            "renownedProfitableCount": len(profitable),
            "renownedHoldingCount": len(holding),
            "renownedRealizedProfitUsd": sum(row["realizedProfitUsd"] for row in trusted),
            "renownedUnrealizedProfitUsd": sum(row["unrealizedProfitUsd"] for row in holding),
        }

    @staticmethod
    def wallet_count_evidence(payload: Any) -> dict[str, Any]:
        """Extract exact-mint wallet counts from ``token info``.

        Discovery ranks are intentionally broad and occasionally omit the
        wallet tag fields altogether. A missing ``renowned_count`` was being
        stored as zero, which hid heavily traded coins such as CYBERLEEK even
        though their exact token page had dozens of renowned wallets. Only
        copy fields whose units are identical here; launch-volume bundler
        statistics are not interchangeable with the rank endpoint's supply
        concentration fields.
        """
        data = _unwrap(payload)
        if not isinstance(data, dict):
            return {}
        tags = data.get("wallet_tags_stat") or {}
        stat = data.get("stat") or {}
        dev = data.get("dev") or {}
        return {
            "holders": integer(data.get("holder_count") or stat.get("holder_count")) or None,
            "top10Pct": _ratio_pct(
                stat.get("top_10_holder_rate")
                if stat.get("top_10_holder_rate") is not None
                else dev.get("top_10_holder_rate")
            ),
            "smartMoneyCount": integer(tags.get("smart_wallets")),
            "kolCount": integer(tags.get("renowned_wallets")),
            "exactWalletCountsChecked": True,
            "exactWalletCountSource": "token-info",
        }

    async def enrich_missing_wallet_counts(
        self,
        candidates: Iterable[Any],
        evidence_by_mint: dict[str, dict[str, Any]],
        *,
        limit: int = 30,
        force_all: bool = False,
    ) -> SourceStatus:
        """Fallback to exact-token counts when a discovery row says zero."""
        if not self.configured:
            return SourceStatus("GMGN exact wallet-count fallback", False, self.unavailable_reason)
        eligible = [
            candidate for candidate in candidates
            if candidate.token.chain_id.lower() in CLI_CHAINS
            and (force_all or (
                int((evidence_by_mint.get(candidate.token.mint, {}) or {}).get("kolCount") or 0) <= 0
                or int((evidence_by_mint.get(candidate.token.mint, {}) or {}).get("smartMoneyCount") or 0) <= 0
            ))
        ]
        ranked = sorted(
            eligible,
            key=lambda candidate: (
                float(candidate.token.volume_24h or 0),
                float(candidate.peak_market_cap or candidate.observed_peak_market_cap or 0),
                float(candidate.token.market_cap or 0),
            ),
            reverse=True,
        )
        if limit > 0:
            ranked = ranked[:limit]
        checked = 0
        recovered_kol = 0
        failures: list[str] = []
        for candidate in ranked:
            chain = CLI_CHAINS[candidate.token.chain_id.lower()]
            _, payload, error = await self._safe(
                f"wallet-counts:{chain}:{candidate.token.mint}",
                "token", "info", "--chain", chain, "--address", candidate.token.mint,
            )
            if error is not None:
                failures.append(str(error))
                if "rate limit" in str(error).lower():
                    break
                continue
            summary = self.wallet_count_evidence(payload)
            if not summary:
                continue
            evidence = evidence_by_mint.setdefault(candidate.token.mint, {"origins": []})
            old_kol = int(evidence.get("kolCount") or 0)
            for key, value in summary.items():
                if value is not None:
                    evidence[key] = value
            if old_kol <= 0 < int(evidence.get("kolCount") or 0):
                recovered_kol += 1
            checked += 1
        detail = f"{checked}/{len(ranked)} zero/incomplete finalists checked; {recovered_kol} KOL counts recovered"
        if failures:
            detail += f"; partial: {failures[0][:160]}"
        return SourceStatus("GMGN exact wallet-count fallback", checked > 0, detail)

    @staticmethod
    def kline_evidence(payload: Any, token: TokenSnapshot) -> dict[str, Any]:
        """Turn GMGN's hourly candles into an honest trailing-24h move.

        Dex feeds expose the present and a percentage.  The candles prove the
        actual high reached during the report window, including runners that
        subsequently faded.  Market-cap estimates preserve the token's current
        circulating-supply basis by scaling its current cap by price ratios.
        """
        data = _unwrap(payload)
        rows = _dicts(data.get("list")) if isinstance(data, dict) else _dicts(data)
        candles = [
            {
                "time": integer(row.get("time")),
                "open": number(row.get("open")),
                "close": number(row.get("close")),
                "high": number(row.get("high")),
                "low": number(row.get("low")),
                "volume": number(row.get("volume")),
            }
            for row in rows
            if number(row.get("open")) > 0 and number(row.get("close")) > 0
        ]
        candles.sort(key=lambda row: row["time"])
        if not candles:
            return {}
        opening = candles[0]["open"]
        closing = candles[-1]["close"]
        peak_row = max(candles, key=lambda row: row["high"])
        low_row = min(candles, key=lambda row: row["low"] if row["low"] > 0 else float("inf"))
        high = peak_row["high"]
        low = low_row["low"]
        current_cap = float(token.market_cap or 0)
        peak_cap = current_cap * high / closing if current_cap and closing else 0.0
        low_cap = current_cap * low / closing if current_cap and closing and low else 0.0
        peak_time = _timestamp(peak_row["time"])
        return {
            "kline24hCandleCount": len(candles),
            "kline24hOpenPrice": opening,
            "kline24hClosePrice": closing,
            "kline24hHighPrice": high,
            "kline24hLowPrice": low,
            "kline24hChangePct": (closing / opening - 1.0) * 100.0,
            "kline24hPeakFromOpenPct": (high / opening - 1.0) * 100.0,
            "kline24hHighLowMultiple": high / low if low else 1.0,
            "kline24hPeakMarketCap": peak_cap,
            "kline24hLowMarketCap": low_cap,
            "kline24hVolumeUsd": sum(row["volume"] for row in candles),
            "kline24hPeakAt": peak_time.isoformat() if peak_time else None,
        }

    async def enrich_runner_klines(
        self,
        tokens: Iterable[TokenSnapshot],
        evidence_by_mint: dict[str, dict[str, Any]],
        *,
        now: datetime,
        limit: int = 40,
        min_kol_count: int = 1,
        exact_only: bool = False,
    ) -> SourceStatus:
        """Verify the 24h path for KOL-backed runner candidates."""
        if not self.configured:
            return SourceStatus("GMGN 24h candle verification", False, self.unavailable_reason)
        eligible = [
            token for token in tokens
            if token.chain_id.lower() in CLI_CHAINS
            and int((evidence_by_mint.get(token.mint, {}) or {}).get("kolCount") or 0) >= min_kol_count
            and (
                not exact_only
                or bool((evidence_by_mint.get(token.mint, {}) or {}).get("exactWalletCountsChecked"))
            )
        ]
        ranked = sorted(
            eligible,
            key=lambda token: (
                bool((evidence_by_mint.get(token.mint, {}) or {}).get("organicQualified")),
                int((evidence_by_mint.get(token.mint, {}) or {}).get("kolCount") or 0),
                float(token.volume_24h or 0),
                float(token.market_cap or 0),
            ),
            reverse=True,
        )
        if limit > 0:
            ranked = ranked[:limit]
        # Query completed hourly buckets. Rounding the boundary also makes the
        # SQLite cache reusable by repeated checks inside the same hour.
        end_at = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        end = int(end_at.timestamp())
        start = int((end_at - timedelta(hours=24)).timestamp())
        checked = 0
        failures: list[str] = []
        for token in ranked:
            chain = CLI_CHAINS[token.chain_id.lower()]
            _, payload, error = await self._safe(
                f"kline:{chain}:{token.mint}",
                "market", "kline", "--chain", chain, "--address", token.mint,
                "--resolution", "1h", "--from", str(start), "--to", str(end),
            )
            if error is not None:
                failures.append(str(error))
                if "rate limit" in str(error).lower():
                    break
                continue
            summary = self.kline_evidence(payload, token)
            if not summary:
                continue
            evidence_by_mint.setdefault(token.mint, {"origins": []}).update(summary)
            checked += 1
        available = checked > 0
        detail = f"{checked}/{len(ranked)} KOL-backed candidates verified from hourly candles"
        if failures:
            detail += f"; partial: {failures[0][:160]}"
        label = "GMGN recovered-mint 24h candles" if exact_only else "GMGN 24h candle verification"
        return SourceStatus(label, available, detail)

    async def enrich_runner_traders(
        self,
        candidates: Iterable[Any],
        evidence_by_mint: dict[str, dict[str, Any]],
        *,
        limit: int = 25,
        rows_per_token: int = 20,
    ) -> SourceStatus:
        """Add token-specific renowned trader outcomes to likely recap names."""
        if not self.configured:
            return SourceStatus("GMGN renowned trader tape", False, self.unavailable_reason)
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                bool((evidence_by_mint.get(candidate.token.mint, {}) or {}).get("exactWalletCountsChecked")),
                bool((evidence_by_mint.get(candidate.token.mint, {}) or {}).get("organicQualified")),
                int((evidence_by_mint.get(candidate.token.mint, {}) or {}).get("kolCount") or 0),
                float(candidate.token.volume_24h or 0),
                float(candidate.token.market_cap or 0),
            ),
            reverse=True,
        )
        if limit > 0:
            ranked = ranked[:limit]
        checked = 0
        with_traders = 0
        failures: list[str] = []
        for candidate in ranked:
            chain = CLI_CHAINS.get(candidate.token.chain_id.lower())
            if not chain:
                continue
            label = f"traders:{chain}:{candidate.token.mint}"
            _, payload, error = await self._safe(
                label, "token", "traders", "--chain", chain,
                "--address", candidate.token.mint, "--tag", "renowned",
                "--limit", str(max(1, min(rows_per_token, 100))),
                "--order-by", "profit",
            )
            if error is not None:
                failures.append(str(error))
                if "rate limit" in str(error).lower():
                    break
                continue
            checked += 1
            summary = self.trader_evidence(payload)
            evidence = evidence_by_mint.setdefault(candidate.token.mint, {"origins": []})
            evidence.update(summary)
            evidence["exactTraderHistoryChecked"] = True
            # Exact trader history is stronger evidence than a missing count
            # on a broad rank row. Suspicious/wash-tagged wallets stay visible
            # but do not satisfy the clean publishing count.
            evidence["kolCount"] = max(
                int(evidence.get("kolCount") or 0),
                int(summary.get("renownedTrustedCount") or 0),
            )
            if summary["renownedTraderCount"]:
                with_traders += 1
        available = checked > 0
        detail = f"{checked}/{len(ranked)} finalists checked; {with_traders} had renowned trader history"
        if failures:
            detail += f"; partial: {failures[0][:160]}"
        return SourceStatus("GMGN renowned trader tape", available, detail)

    async def discover(self, now: datetime) -> GmgnDiscovery:
        result = GmgnDiscovery()
        if not self.configured:
            result.statuses.append(SourceStatus("GMGN", False, self.unavailable_reason))
            return result
        jobs: list[tuple[str, tuple[str, ...]]] = []
        for chain_id in self.chains:
            cli_chain = CLI_CHAINS[chain_id]
            safety_filters = (
                ("renounced", "frozen", "not_wash_trading")
                if cli_chain == "sol"
                else ("not_honeypot", "verified", "renounced", "locked")
            )
            organic_args: list[str] = [
                "market", "trending", "--chain", cli_chain, "--interval", "24h",
                "--order-by", "volume", "--min-volume", "250000",
                "--min-liquidity", "40000", "--min-holder-count", "1000",
                "--min-swaps", "1000", "--max-top10-holder-rate", "0.25",
                "--max-insider-rate", "0.30", "--max-bundler-rate", "0.30",
            ]
            for safety_filter in safety_filters:
                organic_args.extend(("--filter", safety_filter))
            organic_args.extend(("--limit", "100"))
            jobs.extend((
                # Primary runner backbone: GMGN applies chain-native safety,
                # distribution and participation filters server-side. The
                # wider lanes below remain discovery/audit context only.
                (f"trending-organic:{cli_chain}", tuple(organic_args)),
                # Dedicated wallet-ranked lanes prevent a KOL-backed runner
                # from being pushed below a top-100 volume page by majors or
                # wash-heavy high-turnover names. These are discovery only;
                # the exact mint trader ledger and safety gates still decide.
                (f"trending-kol:{cli_chain}", (
                    "market", "trending", "--chain", cli_chain, "--interval", "24h",
                    "--order-by", "renowned_count", "--min-renowned-count", "1",
                    "--min-history-highest-marketcap", "200000", "--limit", "100",
                )),
                (f"trending-smartmoney:{cli_chain}", (
                    "market", "trending", "--chain", cli_chain, "--interval", "24h",
                    "--order-by", "smart_degen_count", "--min-smart-degen-count", "1",
                    "--min-history-highest-marketcap", "200000", "--limit", "100",
                )),
                (f"trending-volume:{cli_chain}", ("market", "trending", "--chain", cli_chain, "--interval", "24h", "--order-by", "volume", "--limit", "100")),
                # This is the retrospective runner lane. A coin remains discoverable
                # after fading because GMGN exposes the highest market cap reached.
                (f"trending-ath:{cli_chain}", ("market", "trending", "--chain", cli_chain, "--interval", "24h", "--order-by", "history_highest_market_cap", "--min-history-highest-marketcap", "200000", "--max-created", "30h", "--limit", "100")),
                (f"trending-holders:{cli_chain}", ("market", "trending", "--chain", cli_chain, "--interval", "24h", "--order-by", "holder_count", "--limit", "100")),
                (f"trenches:{cli_chain}", ("market", "trenches", "--chain", cli_chain, "--type", "new_creation", "--type", "near_completion", "--type", "completed", "--limit", "80")),
                (f"hot-searches:{cli_chain}", ("market", "hot-searches", "--chain", cli_chain, "--interval", "24h", "--limit", "100")),
                (f"kol-trades:{cli_chain}", ("track", "kol", "--chain", cli_chain, "--limit", "200")),
                (f"smartmoney-trades:{cli_chain}", ("track", "smartmoney", "--chain", cli_chain, "--limit", "200")),
            ))
            if cli_chain in {"sol", "bsc"}:
                jobs.append((
                    f"signals:{cli_chain}",
                    ("market", "signal", "--chain", cli_chain, "--groups", '[{"signal_type":[6,7,8]},{"signal_type":[12,13,20]}]'),
                ))
        # GMGN's free allowance is weighted and aggressively rate-limited.
        # Run sequentially and open the circuit on the first 429: continuing to
        # hammer the provider extends the temporary IP ban.
        responses: list[tuple[str, Any | None, Exception | None]] = []
        for label, arguments in jobs:
            response = await self._safe(label, *arguments)
            responses.append(response)
            if response[2] is not None and "rate limit" in str(response[2]).lower():
                break
        failures: list[str] = []
        token_by_mint: dict[str, TokenSnapshot] = {}
        for label, payload, error in responses:
            if error is not None:
                failures.append(f"{label}: {error}")
                continue
            cli_chain = label.rsplit(":", 1)[-1]
            chain_id = _normal_chain(cli_chain)
            if label.startswith(("kol-trades:", "smartmoney-trades:")):
                result.wallet_flow_available = True
                result.wallet_flow_chains.add(chain_id)
                wallet_kind = "kol" if label.startswith("kol-trades:") else "smart_money"
                for row in self._trade_rows(payload):
                    mint = str(row.get("base_address") or "")
                    wallet = str(row.get("maker") or (row.get("maker_info") or {}).get("address") or "")
                    if not _valid_token_address(mint, chain_id) or not wallet:
                        continue
                    event = {
                        "eventKey": str(row.get("transaction_hash") or f"{wallet_kind}:{wallet}:{mint}:{row.get('timestamp')}:{row.get('side')}"),
                        "mint": mint,
                        "chain": chain_id,
                        "wallet": wallet,
                        "walletKind": wallet_kind,
                        "side": str(row.get("side") or "unknown").lower(),
                        "occurredAt": _timestamp(row.get("timestamp")),
                        "amountUsd": number(row.get("amount_usd")) or None,
                        "name": str((row.get("maker_info") or {}).get("twitter_username") or wallet[:8]),
                        "payload": row,
                    }
                    result.wallet_events.append(event)
                continue
            rows = self._signal_rows(payload) if label.startswith("signals:") else self._rank_rows(payload)
            for row in rows:
                if label.startswith("signals:"):
                    cur = row.get("cur_data") or {}
                    normalized = {
                        **(row.get("data") or {}),
                        **cur,
                        "address": row.get("token_address"),
                        "market_cap": row.get("market_cap"),
                        "history_highest_market_cap": row.get("ath"),
                    }
                    item = normalized
                else:
                    item = row
                token = parse_rank_item(item, origin=f"gmgn:{label}", chain=chain_id)
                mint = str(item.get("address") or item.get("token_address") or "")
                if not token:
                    if _valid_token_address(mint, chain_id):
                        evidence = result.evidence.setdefault(mint, {"origins": []})
                        evidence["origins"].append(f"gmgn:{label}")
                    continue
                prior = token_by_mint.get(token.mint)
                if prior is None or token.liquidity_usd > prior.liquidity_usd:
                    token_by_mint[token.mint] = token
                evidence = result.evidence.setdefault(token.mint, {"origins": []})
                evidence["origins"].append(f"gmgn:{label}")
                if label.startswith("trending-organic:"):
                    evidence["organicQualified"] = True
                for key, value in evidence_from_rank(item, f"gmgn:{label}").items():
                    if key == "origin":
                        continue
                    # Preserve observed zero/false. They mean the provider
                    # checked the field and found none, which is different
                    # from an unavailable field.
                    if value not in (None, "", {}):
                        if key == "athMarketCap":
                            evidence[key] = max(float(evidence.get(key) or 0), float(value))
                        else:
                            evidence[key] = value
        result.tokens = list(token_by_mint.values())
        result.statuses.append(SourceStatus(
            "GMGN read-only union",
            not failures,
            f"{len(result.tokens)} tokens and {len(result.wallet_events)} KOL/smart-money events across {', '.join(self.chains)}"
            + (f"; partial: {' | '.join(failures[:3])}" if failures else ""),
        ))
        return result


def aggregate_wallet_evidence(
    events: Iterable[dict[str, Any]], mint: str, chain: str | None = None,
) -> dict[str, Any]:
    relevant = [
        event for event in events
        if event.get("mint") == mint and (chain is None or event.get("chain") == chain)
    ]
    buys = [event for event in relevant if event.get("side") == "buy"]
    sells = [event for event in relevant if event.get("side") == "sell"]
    kol_buyers = sorted({event["wallet"] for event in buys if event.get("walletKind") == "kol"})
    smart_buyers = sorted({event["wallet"] for event in buys if event.get("walletKind") == "smart_money"})
    kol_sellers = sorted({event["wallet"] for event in sells if event.get("walletKind") == "kol"})
    smart_sellers = sorted({event["wallet"] for event in sells if event.get("walletKind") == "smart_money"})
    return {
        "kolBuyers": kol_buyers,
        "smartMoneyBuyers": smart_buyers,
        "kolBuyerNames": sorted({str(event.get("name") or event["wallet"][:8]) for event in buys if event.get("walletKind") == "kol"}),
        "smartMoneyBuyerNames": sorted({str(event.get("name") or event["wallet"][:8]) for event in buys if event.get("walletKind") == "smart_money"}),
        "kolSellers": kol_sellers,
        "smartMoneySellers": smart_sellers,
        "kolSellerNames": sorted({str(event.get("name") or event["wallet"][:8]) for event in sells if event.get("walletKind") == "kol"}),
        "smartMoneySellerNames": sorted({str(event.get("name") or event["wallet"][:8]) for event in sells if event.get("walletKind") == "smart_money"}),
        "kolCount": len(kol_buyers),
        "smartMoneyCount": len(smart_buyers),
        "buyUsd": sum(float(event.get("amountUsd") or 0) for event in buys),
        "sellUsd": sum(float(event.get("amountUsd") or 0) for event in sells),
        "eventCount": len(relevant),
    }
