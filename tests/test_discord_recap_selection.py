from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

from conftest import build_settings
from brief.journal import limit_runner_board


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "discord_recap_selection", ROOT / "scripts" / "send_live_discord_recap.py"
)
assert SPEC is not None and SPEC.loader is not None
recap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recap)


def _coin(
    symbol: str,
    *,
    chain: str = "solana",
    age: float = 12,
    peak: float = 2_000_000,
    move: float = 500,
    top10: float = 15,
    holders: int = 2_500,
    kols: int = 5,
    honeypot: int = 0,
    entrapment: float = 0.0,
    liquidity: float = 120_000,
    volume: float = 2_000_000,
):
    row = {
        "mint": f"{symbol}-mint",
        "symbol": symbol,
        "name": symbol,
        "chain": chain,
        "ageHours": age,
        "marketCap": peak * 0.75,
        "peakMarketCap": peak,
        "liquidity": liquidity,
        "volume24h": volume,
        "change24h": move,
        "holders": holders,
        "top10Pct": top10,
        "lpLockedPct": 100,
        "providerEvidence": {
            "gmgn": {
                "holderCount": holders,
                "top10Pct": top10,
                "kolCount": kols,
                "isHoneypot": honeypot,
                "entrapmentRatio": entrapment,
                "washTrading": False,
                "bundlerRate": 0.0,
                "insiderRate": 0.0,
                "devTeamHoldRate": 0.0,
                "kline24hCandleCount": 24,
                "kline24hPeakMarketCap": peak,
                "kline24hMarketCapVerified": True,
                "kline24hPeakFromOpenPct": move,
            }
        },
    }
    return recap._candidate(row)


def _settings(tmp_path):
    settings = build_settings(tmp_path / "publication")
    settings.values["journal"].update({
        "min_liquidity": 40_000,
        "peak_market_cap_floor": 1_000_000,
        "publication_fresh_hours": 24,
        "publication_max_coins": 15,
        "publication_max_non_solana": 4,
        "publication_max_top10_pct": 50,
        "publication_min_holders": 1_000,
        "publication_min_solana_kols": 1,
        "publication_solana_old_small_move_pct": 200,
        "publication_solana_old_mid_move_pct": 125,
        "publication_solana_old_large_move_pct": 50,
        "publication_other_old_small_move_pct": 400,
        "publication_other_old_mid_move_pct": 250,
        "publication_other_old_large_move_pct": 100,
    })
    return settings


def test_exhaustive_x_context_reaches_every_interactive_snapshot_collection():
    mint = next(iter(recap.X_AUDIT_CONTEXT))
    snapshot = {
        "runnerUniverse": [{"mint": mint, "lore": "Normal community lore."}],
        "runners": [{"mint": mint, "lore": "Normal community lore."}],
    }

    assert recap._merge_exhaustive_x_context_into_snapshot(snapshot) == 2
    for collection in ("runnerUniverse", "runners"):
        row = snapshot[collection][0]
        assert not row["lore"].startswith("X:")
        assert "PumpfunEco reported" not in row["lore"]
        assert row["lore"] == "Normal community lore."
        assert "Lore:" not in row["lore"]
        assert row["providerEvidence"]["why"]["sourceUrl"].startswith("https://x.com/")
        assert row["xInteractions"][0]["summary"].startswith("PumpfunEco reported")
        assert row["xInteractions"][0]["matchedOn"] == "exact_contract_audit"


def test_exhaustive_x_context_merge_is_idempotent():
    mint = next(iter(recap.X_AUDIT_CONTEXT))
    snapshot = {"runnerUniverse": [{"mint": mint, "lore": "Normal lore."}]}

    recap._merge_exhaustive_x_context_into_snapshot(snapshot)
    recap._merge_exhaustive_x_context_into_snapshot(snapshot)

    row = snapshot["runnerUniverse"][0]
    assert len(row["xInteractions"]) == 1
    assert "Lore:" not in row["lore"]
    assert row["lore"].count("Normal lore.") == 1


def test_publication_blocks_explicit_honeypot_and_tut_concentration(tmp_path):
    settings = _settings(tmp_path)
    velvet = _coin("VELVET", chain="base", honeypot=1)
    tut = _coin("TUT", chain="bsc", top10=62.2)

    assert not recap._eligible_for_recap(velvet, settings)
    assert not recap._eligible_for_recap(tut, settings)


