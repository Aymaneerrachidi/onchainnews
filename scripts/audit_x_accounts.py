"""Audit quiet monitored X accounts without re-fetching active accounts.

The existing raw-response archive establishes recent activity. Only handles
not seen there are queried for their latest timeline, which keeps the audit
substantially cheaper than rescanning the full list.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import zlib

import httpx


ROOT = Path(__file__).resolve().parents[1]
SEARCH_ENDPOINT = "https://api.twitterapi.io/twitter/tweet/advanced_search"
TIMELINE_ENDPOINT = "https://api.twitterapi.io/twitter/user/last_tweets"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def unpack(value: str | bytes) -> str:
    return zlib.decompress(value).decode("utf-8") if isinstance(value, bytes) else value


def parse_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
        except ValueError:
            pass
    try:
        return datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y").astimezone(UTC)
    except ValueError:
        return None


def archived_activity(db_path: Path, monitored: set[str]) -> tuple[Counter[str], dict[str, str]]:
    counts: Counter[str] = Counter()
    latest: dict[str, str] = {}
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT response_body FROM raw_responses WHERE endpoint=? AND status BETWEEN 200 AND 299",
            (SEARCH_ENDPOINT,),
        )
        for (body,) in rows:
            try:
                payload = json.loads(unpack(body))
            except (UnicodeDecodeError, zlib.error, json.JSONDecodeError):
                continue
            for tweet in payload.get("tweets") or []:
                author = tweet.get("author") or {}
                handle = str(
                    author.get("userName")
                    or author.get("username")
                    or tweet.get("authorUserName")
                    or ""
                ).lstrip("@").lower()
                if handle not in monitored:
                    continue
                counts[handle] += 1
                created = parse_date(tweet.get("createdAt") or tweet.get("created_at"))
                if created and created.isoformat() > latest.get(handle, ""):
                    latest[handle] = created.isoformat()
    finally:
        db.close()
    return counts, latest


async def inspect_quiet(
    handles: list[str], api_key: str, *, concurrency: int
) -> dict[str, dict[str, object]]:
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, dict[str, object]] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        async def inspect(handle: str) -> None:
            async with semaphore:
                for attempt in range(3):
                    try:
                        response = await client.get(
                            TIMELINE_ENDPOINT,
                            headers={"X-API-Key": api_key},
                            params={"userName": handle, "includeReplies": "true"},
                        )
                        payload = response.json()
                        if response.status_code == 429 or response.status_code >= 500:
                            raise httpx.HTTPStatusError(
                                "retryable response", request=response.request, response=response
                            )
                        tweets = payload.get("tweets") or [] if isinstance(payload, dict) else []
                        dates = [
                            parsed
                            for tweet in tweets
                            if isinstance(tweet, dict)
                            if (parsed := parse_date(tweet.get("createdAt") or tweet.get("created_at")))
                        ]
                        status = str(payload.get("status") or "") if isinstance(payload, dict) else ""
                        message = str(
                            payload.get("message") or payload.get("msg") or payload.get("error") or ""
                        ) if isinstance(payload, dict) else ""
                        results[handle.lower()] = {
                            "http_status": response.status_code,
                            "provider_status": status,
                            "message": message,
                            "latest_post_at": max(dates).isoformat() if dates else None,
                            "posts_returned": len(tweets),
                        }
                        return
                    except (httpx.HTTPError, ValueError) as exc:
                        if attempt == 2:
                            results[handle.lower()] = {
                                "http_status": None,
                                "provider_status": "request_failed",
                                "message": type(exc).__name__,
                                "latest_post_at": None,
                                "posts_returned": 0,
                            }
                        else:
                            await asyncio.sleep(1.5 * (attempt + 1))

        await asyncio.gather(*(inspect(handle) for handle in handles))
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inactive-days", type=int, default=180)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--accounts", type=Path, default=ROOT / "resources/x_accounts_j7.txt")
    parser.add_argument("--database", type=Path, default=ROOT / "data/brief.db")
    parser.add_argument("--output", type=Path, default=ROOT / "output/x-account-audit.json")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    api_key = os.getenv("TWITTERAPI_IO_KEY", "").strip()
    if not api_key:
        raise SystemExit("TWITTERAPI_IO_KEY is missing")

    handles = [
        line.strip().lstrip("@")
        for line in args.accounts.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    invalid = [handle for handle in handles if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle)]
    monitored = {handle.lower() for handle in handles}
    counts, archived_latest = archived_activity(args.database, monitored)
    quiet = [handle for handle in handles if handle.lower() not in counts]
    checked = await inspect_quiet(quiet, api_key, concurrency=max(1, args.concurrency))

    cutoff = datetime.now(UTC) - timedelta(days=args.inactive_days)
    long_inactive: list[str] = []
    broken: list[str] = []
    uncertain: list[str] = []
    for handle in quiet:
        result = checked.get(handle.lower(), {})
        latest = parse_date(result.get("latest_post_at"))
        message = str(result.get("message") or "").lower()
        failed = result.get("provider_status") == "request_failed"
        unavailable = any(word in message for word in ("not found", "suspend", "unavailable", "does not exist"))
        if unavailable:
            broken.append(handle)
        elif latest and latest < cutoff:
            long_inactive.append(handle)
        elif failed or not latest:
            uncertain.append(handle)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "inactive_cutoff_days": args.inactive_days,
        "inactive_before": cutoff.isoformat(),
        "summary": {
            "configured": len(handles),
            "recently_active": len(counts),
            "quiet_checked": len(quiet),
            "long_inactive": len(long_inactive),
            "broken": len(broken),
            "uncertain": len(uncertain),
            "safe_to_remove": len(set(long_inactive + broken + invalid)),
        },
        "safe_to_remove": sorted(set(long_inactive + broken + invalid), key=str.lower),
        "long_inactive": sorted(long_inactive, key=str.lower),
        "broken": sorted(broken, key=str.lower),
        "invalid_syntax": sorted(invalid, key=str.lower),
        "uncertain": sorted(uncertain, key=str.lower),
        "archived_latest": archived_latest,
        "quiet_checks": checked,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
