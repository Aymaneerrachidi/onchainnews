from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from brief.sources.dune import DuneSource
from tests.conftest import build_settings


@pytest.mark.asyncio
async def test_dune_alpha_wallets_are_filtered_and_named(tmp_path, monkeypatch):
    monkeypatch.setenv("DUNE_API_KEY", "test-key")
    settings = build_settings(tmp_path / "dune")
    settings.values["dune"] = {
        "enabled": True,
        "alpha_wallet_query_id": 4032586,
        "alpha_wallet_fetch_limit": 10,
        "alpha_wallets_to_add": 2,
        "alpha_wallet_skip_top_n": 1,
        "alpha_wallet_active_days": 30,
        "alpha_min_profit_usd": 10_000,
        "alpha_max_profit_usd": 25_000_000,
        "requests_per_minute": 12,
        "cache_ttl_seconds": 60,
    }

    calls = {}

    class FakeHttp:
        async def get_json(self, url, *, family, limit, ttl, headers=None, params=None):
            calls.update(url=url, family=family, limit=limit, ttl=ttl, headers=headers, params=params)
            return {
                "result": {
                    "rows": [
                        {
                            "rank": 1,
                            "wallet": "ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn",
                            "realized_profit": 1_000_000_000,
                            "last_tx": "2026-08-20 12:00:00.000 UTC",
                        },
                        {
                            "rank": 2,
                            "wallet": "not a wallet",
                            "realized_profit": 50_000,
                            "last_tx": "2026-08-20 12:00:00.000 UTC",
                        },
                        {
                            "rank": 3,
                            "wallet": "BjYxVF81MgahqgahDTUEGzxzP7bZrA4p5Dg67Y4e3bXZ",
                            "realized_profit": 50_000,
                            "last_tx": "2026-07-01 12:00:00.000 UTC",
                        },
                        {
                            "rank": 4,
                            "wallet": "7K7itu678xAaUcuPQ2f3c2DcjirRjBY4HMTW1dx6hiL6",
                            "realized_profit": 50_000,
                            "last_tx": "2026-08-20 12:00:00.000 UTC",
                        },
                        {
                            "rank": 5,
                            "wallet": "8rvAsDKeAcEjEkiZMug9k8v1y8mW6gQQiMobd89Uy7qR",
                            "realized_profit": 40_000,
                            "last_tx": "2026-08-20 12:00:00.000 UTC",
                        },
                    ]
                }
            }

    source = DuneSource(FakeHttp(), settings)
    wallets = await source.alpha_wallets(datetime(2026, 8, 22, tzinfo=ZoneInfo("UTC")))

    assert calls["family"] == "dune"
    assert calls["headers"]["X-DUNE-API-KEY"] == "test-key"
    assert calls["params"] == {"limit": 10}
    assert [wallet.address for wallet in wallets] == [
        "7K7itu678xAaUcuPQ2f3c2DcjirRjBY4HMTW1dx6hiL6",
        "8rvAsDKeAcEjEkiZMug9k8v1y8mW6gQQiMobd89Uy7qR",
    ]
    assert wallets[0].name.startswith("Dune #4 ")
