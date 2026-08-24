from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import urlparse

from brief.sources.http import CachedHttpClient
from brief.models import Candidate, XInteraction, XPost


TWITTER_EPOCH_MS = 1_288_834_974_657


def x_handle(socials: list[dict[str, str]]) -> str | None:
    for social in socials:
        kind = str(social.get("type") or "").lower()
        url = str(social.get("url") or "")
        if kind not in {"twitter", "x"} and "twitter.com" not in url and "x.com" not in url:
            continue
        path = urlparse(url).path.strip("/")
        handle = path.split("/")[0].lstrip("@") if path else ""
        # X communities use /i/communities/<id>. ``i`` is a route, not a
        # screen name; sending it to the account verifier incorrectly marks a
        # perfectly valid community link as a dead project account.
        if handle and handle.lower() not in {"i", "intent", "share", "home", "search"}:
            return handle
    return None


def account_created_at(account_id: str | int) -> datetime | None:
    try:
        timestamp_ms = (int(account_id) >> 22) + TWITTER_EPOCH_MS
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class SocialVerifier:
    """Uses X's public widget metadata; no paid API and no sentiment scraping."""

    def __init__(self, http: CachedHttpClient, endpoint: str, ttl: int = 86400) -> None:
        self.http = http
        self.endpoint = endpoint
        self.ttl = ttl

    async def verify(self, token_socials: dict[str, list[dict[str, str]]], now: datetime) -> dict[str, tuple[bool | None, float | None]]:
        handles = {mint: x_handle(socials) for mint, socials in token_socials.items()}
        reverse = {handle.lower(): mint for mint, handle in handles.items() if handle}
        if not reverse:
            return {mint: (None, None) for mint in token_socials}
        result = {mint: (False, None) if handle else (None, None) for mint, handle in handles.items()}
        unique = list(reverse)
        for index in range(0, len(unique), 100):
            chunk = unique[index:index + 100]
            try:
                payload = await self.http.get_json(
                    self.endpoint,
                    params={"screen_names": ",".join(chunk)},
                    family="x-public-metadata",
                    limit=60,
                    ttl=self.ttl,
                )
            except Exception:
                for handle in chunk:
                    result[reverse[handle]] = (None, None)
                continue
            accounts = payload if isinstance(payload, list) else []
            # An empty batch from the undocumented public widget is more likely
            # endpoint degradation than proof that every account is invalid.
            if not accounts:
                for handle in chunk:
                    result[reverse[handle]] = (None, None)
                continue
            for account in accounts:
                handle = str(account.get("screen_name") or "").lower()
                mint = reverse.get(handle)
                if not mint:
                    continue
                created = account_created_at(account.get("id") or account.get("id_str"))
                age = (now.astimezone(timezone.utc) - created).total_seconds() / 86400 if created else None
                result[mint] = (True, max(0.0, age) if age is not None else None)
        return result


_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_GENERIC_NAMES = {
    "coin", "token", "meme", "solana", "official", "cash", "money", "cat", "dog",
    "ai", "bot", "guy", "moon", "pump", "going", "orange",
}
_CONFIDENCE = {"confirmed": 3, "probable": 2, "possible": 1}


def _excerpt(text: str, limit: int = 180) -> str:
    cleaned = _SPACE.sub(" ", _URL.sub("", text)).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip(" ,.;:") + "…"


def _engagement(post: XPost) -> int:
    return post.like_count + post.repost_count * 2 + post.reply_count + post.quote_count * 2


