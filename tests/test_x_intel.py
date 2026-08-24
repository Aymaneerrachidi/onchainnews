from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brief.models import (
    Candidate,
    Enrichment,
    SafetyReport,
    Signals,
    TokenSnapshot,
    TransactionWindow,
    XPost,
)
from brief.sources.social import build_dex_evidence, match_x_interactions, x_handle
from brief.sources.openintel import OpenIntelSource
from brief.sources.http import SourceError
from brief.sources.x import XSource


NOW = datetime(2026, 8, 12, 6, 45, tzinfo=timezone.utc)
MINT = "GCa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpump"


def test_x_community_link_is_not_misread_as_the_i_account():
    socials = [{"type": "twitter", "url": "https://x.com/i/communities/1861401413311443182"}]

    assert x_handle(socials) is None


def candidate() -> Candidate:
    token = TokenSnapshot(
        mint=MINT,
        symbol="PLUMBER",
        name="Plumber",
        chain_id="solana",
        pair_address="PAIR",
        url="https://dexscreener.com/solana/PAIR",
        price_usd=.0024,
        market_cap=2_450_000,
        liquidity_usd=156_000,
        volume_24h=14_700_000,
        volume_6h=4_000_000,
        price_change_24h=8_085,
        price_change_6h=312,
        pair_created_at=NOW - timedelta(hours=23),
        txns_6h=TransactionWindow(41_000, 35_000),
    )
    return Candidate(
        token=token,
        signals=Signals(6.0, 0.0, None, 41_000 / 76_000, .064, None, None, 23),
        safety=SafetyReport(MINT, True, True, 100.0, 19.0),
        enrichment=Enrichment(),
        kol_buyers=["Chairman", "Nach"],
        kol_holders=["Chairman"],
    )


def test_x_match_keeps_provenance_and_labels_name_only_association():
    item = candidate()
    post = XPost(
        post_id="123",
        author_id="9",
        author_handle="cobie",
        author_name="Cobie",
        text="what we apin boyz plumber edition https://t.co/x",
        created_at=NOW - timedelta(hours=2),
        interaction="posted",
        url="https://x.com/cobie/status/123",
        like_count=11_000,
        repost_count=2_400,
    )
    match_x_interactions([item], [post])
    assert len(item.x_interactions) == 1
    match = item.x_interactions[0]
    assert match.author_handle == "cobie"
    assert match.url.endswith("/123")
    assert match.confidence == "possible"
    assert match.matched_on == "token name"
    assert "possible" in item.catalyst


def test_contract_address_is_a_confirmed_social_match():
    item = candidate()
    post = XPost(
        post_id="456",
        author_id="8",
        author_handle="desk",
        author_name="Desk",
        text=f"Watching ${item.token.symbol} CA {MINT}",
        created_at=NOW,
        interaction="quoted",
        url="https://x.com/desk/status/456",
    )
    match_x_interactions([item], [post])
    assert item.x_interactions[0].confidence == "confirmed"
    assert item.x_interactions[0].matched_on == "contract address"


def test_dex_evidence_explains_exactly_why_the_coin_was_called():
    lines = build_dex_evidence(candidate())
    assert any("+8,085%" in line and "$2,450,000" in line for line in lines)
    assert any("$14,700,000" in line and "6.0x" in line for line in lines)
    assert any("2 tracked on-chain wallets" in line for line in lines)
    assert any("top 10 held 19%" in line for line in lines)


class FakeHttp:
    async def get_json(self, *_args, **_kwargs):
        return {
            "data": [{
                "id": "789",
                "author_id": "42",
                "created_at": "2026-08-12T05:00:00.000Z",
                "text": f"${{PLUMBER}} {MINT}",
                "public_metrics": {
                    "like_count": 50,
                    "retweet_count": 8,
                    "reply_count": 2,
                    "quote_count": 1,
                },
                "referenced_tweets": [{"type": "quoted", "id": "1"}],
                "entities": {"urls": [{"expanded_url": "https://dexscreener.com/solana/PAIR"}]},
            }],
            "includes": {"users": [{"id": "42", "name": "Desk", "username": "desk"}]},
            "meta": {"result_count": 1},
        }


@pytest.mark.asyncio
async def test_x_source_parses_authors_metrics_interaction_and_links():
    source = XSource(FakeHttp(), "https://api.x.test/search", "token", ["desk"])
    posts = await source.posts(NOW - timedelta(hours=24))
    assert len(posts) == 1
    post = posts[0]
    assert post.author_handle == "desk"
    assert post.interaction == "quoted"
    assert post.like_count == 50 and post.repost_count == 8
    assert post.expanded_urls == ("https://dexscreener.com/solana/PAIR",)


class QuotaHttp:
    def __init__(self):
        self.calls = 0

    async def post_json(self, *_args, **_kwargs):
        self.calls += 1
        raise SourceError("opentwitter failed after 3 attempts: HTTP 402")


@pytest.mark.asyncio
async def test_trusted_timeline_stops_after_permanent_provider_failure():
    http = QuotaHttp()
    source = OpenIntelSource(http, "https://ai.6551.test", pause_seconds=0)
    source.token = "test-token"
    hits, failures = await source.trusted_timeline(
        [candidate()], ["one", "two", "three"], NOW
    )

    assert hits == 0
    assert http.calls == 1
    assert [failure.split(":", 1)[0] for failure in failures] == ["one", "two", "three"]


class TimelineHttp:
    def __init__(self, text: str):
        self.text = text

    async def post_json(self, *_args, **_kwargs):
        return {"data": [{
            "id": "99", "text": self.text, "createdAt": NOW.isoformat(),
            "userName": "Desk",
        }]}


@pytest.mark.asyncio
async def test_trusted_timeline_does_not_guess_between_reused_tickers():
    first = candidate()
    second = candidate()
    second.token.mint = "B" * 32
    source = OpenIntelSource(TimelineHttp("watching $PLUMBER"), "https://ai.6551.test", pause_seconds=0)
    source.token = "test-token"

    hits, _ = await source.trusted_timeline([first, second], ["desk"], NOW)

    assert hits == 0
    assert first.x_interactions == [] and second.x_interactions == []


@pytest.mark.asyncio
async def test_trusted_timeline_uses_exact_mint_for_reused_ticker():
    first = candidate()
    second = candidate()
    second.token.mint = "B" * 32
    source = OpenIntelSource(
        TimelineHttp(f"watching $PLUMBER {second.token.mint}"),
        "https://ai.6551.test",
        pause_seconds=0,
    )
    source.token = "test-token"

    hits, _ = await source.trusted_timeline([first, second], ["desk"], NOW)

    assert hits == 1
    assert first.x_interactions == [] and len(second.x_interactions) == 1
