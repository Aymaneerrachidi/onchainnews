from datetime import datetime, timezone

from brief.lore import EXPLANATORY, STORY_WORDS, query_ladder
from brief.models import Candidate, Enrichment, SafetyReport, Signals, TokenSnapshot


def _candidate() -> Candidate:
    token = TokenSnapshot(
        mint="mint", symbol="MORTY", name="Gucci Morty", chain_id="solana",
        pair_address="pair", url="https://dexscreener.com/solana/pair",
        price_usd=1, market_cap=1, liquidity_usd=1, volume_24h=1, volume_6h=1,
        price_change_24h=1, price_change_6h=1,
        pair_created_at=datetime.now(timezone.utc),
    )
    return Candidate(
        token=token,
        signals=Signals(0, 0, None, None, 0, None, None, None),
        safety=SafetyReport(mint="mint"), enrichment=Enrichment(),
    )


def test_meme_research_always_checks_culture_sources() -> None:
    queries = query_ladder(_candidate())
    assert '"MORTY" "Gucci Morty"' in queries
    assert '"MORTY" "Gucci Morty" lore' in queries
    assert '"MORTY" "Gucci Morty" story' in queries
    assert '"Gucci Morty" lore' in queries
    assert '"Gucci Morty" story' in queries
    assert '"Gucci Morty" origin' in queries
    assert '"Gucci Morty" Solana meme coin' in queries
    assert 'why is "Gucci Morty" trending Solana' in queries
    assert 'site:knowyourmeme.com "Gucci Morty"' in queries
    assert '"Gucci Morty" TikTok trend meme' in queries
    assert '"Gucci Morty" Douyin trend meme' in queries
    assert '"MORTY" TikTok meme trend' in queries
    assert "knowyourmeme.com" in EXPLANATORY
    assert "tiktok.com" in EXPLANATORY
    assert STORY_WORDS.search("The character became a TikTok trend")
