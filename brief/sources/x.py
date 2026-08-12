from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from brief.models import XPost, integer
from brief.sources.http import CachedHttpClient


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _interaction(references: list[dict[str, Any]]) -> str:
    kinds = {str(item.get("type") or "") for item in references}
    if "retweeted" in kinds:
        return "reposted"
    if "quoted" in kinds:
        return "quoted"
    if "replied_to" in kinds:
        return "replied"
    return "posted"


def _urls(post: dict[str, Any]) -> tuple[str, ...]:
    entities = post.get("entities") or {}
    result: list[str] = []
    for item in entities.get("urls") or []:
        value = item.get("expanded_url") or item.get("unwound_url") or item.get("url")
        if value and value not in result:
            result.append(str(value))
    return tuple(result)


class XSource:
    """Fetch public posts from a bounded, operator-owned account list.

    The source reads posts only. It does not infer sentiment, causation or a
    recommendation. Matching and confidence labelling happen separately.
    """

    def __init__(
        self,
        http: CachedHttpClient,
        endpoint: str,
        bearer_token: str | None,
        accounts: list[str],
        *,
        ttl: int = 300,
        requests_per_minute: int = 60,
        accounts_per_query: int = 20,
        max_pages_per_query: int = 5,
    ) -> None:
        self.http = http
        self.endpoint = endpoint
        self.bearer_token = (bearer_token or "").strip()
        self.accounts = list(dict.fromkeys(
            handle.strip().lstrip("@") for handle in accounts if handle.strip()
        ))
        self.ttl = ttl
        self.requests_per_minute = requests_per_minute
        self.accounts_per_query = max(1, accounts_per_query)
        self.max_pages_per_query = max(1, max_pages_per_query)

    @property
    def configured(self) -> bool:
        return bool(self.bearer_token and self.accounts)

    async def posts(self, start: datetime) -> list[XPost]:
        if not self.configured:
            return []
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        collected: dict[str, XPost] = {}
        for group in _chunks(self.accounts, self.accounts_per_query):
            query = "(" + " OR ".join(f"from:{handle}" for handle in group) + ")"
            next_token: str | None = None
            for _ in range(self.max_pages_per_query):
                params: dict[str, Any] = {
                    "query": query,
                    "start_time": _utc(start),
                    "max_results": 100,
                    "tweet.fields": "author_id,created_at,public_metrics,referenced_tweets,entities,note_tweet",
                    "expansions": "author_id",
                    "user.fields": "id,name,username",
                }
                if next_token:
                    params["next_token"] = next_token
                payload = await self.http.get_json(
                    self.endpoint,
                    family="x-recent-search",
                    limit=self.requests_per_minute,
                    ttl=self.ttl,
                    headers=headers,
                    params=params,
                )
                users = {
                    str(user.get("id") or ""): user
                    for user in (payload.get("includes") or {}).get("users") or []
                }
                for raw in payload.get("data") or []:
                    author_id = str(raw.get("author_id") or "")
                    user = users.get(author_id) or {}
                    handle = str(user.get("username") or author_id)
                    created_raw = raw.get("created_at")
                    if not created_raw:
                        continue
                    created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                    metrics = raw.get("public_metrics") or {}
                    post_id = str(raw.get("id") or "")
                    note = raw.get("note_tweet") or {}
                    collected[post_id] = XPost(
                        post_id=post_id,
                        author_id=author_id,
                        author_handle=handle,
                        author_name=str(user.get("name") or handle),
                        text=str(note.get("text") or raw.get("text") or ""),
                        created_at=created_at,
                        interaction=_interaction(raw.get("referenced_tweets") or []),
                        url=f"https://x.com/{handle}/status/{post_id}",
                        like_count=integer(metrics.get("like_count")),
                        repost_count=integer(metrics.get("retweet_count")),
                        reply_count=integer(metrics.get("reply_count")),
                        quote_count=integer(metrics.get("quote_count")),
                        expanded_urls=_urls(raw),
                    )
                next_token = str((payload.get("meta") or {}).get("next_token") or "") or None
                if not next_token:
                    break
        return sorted(collected.values(), key=lambda post: post.created_at, reverse=True)
