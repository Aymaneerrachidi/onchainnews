from __future__ import annotations

import asyncio
import os
import re
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


# Chants, invitations and price targets. All of these get engagement -- that is
# what they are built for -- so an engagement threshold alone promotes them.
HYPE = re.compile(
    r"(to the moon|to billions|to a billion|\blfg\b|speak it (in)?to existence"
    r"|never sell|hold the line|we are so back|wagmi|gm fam|\bfamily\b"
    r"|send it|\bape (in|into)\b|next \d+x|\d+x from here|easy \d+x"
    r"|last chance|dont fade|don't fade|do not fade|still early"
    r"|get in early|early gem|hidden gem|100x|1000x"
    r"|buy now|entry|target|\btp\d?\b|\bsl\b|call channel|join (my|our|the)"
    r"|t\.me/|discord\.gg|telegram\.me|link in bio|pinned)",
    re.I,
)
# A post that is telling you something: a mechanism, a comparison, a number
# that means something, an event.
SUBSTANCE = re.compile(
    r"(because|which means|the reason|turns out|it works|mechanism|rewards?"
    r"|dividend|distribut|buyback|burn|holders?|liquidity|volume|listing"
    r"|listed|launch(ed)?|migrat|community|treasury|supply|unlock|team"
    r"|founder|partner|announce|airdrop|snapshot|exited|sold|bought|accumulat"
    r"|holding|yield|gold|rwa|claim|portal|flip|listed on|delist"
    r"|salary|dividend|revenue|renounce|locked|unlock|reflection"
    r"|culture|generation|millennial|zoomer|worldview|humou?r|joke"
    r"|thesis|because of|the point is|what makes|why it|the idea"
    r"|building|shipped|roadmap|integrat|ecosystem|adoption|dev is"
    r"|reference|meaning|meme|nostalgia|icon"
    r"|compare|versus|\bvs\b|underestimat|different|built|building)",
    re.I,
)
# The call-channel advert. Its whole purpose is engagement, so it beats every
# engagement threshold, and it is the most common post about any running coin.
# Written from real examples that got through: a private-group plug, a claimed
# entry, a multiple, and an invitation.
PROMO = re.compile(
    r"("
    # the group itself
    r"\bmy (private |vip |paid )?(tg|telegram|group|channel|discord|community)\b"
    r"|\bprivate (tg|telegram|group|channel)\b|\bjoin (tg|telegram|discord|now|us|me)\b"
    r"|link (in|on) (my )?bio|\bdm me\b|\bpinned\b|\bt\.me/|discord\.gg"
    r"|notification (on|bell)|turn on notifications"
    # the claimed call
    r"|\bcalled (it|at|this)\b|\bmy call\b|\bcall of the (day|week)\b"
    r"|\bi called\b|\bwe called\b|\bentry at\b|\bmy entry\b"
    # the claimed result
    r"|\d+(\.\d+)?\s*x\s*(gain|profit|return|from (my|our) call)"
    r"|another \d+(\.\d+)?\s*x|\d+(\.\d+)?\s*x\s+on\s+\$"
    r"|[\d.,]+\s*[kKmM]?\s*(->|=>|→)\s*[\d.,]+\s*[kKmM]"
    r"|turn(ed|ing)?\s+\$?[\d.,]+\s*[kKmM]?\s+(in)?to\s+(over\s+|more than\s+|almost\s+)?\$?[\d.,]+"
    r"|\bsold none\b|\bstill sold none\b|\bare you following\b|\bfollow me\b"
    r"|\bretire(d|s)? (you|him|her|from)\b|\bmassive wins?\b|\binsane (gain|profit)"
    r"|\bprofits? secured\b|\bsecuring (massive )?wins?\b"
    # the invitation
    r"|\bdont miss\b|\bdon't miss\b|\bnext one\b|\bwho[' ]?s next\b"
    r"|\bif you believe in me\b|\btrust me\b|\bfollow for more\b"
    r")",
    re.I,
)
# Wallet-watching bots. They post on every running coin and report a
# stranger moving money, which is never the reason the coin ran.
WALLET_TRACKER = re.compile(
    r"(this (whale|wallet|guy|trader|address)\b"
    r"|\bwhale (just |has )?(bought|sold|added|dumped|exited|accumulated)"
    r"|(bought|sold|added|dumped|grabbed)\s+\$[\d.,]+\s*[kKmM]?"
    r"|\bjust came back with\b|\bcame back for\b"
    r"|\d+\s*buys?,?\s+\d+\s*sells?"
    r"|\bon the book\b|\bin one clip\b"
    r"|\bfresh wallet\b|\bnew wallet (bought|opened)"
    r"|\bsmart money (bought|sold|is buying|is selling)\b"
    r"|\bholder has (finally )?(completely )?(exited|sold)"
    r"|\bpaperhand|\bdiamond ?hand)",
    re.I,
)

