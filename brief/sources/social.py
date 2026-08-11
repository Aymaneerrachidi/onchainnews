from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from brief.sources.http import CachedHttpClient


TWITTER_EPOCH_MS = 1_288_834_974_657


def x_handle(socials: list[dict[str, str]]) -> str | None:
    for social in socials:
        kind = str(social.get("type") or "").lower()
        url = str(social.get("url") or "")
        if kind not in {"twitter", "x"} and "twitter.com" not in url and "x.com" not in url:
            continue
        path = urlparse(url).path.strip("/")
        handle = path.split("/")[0].lstrip("@") if path else ""
        if handle and handle.lower() not in {"intent", "share", "home", "search"}:
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