def test_publication_is_launch_first_and_keeps_only_exceptional_old_moves(tmp_path):
    settings = _settings(tmp_path)
    fresh = _coin("FRESH", age=23.9, move=20)
    ordinary_old = _coin("OLD", age=25, peak=5_000_000, move=199)
    exceptional_old = _coin("REVIVAL", age=25, peak=5_000_000, move=200)
    cate = _coin("CATE", age=700, peak=60_000_000, move=87)

    assert recap._eligible_for_recap(fresh, settings)
    assert not recap._eligible_for_recap(ordinary_old, settings)
    assert recap._eligible_for_recap(exceptional_old, settings)
    assert recap._eligible_for_recap(cate, settings)


def test_publication_caps_other_chains_without_crowding_out_solana(tmp_path):
    settings = _settings(tmp_path)
    solana = [_coin(f"SOL{i}", move=100 + i) for i in range(11)]
    others = [_coin(f"EVM{i}", chain="base", move=1_000 + i) for i in range(10)]

    selected = recap._select_recap_candidates([*solana, *others], settings)

    assert len(selected) == 15
    assert sum(coin.token.chain_id != "solana" for coin in selected) == 4
    assert sum(coin.token.chain_id == "solana" for coin in selected) == 11


def test_publication_keeps_normal_edition_at_fifteen_without_exceptional_overflow(tmp_path):
    settings = _settings(tmp_path)
    settings.values["journal"].update({
        "publication_max_coins": 20,
        "publication_standard_coins": 15,
        "publication_overflow_min_multiple": 5.0,
        "publication_max_non_solana": 4,
    })
    candidates = [_coin(f"RUN{i}", move=250 - i) for i in range(18)]

    selected = recap._select_recap_candidates(candidates, settings)

    assert len(selected) == 15
    settings.values["journal"]["max_runners"] = 20
    assert len(limit_runner_board(candidates, settings)) == 15


def test_publication_can_expand_to_twenty_for_additional_five_x_runners(tmp_path):
    settings = _settings(tmp_path)
    settings.values["journal"].update({
        "publication_max_coins": 20,
        "publication_standard_coins": 15,
        "publication_overflow_min_multiple": 5.0,
        "publication_max_non_solana": 4,
    })
    candidates = [_coin(f"RUN{i}", move=900 - i) for i in range(24)]

    selected = recap._select_recap_candidates(candidates, settings)

    assert len(selected) == 20
    assert all(recap.verified_window_multiple(candidate) >= 5 for candidate in selected[15:])
    settings.values["journal"]["max_runners"] = 20
    assert len(limit_runner_board(candidates, settings)) == 20


def test_large_cross_chain_mover_keeps_a_slot_after_clearing_safety(tmp_path):
    settings = _settings(tmp_path)
    settings.values["journal"]["publication_other_old_large_move_pct"] = 30
    solana = [_coin(f"SOL{i}", move=100 + i) for i in range(11)]
    fresh_others = [
        _coin(f"FRESH{i}", chain="base", age=12, move=1_000 + i)
        for i in range(4)
    ]
    cashcat = _coin(
        "CASHCAT",
        chain="robinhood",
        age=1_000,
        peak=182_000_000,
        move=46.87,
        kols=188,
        entrapment=0.10,
    )

    selected = recap._select_recap_candidates([*solana, *fresh_others, cashcat], settings)

    assert len(selected) == 15
    assert "CASHCAT" in {coin.token.symbol for coin in selected}
    assert sum(coin.token.chain_id != "solana" for coin in selected) == 4


def test_entrapment_ceiling_still_blocks_weakly_confirmed_market(tmp_path):
    settings = _settings(tmp_path)
    settings.values["journal"].update({
        "publication_other_old_large_move_pct": 30,
        "publication_other_max_entrapment_ratio": 0.50,
    })
    risky = _coin(
        "TRAPPED",
        chain="robinhood",
        age=1_000,
        peak=182_000_000,
        move=80,
        entrapment=0.51,
    )

    assert not recap._eligible_for_recap(risky, settings)
    assert any(
        "entrapment-linked flow" in reason
        for reason in recap._publication_safety_reasons(risky, settings)
    )


