from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brief.kol import (
    KolTracker,
    MintFlow,
    MintActivity,
    _mints_bought,
    _sol_delta,
    _token_deltas,
    apply_transaction,
    configured_wallets,
)
from tests.conftest import build_settings


WALLET = "WALLETaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WSOL = "So11111111111111111111111111111111111111112"


def transaction(sol_before, sol_after, pre_tokens, post_tokens, *, fee=5000, err=None, block_time=1786000000):
    """A minimal getTransaction shape with the wallet as fee payer."""
    return {
        "blockTime": block_time,
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": "POOL"}]}},
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": [sol_before, 0],
            "postBalances": [sol_after, 0],
            "preTokenBalances": [
                {"owner": WALLET, "mint": mint, "uiTokenAmount": {"uiAmount": amount}}
                for mint, amount in pre_tokens.items()
            ],
            "postTokenBalances": [
                {"owner": WALLET, "mint": mint, "uiTokenAmount": {"uiAmount": amount}}
                for mint, amount in post_tokens.items()
            ],
        },
    }


def test_buy_records_sol_spent_not_the_network_fee():
    """A 2 SOL buy costs 2 SOL; the fee is not part of the position."""
    flows: dict[str, MintFlow] = {}
    tx = transaction(10 * 10**9, 8 * 10**9, {}, {"MINTA": 1000})
    apply_transaction(flows, tx, WALLET)
    assert flows["MINTA"].sol_spent == pytest.approx(2.0, abs=1e-4)
    assert flows["MINTA"].tokens_in == 1000
    assert flows["MINTA"].sol_received == 0


def test_sell_records_realised_profit():
    flows: dict[str, MintFlow] = {}
    apply_transaction(flows, transaction(10 * 10**9, 8 * 10**9, {}, {"MINTA": 1000}), WALLET)
    apply_transaction(flows, transaction(8 * 10**9, 14 * 10**9, {"MINTA": 1000}, {"MINTA": 0}), WALLET)
    flow = flows["MINTA"]
    assert flow.sol_spent == pytest.approx(2.0, abs=1e-4)
    assert flow.sol_received == pytest.approx(6.0, abs=1e-4)
    assert flow.realised_sol == pytest.approx(4.0, abs=1e-4)
    assert not flow.still_holding


def test_partial_sell_leaves_the_position_open():
    flows: dict[str, MintFlow] = {}
    apply_transaction(flows, transaction(10 * 10**9, 8 * 10**9, {}, {"MINTA": 1000}), WALLET)
    apply_transaction(flows, transaction(8 * 10**9, 9 * 10**9, {"MINTA": 1000}, {"MINTA": 700}), WALLET)
    assert flows["MINTA"].still_holding
    assert flows["MINTA"].realised_sol == pytest.approx(-1.0, abs=1e-4)


def test_a_dust_remainder_counts_as_closed():
    """Routers leave crumbs behind; a 1% remainder is not an open position."""
    flows: dict[str, MintFlow] = {}
    apply_transaction(flows, transaction(10 * 10**9, 8 * 10**9, {}, {"MINTA": 1000}), WALLET)
    apply_transaction(flows, transaction(8 * 10**9, 15 * 10**9, {"MINTA": 1000}, {"MINTA": 5}), WALLET)
    assert not flows["MINTA"].still_holding


def test_wrapped_sol_is_not_treated_as_a_position():
    """Counting the wSOL leg would double-count the cost of every swap."""
    flows: dict[str, MintFlow] = {}
    tx = transaction(10 * 10**9, 8 * 10**9, {WSOL: 0}, {WSOL: 2, "MINTA": 1000})
    apply_transaction(flows, tx, WALLET)
    assert set(flows) == {"MINTA"}


def test_multi_hop_splits_sol_across_the_mints_it_moved():
    """One swap must not charge its whole cost to a single leg."""
    flows: dict[str, MintFlow] = {}
    tx = transaction(10 * 10**9, 7 * 10**9, {}, {"MINTA": 100, "MINTB": 100})
    apply_transaction(flows, tx, WALLET)
    assert flows["MINTA"].sol_spent == pytest.approx(1.5, abs=1e-4)
    assert flows["MINTB"].sol_spent == pytest.approx(1.5, abs=1e-4)


def test_failed_transactions_move_nothing():
    flows: dict[str, MintFlow] = {}
    tx = transaction(10 * 10**9, 8 * 10**9, {}, {"MINTA": 1000}, err={"InstructionError": [0, "X"]})
    apply_transaction(flows, tx, WALLET)
    assert flows == {}
    assert _mints_bought(tx, WALLET) == set()


def test_only_this_wallets_balances_are_counted():
    tx = transaction(10 * 10**9, 8 * 10**9, {}, {})
    tx["meta"]["postTokenBalances"].append(
        {"owner": "SOMEONE_ELSE", "mint": "MINTZ", "uiTokenAmount": {"uiAmount": 500}}
    )
    assert _token_deltas(tx, WALLET) == {}


def test_fee_payer_sol_delta_excludes_the_fee():
    tx = transaction(10 * 10**9, 10 * 10**9 - 5000, {}, {}, fee=5000)
    assert _sol_delta(tx, WALLET) == pytest.approx(0.0, abs=1e-9)


