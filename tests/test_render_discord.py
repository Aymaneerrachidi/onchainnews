from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from brief.engine import build_brief
from brief.ledger import Ledger
from brief.render.discord import (
    LOGO_REF,
    MAX_EMBEDS,
    MAX_FIELD_VALUE,
    MAX_TOTAL_CHARS,
    _embed_size,
    bot_channel_ids,
    build_payload,
    interactive_market_components,
    post_bot_payload,
    webhook_urls,
)
from tests.conftest import build_settings

NOW = datetime(2026, 8, 6, 6, 45, tzinfo=ZoneInfo("Europe/Paris"))


async def _payload(tmp_path):
    settings = build_settings(tmp_path)
    ledger = Ledger(settings.path("run", "database_path"))
    try:
        brief = await build_brief(settings, ledger, commit=False, now=NOW)
        return brief, build_payload(brief, settings)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_payload_fits_discord_limits(tmp_path):
    """A payload one character over the shared budget is rejected whole."""
    brief, payload = await _payload(tmp_path)
    if payload is None:
        pytest.skip("no runners in the fixture day")
    assert len(payload["embeds"]) <= MAX_EMBEDS
    assert sum(_embed_size(e) for e in payload["embeds"]) <= MAX_TOTAL_CHARS
    for embed in payload["embeds"]:
        for field in embed.get("fields") or []:
            assert 0 < len(field["value"]) <= MAX_FIELD_VALUE
            assert len(field["name"]) <= 256


@pytest.mark.asyncio
async def test_lead_embed_carries_the_brand_and_the_day(tmp_path):
    brief, payload = await _payload(tmp_path)
    if payload is None:
        pytest.skip("no runners in the fixture day")
    lead = payload["embeds"][0]
    assert lead["author"]["icon_url"] == LOGO_REF, "the logo rides along as an upload"
    assert brief.generated_at.strftime("%d %B %Y") in lead["title"]


@pytest.mark.asyncio
async def test_every_coin_shows_its_contract(tmp_path):
    """The address in a code block is the one thing chat does better than email."""
    _, payload = await _payload(tmp_path)
    if payload is None:
        pytest.skip("no runners in the fixture day")
    fields = [f for e in payload["embeds"] for f in (e.get("fields") or [])]
    assert fields, "runners reached the post"
    for field in fields:
        assert "`" in field["value"]


def test_budget_trims_the_smallest_tier_first():
    """Over budget, the biggest runs stay; the tail of the last tier goes."""
    embeds = [
        {"title": "lead", "description": "x" * 200},
        {"title": "$1M+ runners", "fields": [
            {"name": f"$BIG{i}", "value": "y" * 400} for i in range(10)
        ]},
        {"title": "$250K to $500K", "fields": [
            {"name": f"$SMALL{i}", "value": "z" * 400} for i in range(10)
        ]},
    ]
    while sum(_embed_size(e) for e in embeds) > MAX_TOTAL_CHARS:
        tail = next(e for e in reversed(embeds) if e.get("fields"))
        tail["fields"].pop()
        if not tail["fields"]:
            embeds.remove(tail)
    names = [f["name"] for e in embeds for f in (e.get("fields") or [])]
    assert "$BIG0" in names
    assert "$SMALL9" not in names


def test_webhook_urls_deduplicates_destinations(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/one")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_SECONDARY", "https://discord.test/one")
    assert webhook_urls() == ["https://discord.test/one"]

    monkeypatch.setenv("DISCORD_WEBHOOK_URL_SECONDARY", "https://discord.test/two")
    assert webhook_urls() == ["https://discord.test/one", "https://discord.test/two"]


def test_live_market_button_is_an_in_discord_interaction():
    components = interactive_market_components(1234, "20260825")

    assert len(components) == 3
    assert [button["label"] for button in components[0]["components"]] == [
        "View all runners", "Solana", "BNB", "Base",
    ]
    assert [button["label"] for button in components[1]["components"]] == [
        "Ethereum", "Robinhood", "All caps", "$250K-$500K",
    ]
    assert [button["label"] for button in components[2]["components"]] == [
        "$500K-$1M", "$1M-$10M", "$10M+", "Refresh",
    ]
    assert components[0]["components"][1]["custom_id"] == (
        "rfilter:solana:all:20260825:0:chain"
    )
    custom_ids = [
        button["custom_id"]
        for row in components
        for button in row["components"]
    ]
    assert len(custom_ids) == len(set(custom_ids))

    button = components[2]["components"][3]
    assert button["style"] == 2
    assert button["label"] == "Refresh"
    assert button["custom_id"] == "refresh_mc:1234:20260825"
    assert components[0]["components"][0]["style"] == 1
    assert components[1]["components"][2]["style"] == 1
    assert "url" not in button


@pytest.mark.asyncio
async def test_bot_message_owns_the_refresh_component():
    import httpx

    request_seen = None

    def handler(request):
        nonlocal request_seen
        request_seen = request
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    payload = {
        "username": "fomo onchain",
        "embeds": [{"description": "runner"}],
        "components": interactive_market_components(),
    }
    await post_bot_payload("123", payload, "secret-token", transport=transport)

    assert request_seen is not None
    assert str(request_seen.url) == "https://discord.com/api/v10/channels/123/messages"
    assert request_seen.headers["authorization"] == "Bot secret-token"
    assert "username" not in __import__("json").loads(request_seen.content)


def test_bot_channel_ids_deduplicate(monkeypatch):
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123")
    monkeypatch.setenv("DISCORD_CHANNEL_ID_SECONDARY", "123")
    assert bot_channel_ids() == ["123"]

    monkeypatch.setenv("DISCORD_CHANNEL_ID_SECONDARY", "456")
    assert bot_channel_ids() == ["123", "456"]
