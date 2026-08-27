from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from brief.models import Candidate, XPost, integer
from brief.sources.http import CachedHttpClient


_HANDLE_LINE = re.compile(r"^@?([A-Za-z0-9_]{1,64})$")


def load_x_accounts(
    accounts: list[str] | tuple[str, ...],
    account_files: list[str] | tuple[str, ...] = (),
    *,
    root: str | Path = ".",
) -> list[str]:
    """Merge inline handles with operator-supplied account files.

    Account files contain one ``@handle`` per line. They are deliberately kept
    separate from ``config.toml`` so large tracker exports remain reviewable and
    can be refreshed without replacing the hand-curated fallback desk.
    """
    merged = [str(handle).strip().lstrip("@") for handle in accounts]
    base = Path(root)
    for configured_path in account_files:
        path = Path(str(configured_path))
        if not path.is_absolute():
            path = base / path
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = _HANDLE_LINE.fullmatch(line)
            if match:
                merged.append(match.group(1))
    return list(dict.fromkeys(handle.lower() for handle in merged if handle))


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def candidate_search_terms(candidate: Candidate) -> list[str]:
    """Build exact, low-noise TwitterAPI.io searches for one token."""
    token = candidate.token
    terms = [token.mint]
    symbol = token.symbol.strip().lstrip("$")
    if len(symbol) >= 4 and not symbol.isdigit():
        terms.append(f"${symbol}")
    name = " ".join(token.name.split()).strip()
    name_is_specific = len(name) >= 5 or (
        len(name) >= 2 and any(ord(character) > 127 for character in name)
    )
    if name_is_specific:
        # Quoting preserves multi-word meme names and gives the timeline audit
        # a chance to find posts that omit the cashtag and contract address.
        terms.append(f'"{name.replace(chr(34), "")}"')
    for social in token.socials:
        kind = str(social.get("type") or "").lower()
        url = str(social.get("url") or "")
        if kind not in {"twitter", "x"} and "x.com/" not in url and "twitter.com/" not in url:
            continue
        if "/status/" in url or "/communities/" in url:
            continue
        path = url.split("x.com/", 1)[-1].split("twitter.com/", 1)[-1]
        handle = path.split("?", 1)[0].strip("/@").split("/", 1)[0]
        if handle and handle.lower() not in {"i", "intent", "share", "home", "search"}:
            terms.append(f"@{handle}")
    return list(dict.fromkeys(terms))


def candidate_lore_search_terms(candidate: Candidate) -> list[str]:
    """Build broad X discovery queries for a token's origin and story.

    These searches intentionally cover authors outside the monitored desk.
    Their results are still only *leads*: ``social.match_x_interactions`` keeps
    ticker/name matches out of publishable lore until the exact contract,
    Dexscreener URL, or linked official account confirms the identity.
    """
    token = candidate.token
    symbol = token.symbol.strip().lstrip("$")
    name = " ".join(token.name.split()).strip().replace('"', "")
    terms: list[str] = []
    if token.mint:
        terms.extend((token.mint, f'{token.mint} lore', f'{token.mint} story'))
    if symbol and name and name.casefold() != symbol.casefold():
        identity = f'${symbol} "{name}"'
        terms.extend((identity, f'{identity} lore', f'{identity} story'))
    if symbol and len(symbol) >= 2 and not symbol.isdigit():
        terms.extend((f'${symbol} lore', f'${symbol} story'))
    if name and name.casefold() != symbol.casefold() and len(name) >= 4:
        terms.extend((f'"{name}" lore', f'"{name}" story'))
    return list(dict.fromkeys(terms))


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


def _twitterapi_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def _twitterapi_urls(post: dict[str, Any]) -> tuple[str, ...]:
    entities = post.get("entities") or {}
    result: list[str] = []
    for item in entities.get("urls") or []:
        if not isinstance(item, dict):
            continue
        value = (
            item.get("expanded_url") or item.get("expandedUrl")
            or item.get("unwound_url") or item.get("url")
        )
        if value and value not in result:
            result.append(str(value))
    return tuple(result)


def _twitterapi_interaction(post: dict[str, Any]) -> str:
    if post.get("retweeted_tweet") or post.get("retweetedTweet"):
        return "reposted"
    if post.get("quoted_tweet") or post.get("quotedTweet"):
        return "quoted"
    if post.get("isReply") or post.get("is_reply") or post.get("inReplyToId"):
        return "replied"
    return "posted"


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


