from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import re
from urllib.parse import quote

import httpx

from brief.models import Candidate, XInteraction


_STATUS = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([^/]+)/status/(\d+)", re.I)
_TAGS = re.compile(r"<[^>]+>")


def linked_x_statuses(candidate: Candidate) -> list[tuple[str, str, str]]:
    """Return unique exact X posts attached to the token's market profile."""
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for social in candidate.token.socials or []:
        url = str((social or {}).get("url") if isinstance(social, dict) else social or "")
        match = _STATUS.search(url)
        if not match or match.group(2) in seen:
            continue
        seen.add(match.group(2))
        found.append((match.group(1).lstrip("@"), match.group(2), match.group(0)))
    return found


def _oembed_text(markup: str) -> str:
    paragraph = re.search(r"<p[^>]*>(.*?)</p>", markup or "", re.I | re.S)
    if not paragraph:
        return ""
    text = re.sub(r"<a[^>]*>.*?</a>", "", paragraph.group(1), flags=re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", text))).strip()


async def attach_linked_x_posts(
    candidates: list[Candidate], *, api_key: str | None = None,
    timeout: float = 20.0, concurrency: int = 8
) -> int:
    """Resolve Dex/GMGN-attached X posts, preferring TwitterAPI.io.

    These links are exact-contract metadata leads, not proof that the author
    launched or endorsed the token. The writer receives the verbatim post and
    must preserve any caveats in it.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    attached = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async def twitterapi_post(handle: str, post_id: str) -> dict | None:
            if not api_key:
                return None
            try:
                response = await client.get(
                    "https://api.twitterapi.io/twitter/tweets",
                    headers={"X-API-Key": api_key},
                    params={"tweet_ids": post_id},
                )
                if response.status_code != 200:
                    return None
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return None
            for row in payload.get("tweets") or []:
                if str(row.get("id") or row.get("tweetId") or "") == post_id:
                    return row
            return None

        async def oembed_post(handle: str, post_id: str) -> dict | None:
            endpoint = (
                "https://publish.twitter.com/oembed?omit_script=true&url="
                + quote(f"https://x.com/{handle}/status/{post_id}", safe="")
            )
            try:
                response = await client.get(endpoint)
                return response.json() if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                return None

        async def one(candidate: Candidate, handle: str, post_id: str, url: str) -> None:
            nonlocal attached
            if any(item.url.rstrip("/") == url.rstrip("/") for item in candidate.x_interactions):
                return
            payload = await twitterapi_post(handle, post_id)
            provider = "TwitterAPI.io"
            summary = str((payload or {}).get("text") or "").strip()
            if not summary:
                payload = await oembed_post(handle, post_id)
                provider = "X oEmbed fallback"
                summary = _oembed_text(str((payload or {}).get("html") or ""))
            if not summary:
                return
            created_at = datetime.now(timezone.utc)
            raw_created = str((payload or {}).get("createdAt") or (payload or {}).get("created_at") or "")
            if raw_created:
                try:
                    created_at = datetime.strptime(raw_created, "%a %b %d %H:%M:%S %z %Y")
                except ValueError:
                    try:
                        created_at = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
                    except ValueError:
                        pass
            author = (payload or {}).get("author") or {}
            candidate.x_interactions.append(XInteraction(
                author_handle=handle,
                author_name=str(author.get("name") or (payload or {}).get("author_name") or handle),
                interaction="linked post",
                summary=summary,
                url=f"https://x.com/{handle}/status/{post_id}",
                created_at=created_at,
                confidence="confirmed",
                matched_on=f"exact-contract Dexscreener/GMGN social link via {provider}",
                like_count=int((payload or {}).get("likeCount") or 0),
                repost_count=int((payload or {}).get("retweetCount") or 0),
                reply_count=int((payload or {}).get("replyCount") or 0),
                quote_count=int((payload or {}).get("quoteCount") or 0),
            ))
            attached += 1

        jobs = []
        for candidate in candidates:
            for handle, post_id, url in linked_x_statuses(candidate):
                async def guarded(c=candidate, h=handle, p=post_id, u=url):
                    async with semaphore:
                        await one(c, h, p, u)
                jobs.append(guarded())
        if jobs:
            await asyncio.gather(*jobs)
    return attached
