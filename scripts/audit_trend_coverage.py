"""Compare the completed runner snapshot with GMGN's current 24h trend boards.

The comparison is contract-keyed.  It intentionally applies the newsletter's
minimum quality bar before computing coverage so majors, sub-$1M flashes and
obvious risk rows do not make the scanner look like it missed valid runners.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brief.config import load_settings  # noqa: E402
from scripts.send_live_discord_recap import _dedupe, _eligible_for_recap  # noqa: E402

CHAIN_ALIASES = {
    "sol": "solana",
    "bsc": "bsc",
    "base": "base",
    "eth": "ethereum",
    "robinhood": "robinhood",
}


def _identity(chain: str, address: str) -> tuple[str, str]:
    return (CHAIN_ALIASES.get(chain.lower(), chain.lower()), address.strip().lower())


def _payload(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        raise RuntimeError(f"GMGN returned no JSON: {stdout[:240]}")
    return json.loads(stdout[start:])


def _rank(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data: Any = payload.get("data")
    if isinstance(data, dict):
        for key in ("rank", "items", "list", "tokens"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _fetch(chain: str, limit: int) -> list[dict[str, Any]]:
    executable = shutil.which("gmgn-cli.cmd") or shutil.which("gmgn-cli")
    if not executable:
        raise RuntimeError("gmgn-cli is not available on PATH")
    completed = subprocess.run(
        [
            executable, "market", "trending", "--chain", chain,
            "--interval", "24h", "--order-by", "volume",
            "--limit", str(limit), "--raw",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return _rank(_payload(completed.stdout))


def _eligible(row: dict[str, Any], now: datetime) -> tuple[bool, str]:
    ath = float(row.get("history_highest_market_cap") or 0)
    liquidity = float(row.get("liquidity") or 0)
    rug = float(row.get("rug_ratio") or 0)
    if ath < 1_000_000:
        return False, "ATH below $1M"
    if liquidity < 20_000:
        return False, "liquidity below $20k"
    if bool(row.get("is_wash_trading")):
        return False, "wash-trading flag"
    if int(row.get("is_honeypot") or 0):
        return False, "honeypot flag"
    if rug > 0.30:
        return False, f"rug ratio {rug:.0%}"
    if str(row.get("chain") or "").lower() == "sol" and int(row.get("renowned_count") or 0) < 1:
        return False, "no mapped Solana KOL"

    opened = float(row.get("open_timestamp") or row.get("creation_timestamp") or 0)
    age_hours = (now.timestamp() - opened) / 3600 if opened else 10**9
    if age_hours <= 720:
        return True, ""

    move = float(row.get("price_change_percent") or 0)
    if ath < 10_000_000:
        required = 75.0
    elif ath < 20_000_000:
        required = 50.0
    else:
        required = 30.0
    if move < required:
        return False, f"old coin moved {move:.0f}% (needs {required:.0f}%)"
    return True, ""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="web/data/latest.json")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    snapshot_path = ROOT / args.snapshot
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    scanner_raw = {
        _identity(str(row.get("chain") or ""), str(row.get("mint") or ""))
        for row in snapshot.get("runners") or []
        if row.get("mint")
    }
    blocked_by_identity = {
        _identity(str(row.get("chain") or ""), str(row.get("mint") or "")): row
        for row in snapshot.get("blockedRunners") or []
        if row.get("mint")
    }
    settings = load_settings(ROOT / "config.toml")
    excluded = {
        str(mint).strip().lower()
        for mint in settings.get("journal", "excluded_mints", []) or []
        if str(mint).strip()
    }
    publishable_rows = [
        candidate
        for candidate in _dedupe(list(snapshot.get("runners") or []), excluded)
        if _eligible_for_recap(candidate, settings)
    ]
    scanner_publishable = {
        _identity(str(row.token.chain_id), str(row.token.mint))
        for row in publishable_rows
        if row.token.mint
    }
    now = datetime.now(UTC)
    result: dict[str, Any] = {
        "snapshotGeneratedAt": snapshot.get("generatedAt"),
        "scannerRawContracts": len(scanner_raw),
        "scannerPublishableContracts": len(scanner_publishable),
        "chains": {},
    }

    for gmgn_chain, canonical in CHAIN_ALIASES.items():
        rows = _fetch(gmgn_chain, args.limit)
        eligible: list[dict[str, Any]] = []
        rejected: dict[str, int] = {}
        for row in rows:
            ok, reason = _eligible(row, now)
            if not ok:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            identity = _identity(gmgn_chain, str(row.get("address") or ""))
            blocked = blocked_by_identity.get(identity)
            eligible.append({
                "symbol": row.get("symbol"),
                "address": row.get("address"),
                "ath": row.get("history_highest_market_cap"),
                "marketCap": row.get("market_cap"),
                "change24h": row.get("price_change_percent"),
                "volume24h": row.get("volume"),
                "liquidity": row.get("liquidity"),
                "kols": row.get("renowned_count"),
                "smartMoney": row.get("smart_degen_count"),
                "rugRatio": row.get("rug_ratio"),
                "matchedRaw": identity in scanner_raw,
                "matchedPublishable": identity in scanner_publishable,
                "snapshotDisposition": (
                    "published" if identity in scanner_publishable
                    else "filtered after refresh" if identity in scanner_raw
                    else "blocked" if blocked is not None
                    else "not discovered"
                ),
                "snapshotCurrentMarketCap": blocked.get("marketCap") if blocked else None,
                "snapshotPeakMarketCap": blocked.get("peakMarketCap") if blocked else None,
                "snapshotDrawdownPct": blocked.get("drawdownFromPeakPct") if blocked else None,
                "snapshotRoundTrip": blocked.get("roundTrip") if blocked else None,
                "snapshotRiskLabels": blocked.get("riskLabels") if blocked else None,
            })
        matched_raw = sum(1 for row in eligible if row["matchedRaw"])
        matched_publishable = sum(1 for row in eligible if row["matchedPublishable"])
        result["chains"][canonical] = {
            "rawTop100": len(rows),
            "eligible": len(eligible),
            "matchedRaw": matched_raw,
            "rawCoveragePct": round(matched_raw / len(eligible) * 100, 1) if eligible else 100.0,
            "matchedPublishable": matched_publishable,
            "publishableCoveragePct": (
                round(matched_publishable / len(eligible) * 100, 1) if eligible else 100.0
            ),
            "missedRaw": [row for row in eligible if not row["matchedRaw"]],
            "missedPublishable": [row for row in eligible if not row["matchedPublishable"]],
            "missedByDisposition": {
                disposition: sum(
                    1 for row in eligible
                    if not row["matchedPublishable"] and row["snapshotDisposition"] == disposition
                )
                for disposition in ("filtered after refresh", "blocked", "not discovered")
            },
            "rejected": dict(sorted(rejected.items(), key=lambda item: (-item[1], item[0]))),
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
