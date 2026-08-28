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
from brief.sources.x import (
    TwitterApiIoSource,
    XSource,
    candidate_lore_search_terms,
    candidate_search_terms,
    load_x_accounts,
)
from scripts.check_x_existing import merge_audit_into_snapshot


NOW = datetime(2026, 8, 12, 6, 45, tzinfo=timezone.utc)
MINT = "GCa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpump"


def test_x_community_link_is_not_misread_as_the_i_account():
    socials = [{"type": "twitter", "url": "https://x.com/i/communities/1861401413311443182"}]

    assert x_handle(socials) is None


def test_x_account_files_merge_with_inline_accounts(tmp_path):
    account_file = tmp_path / "accounts.txt"
    account_file.write_text("@NewDesk\nexisting\n# comment\ninvalid handle\n", encoding="utf-8")

    accounts = load_x_accounts(
        ["Existing", "Curated"], [str(account_file)], root=tmp_path
    )

    assert accounts == ["existing", "curated", "newdesk"]


def test_candidate_search_terms_include_specific_token_name():
    item = candidate()

    assert '"Plumber"' in candidate_search_terms(item)
    assert '"Plumber" lore' in candidate_search_terms(item)
    assert '"Plumber" story' in candidate_search_terms(item)


def test_x_lore_search_terms_cover_full_token_name():
    terms = candidate_lore_search_terms(candidate())

    assert '"Plumber" lore' in terms
    assert '"Plumber" story' in terms
    assert '"Plumber" origin' in terms
    assert '"Plumber" meme' in terms
    assert '"Plumber" news' in terms
    assert '"Plumber" TikTok' in terms
    assert '"Plumber" Douyin' in terms


def test_daily_x_audit_replaces_stale_context_in_every_snapshot_index():
    stale = {
        "mint": MINT, "lore": "X: old post Lore: original lore",
        "xInteractions": [{"url": "https://x.com/old/status/1"}],
    }
    snapshot = {"runnerUniverse": [dict(stale)], "runners": [dict(stale)]}
    audit = {
        "generatedAt": NOW.isoformat(), "windowStart": NOW.isoformat(),
        "windowEnd": NOW.isoformat(), "coins": [{
            "mint": MINT,
            "informativeMatches": [{
                "handle": "desk", "author": "Desk", "interaction": "replied",
                "summary": "New verified token update",
                "url": "https://x.com/desk/status/2", "confidence": "confirmed",
            }],
        }],
    }

    assert merge_audit_into_snapshot(snapshot, audit) == 2
    for collection in ("runnerUniverse", "runners"):
        row = snapshot[collection][0]
        assert row["xInteractions"][0]["interaction"] == "replied"
        assert row["lore"] == "X: New verified token update Lore: original lore"


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


def test_name_only_match_is_a_research_lead_not_publishable_lore():
    item = candidate()
    post = XPost(
        post_id="123",
        author_id="9",
        author_handle="cobie",
        author_name="Cobie",
        text="Plumber launched a public rewards portal today with fees distributed to holders https://t.co/x",
        created_at=NOW - timedelta(hours=2),
        interaction="posted",
        url="https://x.com/cobie/status/123",
        like_count=11_000,
        repost_count=2_400,
    )
    match_x_interactions([item], [post])
    assert item.x_interactions == []
    assert len(item.internal_x_leads) == 1
    match = item.internal_x_leads[0]
    assert match.author_handle == "cobie"
    assert match.url.endswith("/123")
    assert match.confidence == "possible"
    assert match.matched_on == "token name"
    assert "No monitored X account" in item.catalyst


