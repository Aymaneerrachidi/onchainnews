from datetime import datetime, timezone

import httpx
import pytest

from brief.models import Candidate, Enrichment, SafetyReport, Signals, TokenSnapshot
from brief.sources.linked_socials import attach_linked_x_posts, linked_x_statuses


def candidate() -> Candidate:
    token = TokenSnapshot(
        mint="mint", symbol="FIH", name="fih", chain_id="solana",
        pair_address="pair", url="https://dexscreener.com/solana/pair",
        price_usd=1, market_cap=1, liquidity_usd=1, volume_24h=1, volume_6h=1,
        price_change_24h=1, price_change_6h=1,
        pair_created_at=datetime.now(timezone.utc),
        socials=[{"type": "twitter", "url": "https://x.com/MarcellxMarcell/status/2092392034678018467?s=20"}],
    )
    return Candidate(
        token=token,
        signals=Signals(0, 0, None, None, 0, None, None, None),
        safety=SafetyReport(mint="mint"),
        enrichment=Enrichment(),
    )


def test_extracts_exact_linked_status() -> None:
    assert linked_x_statuses(candidate()) == [
        ("MarcellxMarcell", "2092392034678018467", "https://x.com/MarcellxMarcell/status/2092392034678018467")
    ]


@pytest.mark.asyncio
async def test_attaches_oembed_post(monkeypatch) -> None:
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "author_name": "Marcell",
        "html": '<blockquote><p>Someone made a meme based on one of my drawings. <a href="https://t.co/x">pic</a></p></blockquote>',
    }))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    coin = candidate()
    assert await attach_linked_x_posts([coin]) == 1
    assert coin.x_interactions[0].summary == "Someone made a meme based on one of my drawings."
    assert coin.x_interactions[0].confidence == "confirmed"
    assert "deployer-supplied token-profile social" in coin.x_interactions[0].matched_on


@pytest.mark.asyncio
async def test_prefers_twitterapi_io_and_keeps_metrics(monkeypatch) -> None:
    original = httpx.AsyncClient
    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(200, json={"tweets": [{
            "id": "2092392034678018467", "text": "FIH linked post",
            "createdAt": "Wed Aug 26 01:21:00 +0000 2026",
            "author": {"name": "Marcell"}, "likeCount": 68,
            "retweetCount": 7, "replyCount": 6, "quoteCount": 2,
        }]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs))
    coin = candidate()
    assert await attach_linked_x_posts([coin], api_key="key") == 1
    post = coin.x_interactions[0]
    assert post.summary == "FIH linked post"
    assert (post.like_count, post.repost_count, post.reply_count, post.quote_count) == (68, 7, 6, 2)
    assert "TwitterAPI.io" in post.matched_on
