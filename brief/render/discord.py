"""The daily recap as a Discord post.

Discord is not an inbox. A message caps at 2,000 characters and the embeds
attached to it share a 6,000 character budget, so the email's twenty-seven
cards cannot simply be reflowed: the recap has to be rebuilt around what a
person reads in a channel between other messages.

The shape that fits is one message carrying a lead embed with the day's
context, then an embed per tier. Each coin is a field: the ticker and the peak
in the name, the reason it ran in the value, and the contract in a code block
underneath, which Discord makes tap-to-copy on a phone. That last part is the
one thing this format does better than the email.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from brief.config import Settings
from brief.journal import kol_trade_count
from brief.links import fomo_token_url
from brief.models import Brief, Candidate
from brief.preflight import DeliveryProof
from brief.render.email import _candidate_tier, _usable_lore, peak_cap
from brief.render.formatting import money

# The logo travels with the post as an upload rather than a link. A hosted
# asset would tie the brand on every message to the site being deployed, and
# the first check proved that dependency real: the URL 404'd.
LOGO = "fomo-logo.jpg"
LOGO_REF = f"attachment://{LOGO}"

# fomo blue, as the stripe down the left of every embed. It carries the brand
# without an asset and it is what people recognise while scrolling a channel.
BRAND = 0x516AF6

# Discord's own limits. Everything here is budgeted against them rather than
# hoped through: a payload one character over is rejected whole.
MAX_EMBEDS = 10
MAX_TOTAL_CHARS = 5800          # 6,000 with room for the wrapper
MAX_FIELDS_PER_EMBED = 25
MAX_FIELD_VALUE = 1024

TIERS = (
    ("S", "$1M+ runners"),
    ("A", "$500K to $1M"),
    ("B", "$250K to $500K"),
)


def interactive_market_components(refreshed_at: int = 0) -> list[dict[str, Any]]:
    """Build the application-owned button handled by our Vercel endpoint."""
    return [{
        "type": 1,
        "components": [{
            "type": 2,
            "style": 2,
            "label": "Refresh live MC",
            "custom_id": f"refresh_mc:{int(refreshed_at)}",
        }],
    }]


def bot_token() -> str:
    return os.environ.get("DISCORD_BOT_TOKEN", "").strip()


def bot_channel_ids() -> list[str]:
    result: list[str] = []
    for name in ("DISCORD_CHANNEL_ID", "DISCORD_CHANNEL_ID_SECONDARY"):
        channel_id = os.environ.get(name, "").strip()
        if channel_id and channel_id not in result:
            result.append(channel_id)
    return result


def webhook_urls() -> list[str]:
    """Configured Discord destinations, in order and without duplicates."""
    result: list[str] = []
    for name in ("DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL_SECONDARY"):
        url = os.environ.get(name, "").strip()
        if url and url not in result:
            result.append(url)
    return result


def configured() -> bool:
    return bool(webhook_urls())


def _cause(candidate: Candidate) -> str:
    """Why it ran, if we know. The same gate the email uses."""
    stated = (candidate.provider_evidence.get("why", {}) or {}).get("cause")
    if stated:
        return _usable_lore(str(stated)) or ""
    for item in candidate.news_evidence or []:
        usable = _usable_lore(str(item.get("summary") or ""))
        if usable:
            return usable
    return ""


def _field(candidate: Candidate) -> dict[str, str]:
    token = candidate.token
    high = peak_cap(candidate)
    current = float(token.market_cap or 0)

    lines: list[str] = []
    cause = _cause(candidate)
    if cause:
        lines.append(cause[:220])

    why = candidate.provider_evidence.get("why", {}) or {}
    source_url = str(why.get("sourceUrl") or "").strip()
    if not source_url:
        source_url = next(
            (
                str(item.get("url") or "").strip()
                for item in (candidate.news_evidence or [])
                if str(item.get("url") or "").strip()
            ),
            "",
        )

    numbers = []
    if current > 0:
        numbers.append(f"{money(current)} now")
    if high > 0:
        numbers.append(f"{money(high)} high")
    traded = kol_trade_count(candidate)
    if traded:
        numbers.append(f"{traded} tracked wallets")
    if numbers:
        lines.append(" · ".join(numbers))

    if token.mint:
        lines.append(f"[Trade on Fomo]({fomo_token_url(token.chain_id, token.mint)})")
        # A code block is tap-to-copy on mobile, which is the whole reason to
        # put the address in a chat message at all.
        lines.append(f"`{token.mint}`")
    if source_url:
        lines.append(f"[Source]({source_url})")

    value = "\n".join(lines)[:MAX_FIELD_VALUE] or "​"
    return {"name": f"${token.symbol} · {money(high)}", "value": value, "inline": False}


def _embed_size(embed: dict[str, Any]) -> int:
    total = len(str(embed.get("title") or "")) + len(str(embed.get("description") or ""))
    for field in embed.get("fields") or []:
        total += len(field["name"]) + len(field["value"])
    footer = (embed.get("footer") or {}).get("text") or ""
    return total + len(footer)


def build_payload(brief: Brief, settings: Settings) -> dict[str, Any] | None:
    """One message: the day's context, then a tier per embed."""
    runners = list(brief.runners or [])
    if not runners:
        return None

    site = str(settings.get("delivery", "report_url", "") or "").rstrip("/")
    when = brief.generated_at.strftime("%d %B %Y")

    narrative = brief.narrative or {}
    intro = str(narrative.get("intro") or "").strip()
    context: list[str] = []
    for section in (narrative.get("sections") or [])[:4]:
        title = str(section.get("title") or "").strip()
        if not title or title.lower() in {"also ran", "the rest", "everything else"}:
            continue
        names = ", ".join(
            f"${item.get('symbol')}" for item in (section.get("coins") or [])[:5]
            if item.get("symbol")
        )
        context.append(f"**{title}**" + (f"\n{names}" if names else ""))

    lead: dict[str, Any] = {
        "color": BRAND,
        "author": {"name": "fomo onchain", "icon_url": LOGO_REF},
        "thumbnail": {"url": LOGO_REF},
        "title": f"Daily memecoin recap · {when}",
        "description": (intro + ("\n\n" + "\n\n".join(context) if context else ""))[:4000],
    }
    if site:
        lead["url"] = site
    embeds: list[dict[str, Any]] = [lead]

    for tier, title in TIERS:
        members = [c for c in runners if _candidate_tier(c) == tier]
        if not members:
            continue
        members.sort(key=peak_cap, reverse=True)
        embeds.append({
            "color": BRAND,
            "title": title,
            "fields": [_field(c) for c in members[:MAX_FIELDS_PER_EMBED]],
        })

    # Trim from the bottom until the whole thing fits. The biggest runs matter
    # most, so the smallest tier loses its tail first.
    while sum(_embed_size(e) for e in embeds) > MAX_TOTAL_CHARS:
        tail = next((e for e in reversed(embeds) if e.get("fields")), None)
        if tail is None or not tail["fields"]:
            break
        tail["fields"].pop()
        if not tail["fields"]:
            embeds.remove(tail)

    shown = sum(len(e.get("fields") or []) for e in embeds)
    if shown < len(runners):
        embeds[-1]["footer"] = {
            "text": f"{len(runners) - shown} more on the site · {site}" if site
            else f"{len(runners) - shown} more in the email"
        }

    return {
        "username": "fomo onchain",
        "embeds": embeds[:MAX_EMBEDS],
    }