def test_strong_market_evidence_overrides_only_entrapment_warning(tmp_path):
    settings = _settings(tmp_path)
    settings.values["journal"].update({
        "publication_other_old_large_move_pct": 30,
        "publication_other_max_entrapment_ratio": 0.50,
        "publication_entrapment_override_enabled": True,
        "publication_entrapment_override_min_liquidity": 250_000,
        "publication_entrapment_override_min_volume_24h": 1_000_000,
        "publication_entrapment_override_min_holders": 5_000,
        "publication_entrapment_override_max_top10_pct": 20,
        "publication_entrapment_override_min_kols": 10,
    })
    organic = _coin(
        "ORGANIC",
        chain="robinhood",
        age=1_000,
        peak=50_000_000,
        move=80,
        holders=12_000,
        top10=14,
        kols=24,
        liquidity=900_000,
        volume=8_000_000,
        entrapment=0.64,
    )

    assert recap._strong_entrapment_override(organic, settings)
    assert recap._eligible_for_recap(organic, settings)
    assert not any(
        "entrapment-linked flow" in reason
        for reason in recap._publication_safety_reasons(organic, settings)
    )


def test_entrapment_override_never_clears_honeypot(tmp_path):
    settings = _settings(tmp_path)
    trapped_honeypot = _coin(
        "TRAP",
        chain="base",
        holders=12_000,
        top10=14,
        kols=24,
        liquidity=900_000,
        volume=8_000_000,
        entrapment=0.64,
        honeypot=1,
    )

    assert recap._strong_entrapment_override(trapped_honeypot, settings)
    assert not recap._eligible_for_recap(trapped_honeypot, settings)
    assert any(
        "honeypot" in reason.lower()
        for reason in recap._publication_safety_reasons(trapped_honeypot, settings)
    )


def test_discord_result_always_includes_current_market_cap():
    candidate = _coin("MOVER", peak=2_000_000)

    result = recap._result(candidate)

    assert "$2.0M" in result
    assert "now $1.5M" in result


def test_legacy_unknown_authority_labels_do_not_become_live_authorities():
    row = {
        "mint": "legacy-mint",
        "symbol": "LEGACY",
        "chain": "solana",
        "mintAuthorityRenounced": False,
        "freezeAuthorityDisabled": False,
        "riskLabels": [
            "mint authority/contract mintability not confirmed disabled",
            "freeze/pause/blacklist powers not confirmed disabled",
        ],
        "scores": {"runner": 88.0},
    }

    candidate = recap._candidate(row)

    assert candidate.safety.mint_authority_renounced is None
    assert candidate.safety.freeze_authority_disabled is None
    assert candidate.scores["runner"] == 88.0


def test_sectioned_recap_layout_is_preserved():
    candidate = _coin("CATE", peak=74_900_000)
    narrative = {
        "intro": "Short version: what ran and why.",
        "sections": [{
            "title": "Market Leaders",
            "coins": [{"mint": candidate.token.mint, "line": "the cat community accelerated again"}],
        }],
    }

    posts = recap._render_posts([candidate], narrative, recap.datetime(2026, 8, 25))
    rendered = "\n".join(
        embed["description"] for post in posts for embed in post["embeds"]
    )

    assert "**Market Leaders**" in rendered
    assert "$CATE" in rendered
    assert "now" in rendered


def test_market_only_runner_has_metrics_without_filler_lore():
    candidate = _coin("ZOE", peak=4_200_000)
    candidate.token.market_cap = 730_000
    candidate.lore = "No fresh public catalyst was verified"
    candidate.provider_evidence["editorial"] = {"status": "not_found"}
    narrative = recap._approved_layout([candidate])

    posts = recap._render_posts([candidate], narrative, recap.datetime(2026, 8, 27))
    rendered = "\n".join(
        embed["description"] for post in posts for embed in post["embeds"]
    )

    assert "reached $4.2M ATH, now at $730k" in rendered
    assert "no fresh public catalyst" not in rendered.lower()
    assert " — " not in rendered


def test_lore_never_publishes_urls_handles_or_untranslated_copy():
    candidate = _coin("FRANKIE", peak=2_000_000)
    candidate.provider_evidence["editorial"] = {"status": "verified"}

    cleaned = recap._publishable_lore(
        candidate,
        "@SomeAccount named the dog Frankie https://t.co/example after the community adopted him.",
    )
    untranslated = recap._publishable_lore(candidate, "你有球了 https://t.co/example")

    assert cleaned == "named the dog Frankie after the community adopted him."
    assert untranslated == ""


def test_trading_warning_is_never_published_as_lore_or_source_link():
    candidate = _coin("QUAKE", peak=1_400_000)
    candidate.token.market_cap = 300_000
    candidate.provider_evidence["editorial"] = {"status": "partial"}
    warning = (
        "QUAKE's contract-matched X trail was a warning after the coin collapsed "
        "from an earlier call. No separate project story surfaced."
    )

    line = recap._runner_line(
        candidate,
        candidate.token.mint,
        warning,
        "https://x.com/trader/status/1",
    )

    assert "warning" not in line.lower()
    assert "x.com" not in line.lower()
    assert "reached $1.4M ATH, now at $300k" in line


