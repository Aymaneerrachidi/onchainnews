"""Run an X-only audit against the saved runner universe.

This command does not discover tokens, refresh market data, perform browser
research, mutate the public snapshot, or deliver a Discord/email message.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import zlib


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brief.config import load_settings
from brief.ledger import open_ledger
from brief.models import XPost, integer
from brief.sources.http import CachedHttpClient
from brief.sources.social import match_x_interactions
from brief.sources.x import (
    TwitterApiIoSource,
    _twitterapi_datetime,
    _twitterapi_interaction,
    _twitterapi_urls,
    candidate_search_terms,
    load_x_accounts,
)
from scripts.enrich_existing import _candidate, _interaction_rows


async def run(
    source_path: Path,
    output_path: Path,
    hours: int,
    limit: int,
    strategy: str,
    timeline_pages: int,
    window_start: datetime | None,
    window_end: datetime | None,
    seed_raw_start: int,
    seed_raw_end: int,
    timeline_start_group: int,
    merge_snapshot: bool,
) -> None:
    settings = load_settings(ROOT / "config.toml")
    snapshot = json.loads(source_path.read_text(encoding="utf-8"))
    rows = list(snapshot.get("runnerUniverse") or [])
    if limit > 0:
        rows = rows[:limit]
    if not rows:
        raise RuntimeError(f"no runnerUniverse rows in {source_path}")

    candidates = [_candidate(row, None) for row in rows]
    raw_candidates = deepcopy(candidates)
    x = settings.section("x")
    urls = settings.section("sources")
    cache = settings.section("cache")
    accounts = load_x_accounts(
        [str(handle) for handle in (x.get("accounts") or [])],
        [str(path) for path in (x.get("account_files") or [])],
        root=settings.root,
    )

    ledger = open_ledger(settings)
    http = CachedHttpClient(
        ledger,
        timeout=float(settings.get("run", "request_timeout_seconds", 15)),
    )
    try:
        source = TwitterApiIoSource(
            http,
            str(urls.get(
                "twitterapi_io_search_url",
                "https://api.twitterapi.io/twitter/tweet/advanced_search",
            )),
            os.getenv("TWITTERAPI_IO_KEY"),
            accounts,
            ttl=int(cache.get("x_ttl_seconds", 300)),
            requests_per_minute=int(x.get("requests_per_minute", 60)),
            accounts_per_query=int(x.get("accounts_per_query", 20)),
            max_pages_per_query=int(x.get("max_pages_per_query", 5)),
        )
        if not source.configured:
            raise RuntimeError("TWITTERAPI_IO_KEY or monitored account list is missing")

        end = (window_end or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = (
            window_start.astimezone(timezone.utc)
            if window_start is not None
            else end - timedelta(hours=max(1, hours))
        )
        post_map: dict[str, XPost] = {}
        if seed_raw_start > 0 and seed_raw_end >= seed_raw_start:
            allowed = set(source.accounts)
            archived = ledger.db.execute(
                "SELECT response_body FROM raw_responses "
                "WHERE id BETWEEN ? AND ? AND endpoint = ? ORDER BY id",
                (seed_raw_start, seed_raw_end, source.endpoint),
            ).fetchall()
            for (body,) in archived:
                try:
                    payload = json.loads(zlib.decompress(body))
                except (TypeError, ValueError, zlib.error):
                    try:
                        payload = json.loads(body)
                    except (TypeError, ValueError):
                        continue
                for raw in payload.get("tweets") or []:
                    if not isinstance(raw, dict):
                        continue
                    created_at = _twitterapi_datetime(
                        raw.get("createdAt") or raw.get("created_at")
                    )
                    author = raw.get("author") or {}
                    handle = str(
                        author.get("userName") or author.get("username")
                        or raw.get("authorUserName") or ""
                    ).lstrip("@")
                    post_id = str(raw.get("id") or raw.get("tweetId") or "")
                    if (
                        created_at is None or created_at < start or created_at > end
                        or handle.lower() not in allowed or not post_id
                    ):
                        continue
                    post_map[post_id] = XPost(
                        post_id=post_id,
                        author_id=str(author.get("id") or raw.get("authorId") or ""),
                        author_handle=handle,
                        author_name=str(author.get("name") or handle),
                        text=str(raw.get("text") or ""),
                        created_at=created_at,
                        interaction=_twitterapi_interaction(raw),
                        url=str(raw.get("url") or f"https://x.com/{handle}/status/{post_id}"),
                        like_count=integer(raw.get("likeCount")),
                        repost_count=integer(raw.get("retweetCount")),
                        reply_count=integer(raw.get("replyCount")),
                        quote_count=integer(raw.get("quoteCount")),
                        expanded_urls=_twitterapi_urls(raw),
                        author_followers=integer(author.get("followers")),
                        author_verified=bool(
                            author.get("isBlueVerified") or author.get("isVerified")
                        ),
                        conversation_id=str(raw.get("conversationId") or ""),
                    )
        if strategy in {"terms", "both"}:
            for post in await source.posts_for_terms(
                start,
                [candidate_search_terms(candidate) for candidate in candidates],
                max_pages_per_query=int(x.get("max_pages_per_term_query", 1)),
                until=end,
            ):
                post_map[post.post_id] = post
        if strategy in {"timeline", "both"}:
            source.max_pages_per_query = max(1, timeline_pages)
            if timeline_start_group > 0:
                offset = timeline_start_group * source.accounts_per_query
                source.accounts = source.accounts[offset:]
            for post in await source.posts(start, until=end):
                post_map[post.post_id] = post
        posts = sorted(post_map.values(), key=lambda post: post.created_at, reverse=True)
        common = {
            "max_per_token": int(x.get("max_matches_per_token", 6)),
            "editorial_accounts": x.get("editorial_accounts", []),
            "internal_only_accounts": x.get("internal_only_accounts", []),
        }
        match_x_interactions(raw_candidates, posts, informative_only=False, **common)
        match_x_interactions(candidates, posts, informative_only=True, **common)

        coin_rows = []
        for row, raw_candidate, candidate in zip(rows, raw_candidates, candidates):
            coin_rows.append({
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "chain": row.get("chain"),
                "mint": row.get("mint"),
                "searchTerms": candidate_search_terms(candidate),
                "rawMatches": _interaction_rows(raw_candidate),
                "informativeMatches": _interaction_rows(candidate),
                "internalMatches": [
                    {
                        "author": item.author_name,
                        "handle": item.author_handle,
                        "interaction": item.interaction,
                        "summary": item.summary,
                        "url": item.url,
                        "createdAt": item.created_at.isoformat(),
                        "confidence": item.confidence,
                        "matchedOn": item.matched_on,
                    }
                    for item in candidate.internal_x_leads
                ],
            })

        raw_matched = sum(bool(item["rawMatches"] or item["internalMatches"]) for item in coin_rows)
        informative_matched = sum(
            bool(item["informativeMatches"] or item["internalMatches"])
            for item in coin_rows
        )
        result = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "windowHours": hours,
            "windowStart": start.isoformat(),
            "windowEnd": end.isoformat(),
            "strategy": strategy,
            "timelinePages": timeline_pages if strategy in {"timeline", "both"} else 0,
            "runnerCount": len(rows),
            "monitoredHandles": len(accounts),
            "postsRead": len(posts),
            "rawMatchedCoins": raw_matched,
            "informativeMatchedCoins": informative_matched,
            "coins": coin_rows,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if merge_snapshot:
            result["snapshotRowsEnriched"] = merge_audit_into_snapshot(snapshot, result)
            source_path.write_text(
                json.dumps(snapshot, ensure_ascii=True, indent=1), encoding="utf-8"
            )
        print(json.dumps({key: value for key, value in result.items() if key != "coins"}))
    finally:
        await http.close()
        ledger.close()


def merge_audit_into_snapshot(snapshot: dict, audit: dict) -> int:
    """Merge fresh public matches into both indexes used by Discord buttons."""
    by_mint = {
        str(coin.get("mint") or "").strip().lower(): coin
        for coin in audit.get("coins") or []
        if isinstance(coin, dict) and coin.get("mint")
    }
    merged = 0
    for collection in ("runnerUniverse", "runners"):
        for row in snapshot.get(collection) or []:
            coin = by_mint.get(str(row.get("mint") or "").strip().lower())
            if not coin:
                continue
            interactions = [
                item for item in (coin.get("informativeMatches") or [])
                if isinstance(item, dict) and item.get("url") and item.get("summary")
            ]
            # The new 24-hour audit is authoritative. Empty means yesterday's
            # social context must disappear rather than leak into today's view.
            row["xInteractions"] = interactions
            normal_lore = str(row.get("lore") or "").strip()
            if normal_lore.startswith("X: ") and " Lore: " in normal_lore:
                normal_lore = normal_lore.split(" Lore: ", 1)[1]
            if not interactions:
                row["lore"] = normal_lore
                continue
            first = interactions[0]
            summary = str(first.get("summary") or "").strip()
            source = str(first.get("url") or "").strip()
            x_context = summary if summary.startswith("X:") else f"X: {summary}"
            combined = f"{x_context} Lore: {normal_lore}" if normal_lore else x_context
            row["lore"] = combined
            provider = dict(row.get("providerEvidence") or {})
            provider["why"] = {"cause": combined, "sourceUrl": source}
            row["providerEvidence"] = provider
            merged += 1
    snapshot["xAudit"] = {
        key: audit.get(key) for key in (
            "generatedAt", "windowStart", "windowEnd", "strategy",
            "runnerCount", "monitoredHandles", "postsRead",
            "rawMatchedCoins", "informativeMatchedCoins",
        )
    }
    return merged


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=ROOT / "web" / "data" / "latest.json"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output" / "x-audit-current.json"
    )
    parser.add_argument("--hours", type=int, default=36)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--strategy", choices=("terms", "timeline", "both"), default="both"
    )
    parser.add_argument("--timeline-pages", type=int, default=2)
    parser.add_argument("--window-start", type=datetime.fromisoformat)
    parser.add_argument("--window-end", type=datetime.fromisoformat)
    parser.add_argument("--seed-raw-start", type=int, default=0)
    parser.add_argument("--seed-raw-end", type=int, default=0)
    parser.add_argument("--timeline-start-group", type=int, default=0)
    parser.add_argument("--merge-snapshot", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(
        args.source.resolve(), args.output.resolve(), args.hours, args.limit,
        args.strategy, args.timeline_pages, args.window_start, args.window_end,
        args.seed_raw_start, args.seed_raw_end, args.timeline_start_group,
        args.merge_snapshot,
    ))


if __name__ == "__main__":
    main()
