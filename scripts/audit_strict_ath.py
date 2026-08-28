from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from brief.models import number
from brief.sources.gmgn import CLI_CHAINS, GmgnSource, _dicts, _unwrap


def dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def relative_error(left: float, right: float) -> float | None:
    if left <= 0 or right <= 0:
        return None
    return abs(left - right) / max(left, right)


async def audit(snapshot_path: Path, output_path: Path) -> dict:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = snapshot.get("runnerUniverse") or []
    start = int(dt(snapshot["windowStart"]).timestamp())
    end = int(dt(snapshot["generatedAt"]).timestamp())
    source = GmgnSource(timeout=45, min_interval_seconds=1.25, chains=CLI_CHAINS)
    results: list[dict] = []

    for index, row in enumerate(rows, 1):
        token = row.get("token") or {}
        mint = str(row.get("mint") or token.get("mint") or "")
        chain_id = str(row.get("chainId") or row.get("chain") or token.get("chainId") or token.get("chain") or "").lower()
        chain = CLI_CHAINS.get(chain_id)
        gmgn = ((row.get("providerEvidence") or {}).get("gmgn") or {})
        result = {
            "symbol": row.get("symbol") or token.get("symbol"),
            "chain": chain_id,
            "mint": mint,
            "oldPublishedPeakMarketCap": max(
                number(row.get("peakMarketCap")),
                number(row.get("observedPeakMarketCap")),
                number(gmgn.get("kline24hPeakMarketCap")),
            ),
            "status": "unverified",
        }
        if not chain or not mint:
            result["reason"] = "unsupported chain or missing contract"
            results.append(result)
            continue

        _, info, info_error = await source._safe(
            f"audit-info:{chain}:{mint}", "token", "info", "--chain", chain, "--address", mint,
        )
        if info_error or not isinstance(_unwrap(info), dict):
            result["reason"] = f"token info unavailable: {info_error}"
            results.append(result)
            continue
        data = _unwrap(info)
        supply = number(data.get("circulating_supply") or data.get("total_supply") or data.get("max_supply"))
        ath_price = number(data.get("ath_price"))
        ath_info = ((data.get("dev") or {}).get("ath_token_info") or {})
        ath_identity_exact = str(ath_info.get("ath_token") or "").casefold() == mint.casefold()
        provider_ath = number(ath_info.get("ath_mc")) if ath_identity_exact else 0.0
        computed_lifetime = ath_price * supply if ath_price and supply else 0.0
        lifetime_error = relative_error(computed_lifetime, provider_ath)
        returned_contract_exact = str(data.get("address") or "").casefold() == mint.casefold()

        _, candles_payload, candles_error = await source._safe(
            f"audit-kline:{chain}:{mint}", "market", "kline", "--chain", chain,
            "--address", mint, "--resolution", "1h", "--from", str(start), "--to", str(end),
        )
        candle_data = _unwrap(candles_payload)
        candles = _dicts(candle_data.get("list")) if isinstance(candle_data, dict) else _dicts(candle_data)
        high = max((number(item.get("high")) for item in candles), default=0.0)
        corrected = high * supply if high and supply else 0.0
        old = result["oldPublishedPeakMarketCap"]
        published_error = relative_error(old, corrected)
        verified = bool(
            not candles_error and returned_contract_exact and supply > 0 and high > 0
            and (lifetime_error is None or lifetime_error <= 0.10)
            and (provider_ath <= 0 or corrected <= provider_ath * 1.10)
        )
        result.update({
            "exactSupply": supply or None,
            "athPrice": ath_price or None,
            "providerLifetimeAthMarketCap": provider_ath or None,
            "computedLifetimeAthMarketCap": computed_lifetime or None,
            "lifetimeCrossCheckErrorPct": lifetime_error * 100 if lifetime_error is not None else None,
            "returnedContractExact": returned_contract_exact,
            "providerAthContractExact": ath_identity_exact,
            "windowHighPrice": high or None,
            "correctedWindowPeakMarketCap": corrected or None,
            "publishedErrorPct": published_error * 100 if published_error is not None else None,
            "wrong": bool(verified and published_error is not None and published_error > 0.10),
            "status": "verified" if verified else "unverified",
            "reason": None if verified else str(candles_error or "exact-contract/supply/candle cross-check failed"),
        })
        results.append(result)
        if index % 6 == 0 or index == len(rows):
            print(f"audited {index}/{len(rows)}", flush=True)

    verified_rows = [row for row in results if row["status"] == "verified"]
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "snapshotGeneratedAt": snapshot.get("generatedAt"),
        "runnerCount": len(rows),
        "verifiedCount": len(verified_rows),
        "unverifiedCount": len(results) - len(verified_rows),
        "wrongCount": sum(bool(row.get("wrong")) for row in verified_rows),
        "rules": {
            "exactContract": True,
            "exactSupplyRequired": True,
            "priceCandlesRequired": True,
            "providerAthCrossCheckTolerancePct": 10,
            "publishedDifferenceTolerancePct": 10,
            "failClosed": True,
        },
        "coins": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def apply_verified_corrections(snapshot_path: Path, report: dict) -> int:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    corrections = {
        str(row["mint"]).casefold(): row
        for row in report.get("coins", [])
        if str(row.get("mint") or "")
    }
    changed = 0

    def visit(value: object) -> None:
        nonlocal changed
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        token = value.get("token") if isinstance(value.get("token"), dict) else {}
        mint = str(value.get("mint") or token.get("mint") or "").casefold()
        correction = corrections.get(mint)
        if correction:
            verified = correction.get("status") == "verified"
            peak = number(correction.get("correctedWindowPeakMarketCap")) if verified else None
            for field in ("peakMarketCap", "observedPeakMarketCap"):
                if field in value:
                    value[field] = peak
            value["athVerified"] = verified
            provider = value.get("providerEvidence")
            if isinstance(provider, dict) and isinstance(provider.get("gmgn"), dict):
                gmgn = provider["gmgn"]
                gmgn["kline24hPeakMarketCap"] = peak
                gmgn["kline24hMarketCapVerified"] = verified
                gmgn["kline24hExactSupply"] = correction.get("exactSupply") if verified else None
                gmgn["kline24hSupplyCheckErrorPct"] = None
            changed += 1
        for child in value.values():
            visit(child)

    visit(snapshot)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("web/data/latest.json"))
    parser.add_argument("--output", type=Path, default=Path("output/strict-ath-audit.json"))
    parser.add_argument("--apply", action="store_true", help="replace snapshot peaks with verified exact-supply peaks")
    parser.add_argument(
        "--require-all-verified",
        action="store_true",
        help="exit non-zero when any runner cannot be verified; use this before publishing",
    )
    args = parser.parse_args()
    report = asyncio.run(audit(args.snapshot, args.output))
    if args.apply:
        report["snapshotObjectsCorrected"] = apply_verified_corrections(args.snapshot, report)
    print(json.dumps({key: report[key] for key in ("runnerCount", "verifiedCount", "unverifiedCount", "wrongCount")}))
    if args.require_all_verified and report["unverifiedCount"]:
        print(
            f"STRICT ATH AUDIT FAILED: {report['unverifiedCount']} of "
            f"{report['runnerCount']} runners could not be verified; publication is blocked.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