def test_research_process_notes_are_not_lore():
    candidate = _coin("SPARKY", peak=2_300_000)
    candidate.provider_evidence["editorial"] = {"status": "partial"}
    note = (
        "SPARKY is being traded as the official Tesla dog after a contract-matched "
        "post. That social framing is visible, but no affiliation was verified."
    )

    assert recap._publishable_lore(candidate, note) == ""


def test_call_flex_and_bullish_copy_are_never_published_as_lore():
    candidate = _coin("RUNNER", peak=400_000)
    candidate.provider_evidence["editorial"] = {"status": "partial"}

    rejected = (
        "Called this early, LFG bullish. Took it from 20k -> 400k and still "
        "looks bullish for the next leg."
    )

    assert recap._publishable_lore(candidate, rejected) == ""


def test_long_lore_is_never_cut_mid_sentence():
    candidate = _coin("COMPLETE")
    first = "This is a complete origin sentence with enough genuine character context."
    second = "This follow-up sentence is deliberately " + ("long " * 100) + "and should not be cropped."

    rendered = recap._publishable_lore(candidate, f"{first} {second}")

    assert rendered == first
    assert not rendered.endswith("â€¦")


def test_incomplete_lore_fragment_is_not_published():
    candidate = _coin("FRAGMENT")

    assert recap._publishable_lore(
        candidate,
        "A community mascot derived from an old animated character",
    ) == ""


def test_approved_fifteen_name_layout_stays_in_one_discord_message():
    leaders = [_coin(f"LEAD{i}", peak=20_000_000 - i * 1_000_000) for i in range(5)]
    solana = [_coin(f"SOL{i}", peak=8_000_000 - i * 500_000) for i in range(6)]
    cross_chain = [
        _coin(f"OTHER{i}", chain="bsc", peak=4_000_000 - i * 250_000)
        for i in range(4)
    ]
    candidates = [*leaders, *solana, *cross_chain]
    narrative = recap._approved_layout(candidates)

    posts = recap._render_posts(candidates, narrative, recap.datetime(2026, 8, 25))

    assert len(posts) == 1
    rendered = "\n".join(embed["description"] for embed in posts[0]["embeds"])
    assert rendered.count("**Market Leaders**") == 1
    assert rendered.count("**More Solana Runners**") == 1
    assert rendered.count("**Cross-Chain Moves**") == 1
    assert "Continued" not in rendered
    assert "15 featured · 15 total runners · page 1/1 · 11 Solana · 4 cross-chain" in posts[0]["embeds"][-1]["footer"]["text"]


def test_approved_layout_keeps_leaders_solana_and_cross_chain_sections():
    leaders = [_coin(f"LEAD{i}", peak=10_000_000 - i * 100_000) for i in range(5)]
    solana = _coin("SOLTAIL", peak=2_000_000)
    cross_chain = _coin("BNBTAIL", chain="bsc", peak=1_500_000)

    narrative = recap._approved_layout([*leaders, solana, cross_chain])

    assert [section["title"] for section in narrative["sections"]] == [
        "Market Leaders", "More Solana Runners", "Cross-Chain Moves",
    ]
    assert [coin["mint"] for coin in narrative["sections"][0]["coins"]] == [
        candidate.token.mint for candidate in leaders
    ]


@pytest.mark.asyncio
async def test_final_discord_reprice_uses_deepest_exact_market():
    candidate = _coin("LIVE", peak=2_000_000)

    def handler(request):
        assert "/tokens/v1/solana/LIVE-mint" in str(request.url)
        return httpx.Response(200, json=[
            {
                "baseToken": {"address": "LIVE-mint"},
                "marketCap": 1_600_000,
                "liquidity": {"usd": 50_000},
                "volume": {"h24": 900_000},
                "priceChange": {"h24": 70},
            },
            {
                "baseToken": {"address": "LIVE-mint"},
                "marketCap": 1_700_000,
                "liquidity": {"usd": 300_000},
                "volume": {"h24": 2_500_000},
                "priceChange": {"h24": 80},
            },
        ])

    refreshed = await recap._refresh_current_market_caps(
        [candidate], transport=httpx.MockTransport(handler)
    )

    assert refreshed == 1
    assert candidate.token.market_cap == 1_700_000
    assert candidate.peak_market_cap == 2_000_000
    assert "now $1.7M" in recap._result(candidate)