def test_contract_address_is_a_confirmed_social_match():
    item = candidate()
    post = XPost(
        post_id="456",
        author_id="8",
        author_handle="desk",
        author_name="Desk",
        text=f"${item.token.symbol} launched its public rewards portal today. CA {MINT}",
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


def test_bullish_or_bought_posts_never_become_coin_lore():
    item = candidate()
    posts = [
        XPost(
            post_id="weak-1", author_id="1", author_handle="trader", author_name="Trader",
            text=f"I'm so bullish on $PLUMBER LFG {MINT}", created_at=NOW,
            interaction="posted", url="https://x.com/trader/status/weak-1",
            like_count=50_000, author_followers=500_000,
        ),
        XPost(
            post_id="weak-2", author_id="1", author_handle="trader", author_name="Trader",
            text=f"I just bought $PLUMBER, this is going to moon {MINT}", created_at=NOW,
            interaction="posted", url="https://x.com/trader/status/weak-2",
            like_count=50_000, author_followers=500_000,
        ),
    ]
    match_x_interactions([item], posts)
    assert item.x_interactions == []


def test_generic_name_collision_without_crypto_context_is_not_lore():
    item = candidate()
    item.token.symbol = "DOGE2"
    item.token.name = "Caesar"
    post = XPost(
        post_id="film", author_id="1", author_handle="film", author_name="Film",
        text="New look at Caesar Flickerman in the next Hunger Games film.",
        created_at=NOW, interaction="posted", url="https://x.com/film/status/film",
        author_followers=500_000, author_verified=True,
    )

    match_x_interactions([item], [post])

    assert item.x_interactions == []


def test_name_match_with_memecoin_context_still_needs_exact_identity_for_lore():
    item = candidate()
    item.token.symbol = "KYLIE"
    item.token.name = "Kylie"
    post = XPost(
        post_id="hack", author_id="1", author_handle="news", author_name="News",
        text="Kylie Jenner's account was reportedly hacked to promote a memecoin in deleted posts.",
        created_at=NOW, interaction="posted", url="https://x.com/news/status/hack",
        author_followers=500_000, author_verified=True,
    )

    match_x_interactions([item], [post])

    assert item.x_interactions == []
    assert len(item.internal_x_leads) == 1


def test_recycled_ticker_needs_exact_identity():
    item = candidate()
    item.recycled_label_count = 3
    post = XPost(
        post_id="ambiguous", author_id="1", author_handle="news", author_name="News",
        text="$PLUMBER announced a public rewards program and launched its claim portal today.",
        created_at=NOW, interaction="posted", url="https://x.com/news/status/ambiguous",
        author_followers=100_000,
    )
    match_x_interactions([item], [post])
    assert item.x_interactions == []


def test_duplicate_ticker_needs_exact_identity_even_without_provider_label():
    first = candidate()
    second = candidate()
    second.token.mint = "B" * 32
    post = XPost(
        post_id="duplicate", author_id="1", author_handle="desk", author_name="Desk",
        text="$PLUMBER launched a public rewards program and claim portal today.",
        created_at=NOW, interaction="posted", url="https://x.com/desk/status/duplicate",
        author_followers=100_000,
    )

    match_x_interactions([first, second], [post])

    assert first.x_interactions == [] and second.x_interactions == []


def test_whitelisted_editorial_recap_can_cover_multiple_tickers():
    item = candidate()
    post = XPost(
        post_id="recap", author_id="1", author_handle="mellometrics",
        author_name="Mello Metrics",
        text=(
            "Daily Memecoin Recap - August 24 $PLUMBER -> hit $4M, public rewards portal "
            "$ALPHA $BETA $GAMMA $DELTA $OMEGA"
        ),
        created_at=NOW, interaction="posted", url="https://x.com/mellometrics/status/recap",
    )

    match_x_interactions(
        [item], [post], editorial_accounts=["mellometrics"]
    )

    assert item.x_interactions == []
    assert len(item.internal_x_leads) == 1


def test_internal_editorial_account_never_enters_public_interactions():
    item = candidate()
    post = XPost(
        post_id="private-recap", author_id="1", author_handle="mellometrics",
        author_name="Mello Metrics",
        text="Daily Memecoin Recap - August 24 $PLUMBER -> hit $4M, public rewards portal",
        created_at=NOW, interaction="posted",
        url="https://x.com/mellometrics/status/private-recap",
    )

    match_x_interactions(
        [item], [post], editorial_accounts=["mellometrics"],
        internal_only_accounts=["mellometrics"],
    )

    assert item.x_interactions == []
    assert len(item.internal_x_leads) == 1


class TwitterApiIoHttp:
    def __init__(self):
        self.kwargs = None

    async def get_json(self, *_args, **kwargs):
        self.kwargs = kwargs
        return {
            "tweets": [{
                "id": "999",
                "url": "https://x.com/desk/status/999",
                "text": f"$PLUMBER launched a rewards portal {MINT}",
                "createdAt": "Tue Aug 12 05:00:00 +0000 2026",
                "retweetCount": 7,
                "replyCount": 3,
                "likeCount": 42,
                "quoteCount": 2,
                "isReply": False,
                "conversationId": "998",
                "author": {
                    "id": "42", "userName": "Desk", "name": "Desk News",
                    "followers": 12000, "isBlueVerified": True,
                },
                "entities": {"urls": [{"expanded_url": "https://project.test/launch"}]},
            }],
            "has_next_page": False,
            "next_cursor": "",
        }


class PaginatedTwitterApiIoHttp:
    def __init__(self):
        self.calls = 0

    async def get_json(self, *_args, **_kwargs):
        self.calls += 1
        return {
            "tweets": [{
                "id": str(self.calls),
                "text": "$PLUMBER story",
                "createdAt": "Mon Aug 11 05:00:00 +0000 2026",
                "author": {"id": "42", "userName": "Desk", "name": "Desk"},
            }],
            "has_next_page": True,
            "next_cursor": f"page-{self.calls + 1}",
        }


@pytest.mark.asyncio
async def test_twitterapi_io_source_uses_api_key_and_parses_schema():
    http = TwitterApiIoHttp()
    source = TwitterApiIoSource(
        http, "https://api.twitterapi.io/twitter/tweet/advanced_search",
        "secret", ["Desk", "News"],
    )
    posts = await source.posts(NOW - timedelta(hours=24))
    assert len(posts) == 1
    assert posts[0].author_handle == "Desk"
    assert posts[0].author_followers == 12000
    assert posts[0].author_verified is True
    assert posts[0].expanded_urls == ("https://project.test/launch",)
    assert http.kwargs["headers"] == {"X-API-Key": "secret"}
    assert "from:desk OR from:news" in http.kwargs["params"]["query"]
    assert http.kwargs["params"]["queryType"] == "Latest"


@pytest.mark.asyncio
async def test_twitterapi_io_stops_pagination_at_reporting_boundary():
    http = PaginatedTwitterApiIoHttp()
    source = TwitterApiIoSource(
        http, "https://api.twitterapi.io/twitter/tweet/advanced_search",
        "secret", ["desk"], max_pages_per_query=100,
    )
    posts = await source.posts(NOW - timedelta(hours=24), until=NOW)
    assert posts == []
    assert http.calls == 1


@pytest.mark.asyncio
async def test_twitterapi_io_term_search_filters_to_monitored_accounts():
    http = TwitterApiIoHttp()
    source = TwitterApiIoSource(
        http, "https://api.twitterapi.io/twitter/tweet/advanced_search",
        "secret", ["desk"],
    )
    posts = await source.posts_for_terms(
        NOW - timedelta(hours=24), [[MINT, "$PLUMBER"]]
    )
    assert len(posts) == 1
    assert MINT in http.kwargs["params"]["query"]
    assert "$PLUMBER" in http.kwargs["params"]["query"]
    assert "from:desk" not in http.kwargs["params"]["query"]


@pytest.mark.asyncio
async def test_twitterapi_io_targeted_lore_search_accepts_unmonitored_authors():
    http = TwitterApiIoHttp()
    source = TwitterApiIoSource(
        http, "https://api.twitterapi.io/twitter/tweet/advanced_search",
        "secret", ["someone_else"],
    )
    posts = await source.posts_for_terms(
        NOW - timedelta(days=30), [[f'$PLUMBER lore', f'$PLUMBER story']],
        allow_any_author=True,
    )
    assert len(posts) == 1
    assert posts[0].author_handle == "Desk"
    assert '$PLUMBER lore' in http.kwargs["params"]["query"]