class TwitterApiIoSource:
    """Read a bounded account list through TwitterAPI.io advanced search.

    Handles are grouped into OR queries so a daily scan does not spend one
    request per account. The provider remains evidence ingestion only: exact
    token matching and editorial-quality checks happen in ``social.py``.
    """

    def __init__(
        self,
        http: CachedHttpClient,
        endpoint: str,
        api_key: str | None,
        accounts: list[str],
        *,
        ttl: int = 300,
        requests_per_minute: int = 60,
        accounts_per_query: int = 20,
        max_pages_per_query: int = 5,
    ) -> None:
        self.http = http
        self.endpoint = endpoint
        self.api_key = (api_key or "").strip()
        # Preserve the client's full list while preventing an accidental exact
        # duplicate from billing the same search twice.
        self.accounts = list(dict.fromkeys(
            handle.strip().lstrip("@").lower() for handle in accounts if handle.strip()
        ))
        self.ttl = ttl
        self.requests_per_minute = requests_per_minute
        self.accounts_per_query = max(1, accounts_per_query)
        self.max_pages_per_query = max(1, max_pages_per_query)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.accounts)

    async def _search(
        self,
        query: str,
        start: datetime,
        *,
        max_pages: int,
        until: datetime | None = None,
        allow_any_author: bool = False,
    ) -> list[XPost]:
        """Run one provider search and keep only the configured accounts.

        TwitterAPI.io's advanced search is global.  The caller may therefore
        search exact token identifiers without building a very long 109-way
        ``from:`` expression; the allow-list is enforced again here before a
        post can enter the evidence set.
        """
        if not self.api_key or (not allow_any_author and not self.accounts):
            return []
        start = start.astimezone(timezone.utc)
        until = (until or datetime.now(timezone.utc)).astimezone(timezone.utc)
        headers = {"X-API-Key": self.api_key}
        allowed = set(self.accounts)
        collected: dict[str, XPost] = {}
        cursor = ""
        bounded_query = (
            f"({query}) since_time:{int(start.timestamp())} "
            f"until_time:{int(until.timestamp())}"
        )
        for _ in range(max(1, max_pages)):
            params: dict[str, Any] = {"query": bounded_query, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor
            payload = await self.http.get_json(
                self.endpoint,
                family="twitterapi-advanced-search",
                limit=self.requests_per_minute,
                ttl=self.ttl,
                headers=headers,
                params=params,
            )
            if not isinstance(payload, dict):
                break
            for raw in payload.get("tweets") or []:
                if not isinstance(raw, dict):
                    continue
                created_at = _twitterapi_datetime(raw.get("createdAt") or raw.get("created_at"))
                if created_at is None or created_at < start:
                    continue
                author = raw.get("author") or {}
                handle = str(
                    author.get("userName") or author.get("username")
                    or raw.get("authorUserName") or ""
                ).lstrip("@")
                if not allow_any_author and handle.lower() not in allowed:
                    continue
                post_id = str(raw.get("id") or raw.get("tweetId") or "")
                if not post_id:
                    continue
                url = str(raw.get("url") or f"https://x.com/{handle}/status/{post_id}")
                collected[post_id] = XPost(
                    post_id=post_id,
                    author_id=str(author.get("id") or raw.get("authorId") or ""),
                    author_handle=handle,
                    author_name=str(author.get("name") or handle),
                    text=str(raw.get("text") or ""),
                    created_at=created_at,
                    interaction=_twitterapi_interaction(raw),
                    url=url,
                    like_count=integer(raw.get("likeCount")),
                    repost_count=integer(raw.get("retweetCount")),
                    reply_count=integer(raw.get("replyCount")),
                    quote_count=integer(raw.get("quoteCount")),
                    expanded_urls=_twitterapi_urls(raw),
                    author_followers=integer(author.get("followers")),
                    author_verified=bool(author.get("isBlueVerified") or author.get("isVerified")),
                    conversation_id=str(raw.get("conversationId") or ""),
                )
            cursor = str(payload.get("next_cursor") or "")
            if not payload.get("has_next_page") or not cursor:
                break
        return sorted(collected.values(), key=lambda post: post.created_at, reverse=True)

    async def posts_for_terms(
        self,
        start: datetime,
        term_groups: list[list[str]],
        *,
        max_pages_per_query: int = 1,
        until: datetime | None = None,
        allow_any_author: bool = False,
    ) -> list[XPost]:
        """Search token-specific terms, then enforce the monitored-handle list.

        Each inner list belongs to one token (normally its exact contract,
        cashtag and official handle).  This prevents a recent-timeline slice
        from hiding a relevant post behind unrelated output from busy accounts.
        """
        collected: dict[str, XPost] = {}
        for terms in term_groups:
            clean = [str(term).strip() for term in terms if str(term).strip()]
            if not clean:
                continue
            query = " OR ".join(dict.fromkeys(clean))
            for post in await self._search(
                query, start, max_pages=max_pages_per_query, until=until,
                allow_any_author=allow_any_author,
            ):
                collected[post.post_id] = post
        return sorted(collected.values(), key=lambda post: post.created_at, reverse=True)

    async def posts(
        self, start: datetime, *, until: datetime | None = None
    ) -> list[XPost]:
        if not self.configured:
            return []
        collected: dict[str, XPost] = {}
        for group in _chunks(self.accounts, self.accounts_per_query):
            accounts_query = " OR ".join(f"from:{handle}" for handle in group)
            for post in await self._search(
                accounts_query,
                start,
                max_pages=self.max_pages_per_query,
                until=until,
            ):
                collected[post.post_id] = post
        return sorted(collected.values(), key=lambda post: post.created_at, reverse=True)
