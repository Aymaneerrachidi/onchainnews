"""Produce an exact-contract diff and distribution stats for two run snapshots."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brief.config import load_settings  # noqa: E402
from scripts.send_live_discord_recap import (  # noqa: E402
    _dedupe,
    _eligible_for_recap,
    _verified_peak,
)


def _bucket(value: float, stops: list[tuple[float, str]], fallback: str) -> str:
    for ceiling, label in stops:
        if value <= ceiling:
            return label
    return fallback


CHAIN_ALIASES = {"bnb": "bsc", "eth": "ethereum"}


def _load(path: Path, settings, *, apply_gate: bool = True) -> tuple[dict, list]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    excluded = {
        str(mint).strip().lower()
        for mint in settings.get("journal", "excluded_mints", []) or []
    }
    candidates = _dedupe(list(payload.get("runners") or []), excluded)
    if apply_gate:
        candidates = [candidate for candidate in candidates if _eligible_for_recap(candidate, settings)]
    return payload, candidates


def _key(candidate) -> tuple[str, str]:
    return candidate.token.chain_id.lower(), candidate.token.mint.lower()


def _preview_keys(path: Path) -> set[tuple[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    keys: set[tuple[str, str]] = set()
    for chain, mint in re.findall(
        r"https://fomo\.family/tokens/([^/]+)/([^\)\s\\\"]+)", text, flags=re.IGNORECASE
    ):
        canonical = CHAIN_ALIASES.get(chain.lower(), chain.lower())
        keys.add((canonical, mint.lower()))
    return keys


def _row(candidate) -> dict:
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    return {
        "chain": candidate.token.chain_id,
        "symbol": candidate.token.symbol,
        "mint": candidate.token.mint,
        "ath": round(_verified_peak(candidate), 2),
        "marketCap": round(float(candidate.token.market_cap or 0), 2),
        "change24h": round(float(candidate.token.price_change_24h or 0), 2),
        "ageHours": round(float(candidate.signals.age_hours or 0), 1),
        "volume24h": round(float(candidate.token.volume_24h or 0), 2),
        "liquidity": round(float(candidate.token.liquidity_usd or 0), 2),
        "holders": int(candidate.enrichment.holder_count or candidate.safety.holder_count or 0),
        "kols": len(set(candidate.kol_buyers + candidate.kol_sellers + candidate.kol_holders))
        or int(gmgn.get("renownedCount") or gmgn.get("kolCount") or 0),
        "smartMoney": int(gmgn.get("smartDegenCount") or gmgn.get("smartMoneyCount") or 0),
        "drawdownPct": round(float(candidate.drawdown_from_peak_pct or 0), 1),
        "riskLabels": list(candidate.risk_labels or []),
    }


def _stats(candidates: list) -> dict:
    ages = Counter()
    aths = Counter()
    chains = Counter()
    for candidate in candidates:
        age = float(candidate.signals.age_hours or 0)
        peak = _verified_peak(candidate)
        ages[_bucket(age, [(30, "0-30h"), (168, "30h-7d"), (720, "7-30d")], ">30d")] += 1
        aths[_bucket(
            peak,
            [
                (2_000_000, "$1-2M"),
                (5_000_000, "$2-5M"),
                (10_000_000, "$5-10M"),
                (20_000_000, "$10-20M"),
            ],
            "$20M+",
        )] += 1
        chains[candidate.token.chain_id] += 1
    return {
        "count": len(candidates),
        "chains": dict(chains),
        "ages": dict(ages),
        "athBands": dict(aths),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument(
        "--before-preview",
        help="Optional archived Discord preview containing the exact previously approved contracts",
    )
    args = parser.parse_args()
    settings = load_settings(str(ROOT / "config.toml"))
    before_payload, before = _load(
        ROOT / args.before,
        settings,
        apply_gate=not bool(args.before_preview),
    )
    if args.before_preview:
        approved = _preview_keys(ROOT / args.before_preview)
        before = [candidate for candidate in before if _key(candidate) in approved]
    after_payload, after = _load(ROOT / args.after, settings)
    before_by = {_key(candidate): candidate for candidate in before}
    after_by = {_key(candidate): candidate for candidate in after}
    repeated = sorted(before_by.keys() & after_by.keys())
    added = sorted(after_by.keys() - before_by.keys())
    dropped = sorted(before_by.keys() - after_by.keys())
    output = {
        "beforeGeneratedAt": before_payload.get("generatedAt"),
        "afterGeneratedAt": after_payload.get("generatedAt"),
        "before": _stats(before),
        "after": _stats(after),
        "counts": {"new": len(added), "repeated": len(repeated), "dropped": len(dropped)},
        "new": [_row(after_by[key]) for key in added],
        "repeated": [_row(after_by[key]) for key in repeated],
        "dropped": [_row(before_by[key]) for key in dropped],
        "sources": after_payload.get("sources") or [],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