# A wall of rocket and fire emoji is an advert whatever the words say.
CONTRACT_PASTE = re.compile(
    r"(\bca\b\s*[:=]|\bcontract\b\s*[:=]|0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}pump\b)",
    re.I,
)
EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2764]"
)
CASHTAG = re.compile(r"[$#][A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff]*")
URL = re.compile(r"https?://\S+")


PRICE_TICK = re.compile(
    r"(\b(OI|oi)\s*\d+\s*(min|m|h)\b|\b\d+s\b.{0,12}(up|down)\s*\d"
    r"|(up|down)\s*\d+(\.\d+)?%\s*\$?[\d.,]+[KMB]?\s*(drop|rise|move)"
    r"|liquidation|funding rate|open interest)",
    re.I,
)


# A post that reports an event explains the run. A post that praises the coin
# does not, however well written it is.
CAUSAL_POST = re.compile(
    r"(listed on|listing|now live|announce|launch(ed|ing)?|partnership"
    r"|collab|acquired|integrat|burn(ed|t)?|locked|airdrop|snapshot"
    r"|takeover|rebrand|migrat|upgrade|shipped|went viral|posted about"
    r"|picked up by|featured|added to|support(s|ed)? by)",
    re.I,
)


def post_substance(text: str) -> int:
    """Words left once tickers, links, mentions and emoji are taken out.

    "All things are easier together. $CETS https://t.co/..." looks like a
    sentence and carries nothing; stripping the furniture is what exposes it.
    """
    body = URL.sub(" ", text or "")
    body = CASHTAG.sub(" ", body)
    body = re.sub(r"@\w+", " ", body)
    body = re.sub(r"[^A-Za-z\u4e00-\u9fff ]+", " ", body)
    latin = [word for word in body.split() if len(word) > 2 and word.isascii()]
    # Chinese and Japanese do not separate words with spaces, so counting on
    # whitespace reported a substantial post as empty and dropped it. Roughly
    # two characters to a word.
    cjk = len(re.findall(r"[一-鿿]", body)) // 2
    return len(latin) + cjk