def test_a_wallet_absent_from_the_transaction_has_no_delta():
    tx = transaction(10 * 10**9, 8 * 10**9, {}, {})
    assert _sol_delta(tx, "NOT_IN_THIS_TX") == 0.0


def test_wallet_list_is_deduplicated_and_named(tmp_path):
    """The leaderboard repeats wallets across its daily/weekly/monthly boards."""
    settings = build_settings(
        tmp_path / "kol",
        "movers.json",
        extra=(
            "\n[kol]\nenabled = true\nwallets = [\n"
            '  { address = "AAAA1111", name = "Wugi" },\n'
            '  { address = "AAAA1111", name = "Wugi" },\n'
            '  "BBBB2222",\n'
            "]\n"
        ),
    )
    wallets = configured_wallets(settings)
    assert len(wallets) == 2
    assert wallets["AAAA1111"] == "Wugi"
    assert wallets["BBBB2222"] == "BBBB...2222"


def test_the_shipped_wallet_list_has_no_duplicates():
    """Guards the config the report actually runs against."""
    import tomllib
    from pathlib import Path

    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    entries = config["kol"]["wallets"]
    addresses = [entry["address"] for entry in entries]
    assert len(addresses) == len(set(addresses)), "duplicate wallet in config.toml"
    assert all(entry.get("name") for entry in entries), "every wallet needs a name"
    assert len(addresses) == 100


def test_extra_dune_wallets_extend_static_kol_wallets(tmp_path):
    settings = build_settings(
        tmp_path / "kol-extra",
        "movers.json",
        extra=(
            "\n[kol]\nenabled = true\nmax_wallets_per_run = 3\nwallets = [\n"
            '  { address = "AAAA1111", name = "Static A" },\n'
            '  { address = "BBBB2222", name = "Static B" },\n'
            "]\n"
        ),
    )
    tracker = KolTracker(
        object(),
        settings,
        extra_wallets={
            "CCCC3333": "Dune #44 CCCC...3333",
            "AAAA1111": "Duplicate should not replace",
        },
    )
    assert tracker.wallets == {
        "AAAA1111": "Static A",
        "BBBB2222": "Static B",
        "CCCC3333": "Dune #44 CCCC...3333",
    }


def test_a_wallet_that_only_sold_still_counts_as_a_trader():
    """Positions opened before the window close inside it; buyers alone misses them."""
    record = MintActivity(mint="MINTA", buyers=["Wugi"], sellers=["Wugi", "theo"])
    assert record.participants == 2
    assert len(record.buyers) == 1


@pytest.mark.asyncio
async def test_kol_wallet_transactions_include_ata_balance_changes():
    """KOL scans must include associated token-account balance changes."""
    from brief.sources.helius import HeliusSource

    asked = {}

    class FakeHttp:
        async def post_json(self, url, *, family, limit, ttl, json_body, params=None, headers=None):
            asked.update(json_body["params"][1])
            return {"result": {"data": []}}

    source = HeliusSource(FakeHttp(), "https://helius.test", "key", 60)
    await source.wallet_transactions("WALLET", limit=100, since_unix=1786000000)

    filters = asked["filters"]
    assert filters["status"] == "succeeded"
    assert filters["tokenAccounts"] == "balanceChanged"
    assert filters["blockTime"]["gte"] == 1786000000


def test_kol_activity_promotes_unknown_mints_for_market_checks(tmp_path):
    """Tracked-wallet flow is discovery, not an automatic pass."""
    from brief.engine import kol_discovery_mints

    settings = build_settings(tmp_path / "kol-discovery")
    settings.values["kol"] = {
        "max_mints_enriched": 2,
        "min_buyers_to_enrich": 1,
        "min_participants_to_enrich": 3,
        "min_realised_sol_to_enrich": 5.0,
    }
    activity = {
        "MINT_A": MintActivity("MINT_A", buyers=["Wugi"], holders=["Wugi"], sol_spent=4.0),
        "MINT_B": MintActivity("MINT_B", sellers=["theo", "Pain"], realised_sol=2.0),
        "MINT_C": MintActivity("MINT_C", sellers=["Gasp"], realised_sol=9.0),
        "MINT_D": MintActivity("MINT_D", sellers=["Noise"], realised_sol=0.1),
    }

    assert kol_discovery_mints(activity, settings) == ["MINT_A", "MINT_C"]


@pytest.mark.asyncio
async def test_goplus_asks_for_one_contract_at_a_time():
    """A batch answers HTTP 200 and returns a single record regardless.

    Sending twenty addresses looked like it worked and quietly left nineteen
    coins with no safety data at all, so each contract gets its own request.
    """
    from brief.sources.goplus import GoPlusSource

    asked: list[str] = []

    class FakeHttp:
        async def get_json(self, url, *, family, limit, ttl, params=None, headers=None):
            address = params["contract_addresses"]
            asked.append(address)
            assert "," not in address, "batching silently drops every extra address"
            return {"result": {address.lower(): {"holder_count": "1234", "is_mintable": "0"}}}

    source = GoPlusSource(FakeHttp(), "https://goplus.test", 60)
    reports = await source.reports("base", ["0xAAA", "0xBBB", "0xCCC"])

    assert len(asked) == 3
    assert set(reports) == {"0xAAA", "0xBBB", "0xCCC"}
    assert reports["0xAAA"].holder_count == 1234
