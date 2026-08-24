from __future__ import annotations

import importlib.util
from pathlib import Path

from conftest import build_settings


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