def _match_post(candidate: Candidate, post: XPost) -> tuple[str, str] | None:
    token = candidate.token
    haystack = " ".join([post.text, *post.expanded_urls]).casefold()
    if token.mint.casefold() in haystack:
        return "confirmed", "contract address"
    if token.url and token.url.casefold() in haystack:
        return "confirmed", "Dexscreener link"

    symbol = token.symbol.strip().lstrip("$")
    if len(symbol) >= 2 and re.search(rf"(?<![\w$])\${re.escape(symbol)}(?!\w)", post.text, re.IGNORECASE):
        confidence = "probable" if candidate.recycled_label_count else "confirmed"
        return confidence, f"${symbol} cashtag"

    linked_handle = x_handle(token.socials)
    if linked_handle and linked_handle.casefold() == post.author_handle.casefold():
        return "probable", "linked project account"

    name = _SPACE.sub(" ", token.name).strip()
    if len(name) >= 5 and name.casefold() not in _GENERIC_NAMES:
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", post.text, re.IGNORECASE):
            return "possible", "token name"
    return None


def match_x_interactions(
    candidates: list[Candidate], posts: list[XPost], *, max_per_token: int = 6
) -> None:
    """Attach source-linked public X activity without asserting causation."""
    for candidate in candidates:
        matches: list[tuple[XPost, str, str]] = []
        for post in posts:
            matched = _match_post(candidate, post)
            if matched:
                matches.append((post, *matched))
        matches.sort(
            key=lambda item: (_CONFIDENCE[item[1]], _engagement(item[0]), item[0].created_at.timestamp()),
            reverse=True,
        )
        candidate.x_interactions = [
            XInteraction(
                author_handle=post.author_handle,
                author_name=post.author_name,
                interaction=post.interaction,
                summary=_excerpt(post.text),
                url=post.url,
                created_at=post.created_at,
                confidence=confidence,
                matched_on=matched_on,
                like_count=post.like_count,
                repost_count=post.repost_count,
                reply_count=post.reply_count,
                quote_count=post.quote_count,
            )
            for post, confidence, matched_on in matches[:max_per_token]
        ]
        if candidate.x_interactions:
            lead = candidate.x_interactions[0]
            qualifier = "verified" if lead.confidence == "confirmed" else lead.confidence
            candidate.catalyst = (
                f"@{lead.author_handle} {lead.interaction} matching content during the report window; "
                f"the association is {qualifier} via {lead.matched_on}."
            )
        else:
            candidate.catalyst = "No monitored X account produced a verifiable match in this 24-hour window."


def build_dex_evidence(candidate: Candidate) -> list[str]:
    """Explain admission using only the fields collected from the pair tape."""
    token = candidate.token
    signal = candidate.signals
    lines: list[str] = []
    age = signal.age_hours
    age_text = f"{age:.0f} hours old" if age is not None and age < 48 else (
        f"{age / 24:.0f} days old" if age is not None else "age unavailable"
    )
    lines.append(
        f"The pair was {age_text} at the cutoff and moved {token.price_change_24h:+,.0f}% in 24 hours to a ${token.market_cap:,.0f} market cap."
    )
    lines.append(
        f"Dexscreener recorded ${token.volume_24h:,.0f} of 24-hour volume, equal to {signal.turnover:.1f}x market cap, with ${token.liquidity_usd:,.0f} liquidity."
    )
    if signal.buy_imbalance_6h is not None and token.txns_6h.total:
        lines.append(
            f"Six-hour flow was {signal.buy_imbalance_6h:.0%} buys across {token.txns_6h.total:,} trades while the pair moved {token.price_change_6h:+.0f}%."
        )
    if candidate.kol_buyers:
        holding = len(candidate.kol_holders)
        lines.append(
            f"{len(candidate.kol_buyers)} tracked on-chain wallets bought during the window; {holding} still held at the snapshot."
        )
    structure: list[str] = []
    if candidate.safety.lp_locked_or_burned_pct is not None:
        structure.append(f"LP {candidate.safety.lp_locked_or_burned_pct:.0f}% locked or burned")
    if candidate.safety.top10_pct is not None:
        structure.append(f"nominal top 10 held {candidate.safety.top10_pct:.0f}%")
    if structure:
        lines.append("Supply structure at the snapshot: " + "; ".join(structure) + ".")
    return lines