async def send_discord(
    brief: Brief,
    settings: Settings,
    *,
    audit: DeliveryProof | None = None,
    transport=None,
) -> bool:
    """Post the recap; False when it is switched off or unconfigured.

    Failures propagate. The caller in main.py catches them so a chat post can
    never cost the report that already rendered.
    """
    if not bool(settings.get("delivery", "discord_enabled", False)):
        return False
    if bool(settings.get("delivery", "require_preflight", False)) and audit is None:
        raise RuntimeError("delivery blocked: a passing runner preflight is required")
    payload = build_payload(brief, settings)
    if payload is None:
        return False
    token = bot_token()
    channels = bot_channel_ids()
    if token and channels:
        payload["components"] = interactive_market_components()
        for channel_id in channels:
            await post_bot_payload(
                channel_id, payload, token, settings.root / "web" / LOGO,
                transport=transport,
            )
        return True
    urls = webhook_urls()
    if not urls:
        return False
    for url in urls:
        await post_payload(url, payload, settings.root / "web" / LOGO, transport=transport)
    return True


async def post_payload(url: str, payload: dict[str, Any], logo: Path | None = None,
                       *, transport=None) -> None:
    """Send one webhook message, carrying the logo as an upload when we have it."""
    files = None
    if logo is not None and logo.exists():
        files = {LOGO: (LOGO, logo.read_bytes(), "image/jpeg")}
    else:
        # Nothing to attach, so strip the references: Discord rejects an
        # attachment:// URL that no upload satisfies.
        for embed in payload.get("embeds") or []:
            embed.pop("thumbnail", None)
            (embed.get("author") or {}).pop("icon_url", None)
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        if files:
            response = await client.post(
                url, data={"payload_json": json.dumps(payload)}, files=files,
            )
        else:
            response = await client.post(url, json=payload)
        response.raise_for_status()


async def post_bot_payload(
    channel_id: str,
    payload: dict[str, Any],
    token: str,
    logo: Path | None = None,
    *,
    transport=None,
) -> None:
    """Create an application-owned message so Discord can deliver clicks."""
    body = dict(payload)
    body.pop("username", None)
    files = None
    if logo is not None and logo.exists():
        files = {"files[0]": (LOGO, logo.read_bytes(), "image/jpeg")}
    else:
        for embed in body.get("embeds") or []:
            embed.pop("thumbnail", None)
            (embed.get("author") or {}).pop("icon_url", None)
    headers = {"Authorization": f"Bot {token}"}
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        if files:
            response = await client.post(
                url, headers=headers,
                data={"payload_json": json.dumps(body)}, files=files,
            )
        else:
            response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
