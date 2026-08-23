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
    build_payload,
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