def post_quality(
    text: str,
    followers: int,
    likes: int,
    reposts: int,
    verified: bool,
    min_reach: int = 3_000,
) -> int:
    """A score for whether this post is worth quoting in a recap.

    Engagement is necessary and nowhere near sufficient: the loudest posts
    about any running coin are chants and call-channel adverts, and they are
    engineered for exactly the numbers an engagement filter rewards.
    """
    words = post_substance(text)
    if words < 6:
        return 0
    # Reach, or engagement large enough that reach clearly followed. Without
    # this the recap quotes whoever is holding the bag hardest.
    if followers < min_reach and (likes + reposts * 2) < 80:
        return 0
    if HYPE.search(text or "") or PROMO.search(text or ""):
        return 0
    # Four or more decorative emoji is a poster, not a post.
    if len(EMOJI.findall(text or "")) >= 4:
        return 0
    # Somebody who is explaining a coin writes about it; somebody who wants you
    # to buy it pastes the address so you can.
    if CONTRACT_PASTE.search(text or ""):
        return 0
    # Somebody else's trade is not the story of the coin.
    if WALLET_TRACKER.search(text or ""):
        return 0
    # A list of tickers is somebody's portfolio, not an account of this coin.
    if len(set(CASHTAG.findall(text or ""))) > 4:
        return 0

    score = 0
    score += min(30, words)
    if CAUSAL_POST.search(text or ""):
        # Weighted above ordinary substance so the quote we keep is the one
        # that explains the move.
        score += 40
    elif SUBSTANCE.search(text or ""):
        score += 25
    if verified:
        score += 5
    score += min(20, (likes + reposts * 2) // 10)
    score += min(15, followers // 5000)
    return score


class OpenIntelSource:
    """Read-only 6551 OpenNews/OpenTwitter REST adapter."""

    def __init__(
        self,
        http: CachedHttpClient,
        base_url: str,
        ttl: int = 900,
        product: str = "Top",
        min_followers: int = 500,
        min_engagement: int = 3,
        min_quality: int = 45,
        min_reach: int = 10_000,
        min_news_score: int = 40,
        pause_seconds: float = 2.0,
    ) -> None:
        self.min_news_score = min_news_score
        # The provider answers "the skill is being invoked too frequently"
        # well before the monthly quota is touched, and a failed query means a
        # coin ships with no evidence at all. Spacing the calls costs a minute
        # and recovers half the board.
        self.pause_seconds = pause_seconds
        self.min_quality = min_quality
        self.min_reach = min_reach
        self.product = product
        # A post from an account nobody follows, that nobody engaged with, is
        # not evidence of anything. These keep the shill floor out.
        self.min_followers = min_followers
        self.min_engagement = min_engagement
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

    async def trusted_timeline(
        self,
        candidates: list[Candidate],
        accounts: list[str],
        now: datetime,
        *,
        max_per_account: int = 30,
        window_hours: float = 36.0,
    ) -> tuple[int, list[str]]:
        """Pull each trusted account's recent posts and match them to our coins.

        Filtering the open firehose means judging strangers on their wording,
        which is a losing game: a call channel writes better copy than an
        analyst. Reading a fixed list of accounts inverts it. One request per
        account covers every coin at once, so thirty accounts cost thirty
        calls rather than thirty per coin.
        """
        if not self.configured or not accounts:
            return 0, []
        by_ticker: dict[str, Candidate] = {}
        by_mint: dict[str, Candidate] = {}
        for candidate in candidates:
            symbol = str(candidate.token.symbol or "").strip().lower()
            if symbol:
                by_ticker.setdefault(symbol, candidate)
            mint = str(candidate.token.mint or "").strip().lower()
            if mint:
                by_mint[mint] = candidate

        matched = 0
        failures: list[str] = []
        for index, handle in enumerate(accounts):
            handle = str(handle).strip().lstrip("@")
            if not handle:
                continue
            if index and self.pause_seconds:
                await asyncio.sleep(self.pause_seconds)
            try:
                payload = await self.http.post_json(
                    f"{self.base_url}/open/twitter_user_tweets",
                    family="opentwitter",
                    limit=30,
                    ttl=self.ttl,
                    headers=self.headers,
                    json_body={
                        "username": handle,
                        "maxResults": max_per_account,
                        "product": "Latest",
                        "includeReplies": False,
                        "includeRetweets": False,
                    },
                )
            except Exception as exc:
                failures.append(f"{handle}: {str(exc)[:60]}")
                continue

            for row in _items(payload):
                text = str(row.get("text") or "")
                if not text:
                    continue
                created = _dt(row.get("createdAt") or row.get("created_at"))
                if created and (now - created).total_seconds() > window_hours * 3600:
                    continue

                hit: Candidate | None = None
                lowered = text.lower()
                for mint, candidate in by_mint.items():
                    if mint in lowered:
                        hit = candidate
                        break
                if hit is None:
                    for token in re.findall(r"[$#]([A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff]{1,14})", text):
                        candidate = by_ticker.get(token.lower())
                        if candidate is not None:
                            hit = candidate
                            break
                if hit is None:
                    continue

                post_id = str(row.get("id") or row.get("tweetId") or "")
                hit.x_interactions.append(XInteraction(
                    author_handle=handle,
                    author_name=str(row.get("userName") or handle),
                    interaction="post",
                    summary=text[:280],
                    url=str(row.get("url") or (f"https://x.com/{handle}/status/{post_id}" if post_id else "")),
                    created_at=created or now,
                    confidence="high",
                    matched_on="trusted account",
                    like_count=int(row.get("favoriteCount") or 0),
                    repost_count=int(row.get("retweetCount") or 0),
                    reply_count=int(row.get("replyCount") or 0),
                ))
                matched += 1
        return matched, failures

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
        rate_limited = 0
        for index, candidate in enumerate(candidates[:limit]):
            if index and self.pause_seconds:
                await asyncio.sleep(self.pause_seconds)
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
                    json_body={
                        "q": keywords,
                        "limit": 20,
                        "page": 1,
                        # The provider's own relevance score. Without it the
                        # feed is mostly automated price ticks.
                        "score": self.min_news_score,
                        "hasCoin": True,
                    },
                )
                for row in _items(news_payload):
                    searchable = " ".join(str(row.get(key) or "") for key in ("title", "text", "summary_en", "link"))
                    if token.mint not in searchable and f"${token.symbol}".lower() not in searchable.lower():
                        continue
                    published = _dt(row.get("ts") or row.get("published_at") or row.get("createdAt"))
                    if published and (published > now or (now - published).total_seconds() > 30 * 3600):
                        continue
                    headline = str(row.get("title") or row.get("text") or "")
                    if PRICE_TICK.search(headline):
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
                if "HTTP 429" in str(exc):
                    # Back off and let the next coin through rather than
                    # closing the lane for the rest of the run.
                    rate_limited += 1
                    await asyncio.sleep(self.pause_seconds * 3)
                elif "HTTP 402" in str(exc):
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
                        "product": str(self.product),
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
                    followers = int(row.get("userFollowers") or 0)
                    quality = post_quality(
                        post_text,
                        followers,
                        int(row.get("favoriteCount") or 0),
                        int(row.get("retweetCount") or 0),
                        bool(row.get("userVerified")),
                        min_reach=self.min_reach,
                    )
                    if quality < self.min_quality:
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
                if "HTTP 429" in str(exc):
                    # Back off and let the next coin through rather than
                    # closing the lane for the rest of the run.
                    rate_limited += 1
                    await asyncio.sleep(self.pause_seconds * 3)
                elif "HTTP 402" in str(exc):
                    twitter_available = False
        # Keep the posts a reader would care about: reach times engagement.
        for candidate in candidates[:limit]:
            if len(candidate.x_interactions) > 1:
                candidate.x_interactions.sort(
                    key=lambda post: (
                        1 if CAUSAL_POST.search(post.summary or "") else 0,
                        post.like_count + post.repost_count * 2,
                    ),
                    reverse=True,
                )
                # Cap each account at two posts. One prolific holder posted
                # eight of a coin's fourteen surviving posts, which reads as
                # the coin's story and is really one person talking.
                seen_authors: dict[str, int] = {}
                kept = []
                for post in candidate.x_interactions:
                    handle = post.author_handle.lower()
                    if seen_authors.get(handle, 0) >= 2:
                        continue
                    seen_authors[handle] = seen_authors.get(handle, 0) + 1
                    kept.append(post)
                candidate.x_interactions = kept[:6]

        return SourceStatus(
            "OpenNews/OpenTwitter token evidence",
            failures == 0,
            f"{matched_news} news matches and {matched_posts} X matches across {min(len(candidates), limit)} finalists"
            + (f"; {failures} query failures" if failures else "")
            + (f" ({rate_limited} rate limited)" if rate_limited else ""),
        )
