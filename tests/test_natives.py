from __future__ import annotations

import pathlib
import tomllib

from brief.config import Settings
from brief.natives import movers_from_pairs, qualifies, watchlist
from brief.sources.gmgn import CLI_CHAINS, safety_from_evidence, transfer_tax_pct

ROOT = pathlib.Path(__file__).resolve().parents[1]


def real_settings() -> Settings:
    """The shipped config, read without touching the environment.

    The loader in brief.config also reads .env, which writes the real API keys
    back into os.environ after conftest has cleared them; every test after this
    one then talks to the live network. Reading the file directly keeps the
    suite offline.
    """
    with (ROOT / "config.toml").open("rb") as handle:
        return Settings(root=ROOT, values=tomllib.load(handle))


def _pair(mint: str, change, *, liquidity=100_000.0, mcap=1_000_000.0, symbol="X"):
    return {
        "baseToken": {"address": mint, "symbol": symbol},
        "priceChange": {"h24": change},
        "liquidity": {"usd": liquidity},
        "marketCap": mcap,
        "volume": {"h24": 50_000.0},
        "url": "https://dexscreener.com/x",
    }


ENTRY = [{"symbol": "PEPE", "chain": "ethereum", "mint": "0xabc"}]


def test_only_real_moves_publish():
    """A watchlist that prints every name every day is a price table."""
    assert movers_from_pairs(ENTRY, {"ethereum": [_pair("0xabc", 12.0)]}, 30.0, 50.0) == []
    moved = movers_from_pairs(ENTRY, {"ethereum": [_pair("0xabc", 44.0)]}, 30.0, 50.0)
    assert len(moved) == 1 and moved[0].change_24h == 44.0


def test_a_rally_and_a_collapse_are_not_the_same_size():
    """30% up is a rally. 30% down is a red day. Only 50% down is a story."""
    assert qualifies(30.0, 30.0, 50.0) is True
    assert qualifies(-30.0, 30.0, 50.0) is False
    assert qualifies(-49.9, 30.0, 50.0) is False
    assert qualifies(-50.0, 30.0, 50.0) is True


def test_a_big_drop_is_a_move():
    """$CATE fell 58% in a day. That is the story, not the absence of one."""
    assert movers_from_pairs(ENTRY, {"ethereum": [_pair("0xabc", -35.0)]}, 30.0, 50.0) == []
    moved = movers_from_pairs(ENTRY, {"ethereum": [_pair("0xabc", -58.0)]}, 30.0, 50.0)
    assert len(moved) == 1
    assert moved[0].direction == "down"


def test_a_small_survivor_is_not_a_major():
    """A 60% day on a $4M coin is true, and still not a major-meme story."""
    small = _pair("0xabc", 60.0, mcap=4_600_000.0)
    assert movers_from_pairs(ENTRY, {"ethereum": [small]}, 30.0, 50.0, 10_000_000.0) == []
    assert len(movers_from_pairs(ENTRY, {"ethereum": [small]}, 30.0, 50.0, 0.0)) == 1


def test_missing_change_is_not_a_flat_day():
    """An unreported field must never be read as zero and silently dropped."""
    assert movers_from_pairs(ENTRY, {"ethereum": [_pair("0xabc", None)]}, 30.0, 50.0) == []
    assert movers_from_pairs(ENTRY, {"ethereum": [_pair("0xabc", "n/a")]}, 30.0, 50.0) == []


def test_the_deepest_pool_wins():
    """Searching a ticker returns spoof pools; depth is the honest tiebreak."""
    shallow = _pair("0xabc", 90.0, liquidity=10.0, mcap=99_000_000_000.0)
    real = _pair("0xabc", 41.0, liquidity=5_000_000.0, mcap=1_700_000_000.0)
    moved = movers_from_pairs(ENTRY, {"ethereum": [shallow, real]}, 30.0, 50.0)
    assert len(moved) == 1
    assert moved[0].market_cap == 1_700_000_000.0


def test_another_contract_in_the_batch_is_ignored():
    """One request carries thirty contracts; rows must match by address."""
    assert movers_from_pairs(ENTRY, {"ethereum": [_pair("0xdead", 80.0)]}, 30.0, 50.0) == []


def test_watchlist_is_deduplicated_and_real():
    settings = real_settings()
    rows = watchlist(settings)
    assert len(rows) >= 45
    assert len({r["mint"].lower() for r in rows}) == len(rows)
    # Robinhood is in the list because the chain is now covered at all.
    assert any(r["chain"] == "robinhood" for r in rows)
    for row in rows:
        assert row["chain"] in CLI_CHAINS, row


def test_robinhood_is_a_discovery_chain():
    assert CLI_CHAINS["robinhood"] == "robinhood"


def test_gmgn_safety_stands_in_where_goplus_cannot_reach():
    """Robinhood has no GoPlus coverage, so GMGN answers instead of nobody."""
    report = safety_from_evidence("0xabc", {
        "holders": 72_458,
        "top10Pct": 16.82,
        "isHoneypot": "no",
        "sellTax": 0.0,
        "buyTax": 0.0,
        "renouncedMint": 1,
        "renouncedFreeze": 1,
        "washTrading": False,
    })
    assert report.source == "gmgn"
    assert report.rugged is False
    assert report.holder_count == 72_458
    assert report.mint_authority_renounced is True
    assert report.risk_flags == []


def test_gmgn_safety_names_a_honeypot_and_a_tax():
    report = safety_from_evidence("0xabc", {
        "isHoneypot": "yes", "sellTax": 0.05, "washTrading": True,
    })
    assert report.rugged is True
    assert any("honeypot" in f for f in report.risk_flags)
    assert any("5% sell tax" == f for f in report.risk_flags)
    assert any("wash-trading" in f for f in report.risk_flags)


def test_gmgn_safety_leaves_unreported_fields_unknown():
    """Absent is not clean: an unanswered field must stay None, never False."""
    report = safety_from_evidence("0xabc", {})
    assert report.mint_authority_renounced is None
    assert report.freeze_authority_disabled is None
    assert report.top10_pct is None
    assert report.rugged is False


def test_only_explicit_buy_sell_fields_define_transfer_tax():
    evidence = {"totalFee": 33.4, "tradeFee": 28.1, "buyTax": "0.01", "sellTax": 0.03}
    assert transfer_tax_pct(evidence) == 3.0
    assert transfer_tax_pct({"totalFee": 99, "tradeFee": 88}) is None
