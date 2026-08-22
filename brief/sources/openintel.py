from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from brief.models import Candidate, SourceStatus, XInteraction
from brief.sources.http import CachedHttpClient


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "list", "tweets", "news"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class OpenIntelSource:
    """Read-only 6551 OpenNews/OpenTwitter REST adapter."""

    def __init__(self, http: CachedHttpClient, base_url: str, ttl: int = 900) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.token = os.getenv("OPENNEWS_TOKEN")
        self.ttl = ttl

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def free_market_context(self) -> tuple[list[dict[str, Any]], SourceStatus]:
        """Fetch the no-token crypto feed as a non-token-specific fallback."""
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/open/free_hot",
                family="opennews-free",
                limit=20,
                ttl=self.ttl,
                params={"category": "crypto"},
            )
            news = ((payload or {}).get("news") or {}).get("items") or []
            tweets = ((payload or {}).get("tweets") or {}).get("items") or []
            rows = [item for item in [*news, *tweets] if isinstance(item, dict)]
            return rows, SourceStatus(
                "OpenNews free context", True, f"{len(news)} news and {len(tweets)} social items"
            )
        except Exception as exc:
            return [], SourceStatus("OpenNews free context", False, str(exc))

    async def enrich(self, candidates: list[Candidate], now: datetime, limit: int = 10) -> SourceStatus:
        if not self.configured:
            return SourceStatus(
                "OpenNews/OpenTwitter token evidence",
                False,
                "OPENNEWS_TOKEN is not configured; free broad context remains available",
            )
        failures = 0
        matched_news = 0
        matched_posts = 0
        news_available = True
        twitter_available = True
        for candidate in candidates[:limit]:
            token = candidate.token
            keywords = f'"{token.mint}" OR "${token.symbol}" OR "{token.name}"'
            try:
                if not news_available:
                    raise RuntimeError("OpenNews quota circuit open")
                news_payload = await self.http.post_json(
                    f"{self.base_url}/open/news_search",
                    family="opennews",
                    limit=30,
                    ttl=self.ttl,
                    headers=self.headers,
                    json_body={"q": keywords, "limit": 20, "page": 1},
                )
                for row in _items(news_payload):
                    searchable = " ".join(str(row.get(key) or "") for key in ("title", "text", "summary_en", "link"))
                    if token.mint not in searchable and f"${token.symbol}".lower() not in searchable.lower():
                        continue
                    published = _dt(row.get("ts") or row.get("published_at") or row.get("createdAt"))
                    if published and (published > now or (now - published).total_seconds() > 30 * 3600):
                        continue
                    rating = row.get("aiRating") or {}
                    candidate.news_evidence.append({
                        "kind": "news",
                        "source": str(row.get("newsType") or row.get("source") or "OpenNews"),
                        "summary": str(rating.get("enSummary") or row.get("summary_en") or row.get("text") or row.get("title") or "")[:280],
                        "url": str(row.get("link") or row.get("url") or ""),
                        "publishedAt": published.isoformat() if published else None,
                        "score": rating.get("score") if rating else row.get("score"),
                    })
                    matched_news += 1
            except Exception as exc:
                failures += 1
                if "HTTP 402" in str(exc) or "HTTP 429" in str(exc):
                    news_available = False
            try:
                if not twitter_available:
                    raise RuntimeError("OpenTwitter quota circuit open")
                twitter_payload = await self.http.post_json(
                    f"{self.base_url}/open/twitter_search",
                    family="opentwitter",
                    limit=30,
                    ttl=self.ttl,
                    headers=self.headers,
                    json_body={
                        "keywords": f"{token.mint} OR ${token.symbol}",
                        "sinceDate": ((now.astimezone(timezone.utc) - timedelta(hours=24)).date()).isoformat(),
                        "product": "Latest",
                        "maxResults": 50,
                        "excludeRetweets": False,
                    },
                )
                for row in _items(twitter_payload):
                    post_text = str(row.get("text") or row.get("content") or "")
                    if token.mint not in post_text and f"${token.symbol}".lower() not in post_text.lower():
                        continue
                    created = _dt(row.get("createdAt") or row.get("created_at"))
                    if created and (created > now or (now - created).total_seconds() > 30 * 3600):
                        continue
                    handle = str(row.get("userScreenName") or row.get("screenName") or row.get("author") or "unknown").lstrip("@")
                    post_id = str(row.get("id") or row.get("tweetId") or "")
                    candidate.x_interactions.append(XInteraction(
                        author_handle=handle,
                        author_name=str(row.get("userName") or row.get("name") or handle),
                        interaction=("reply" if row.get("isReply") else "quote" if row.get("isQuote") else "post"),
                        summary=post_text[:280],
                        url=str(row.get("url") or (f"https://x.com/{handle}/status/{post_id}" if post_id else "")),
                        created_at=created or now,
                        confidence="high" if token.mint in post_text else "medium",
                        matched_on="contract address" if token.mint in post_text else "ticker",
                        like_count=int(row.get("favoriteCount") or row.get("likeCount") or 0),
                        repost_count=int(row.get("retweetCount") or 0),
                        reply_count=int(row.get("replyCount") or 0),
                        quote_count=int(row.get("quoteCount") or 0),
                    ))
                    matched_posts += 1
            except Exception as exc:
                failures += 1
                if "HTTP 402" in str(exc) or "HTTP 429" in str(exc):
                    twitter_available = False
        return SourceStatus(
            "OpenNews/OpenTwitter token evidence",
            failures == 0,
            f"{matched_news} news matches and {matched_posts} X matches across {min(len(candidates), limit)} finalists"
            + (f"; {failures} query failures" if failures else ""),
        )
