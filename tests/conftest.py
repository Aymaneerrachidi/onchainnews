from __future__ import annotations

from pathlib import Path

import pytest

from brief.config import load_settings


@pytest.fixture(autouse=True)
def clear_api_keys(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)


def build_settings(tmp_path: Path, fixture_name: str = "run.json", extra: str = ""):
    """Write a config pointed at an offline fixture, with optional extra sections."""
    fixture = (Path(__file__).parent / "fixtures" / fixture_name).resolve().as_posix()
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[run]
timezone = "Europe/Paris"
top_tokens = 10
database_path = "brief.db"
html_path = "latest.html"
request_timeout_seconds = 1
log_level = "WARNING"
archive_retention_days = 14
fixture_path = "{fixture}"

[thresholds]
chains = ["solana"]
min_market_cap = 150000.0
min_liquidity = 20000.0
min_volume_24h = 25000.0
min_turnover_24h = 0.15
max_volume_liquidity_ratio = 25.0
max_volume_liquidity_ratio_established = 75.0
max_top10_pct = 30.0
novelty_days = 7
follow_up_multiple = 3.0
retire_after_features = 3
max_pair_idle_hours = 6

[ranking]
cto_bonus = 1.0
profile_bonus = 0.25
boost_without_growth_penalty = 1.0

[editorial]
min_strength_signals = 3
min_interest_signals = 2
max_shortlist = 5
recycled_symbol_lookback_days = 30

[movers]
enabled = true
max_movers = 5
min_strength_signals = 2
min_price_change_24h = 25.0
min_turnover = 0.5
min_volume_24h = 100000.0
max_age_days = 120

[cto]
enabled = true
max_ctos = 3
max_claim_age_days = 7
min_strength_signals = 2

[birdeye]
enabled = true
max_tokens = 100
page_size = 50
requests_per_minute = 50

[journal]
enabled = true
max_runners = 40
min_volume_24h = 50000.0
fresh_window_hours = 24
min_fresh_change_pct = 30.0
old_coin_multiple = 5.0
max_age_days = 0
kol_buyers_door = 0
expect_tracked_wallets_above = 10.0
max_credible_multiple = 1000.0
corroborate_above_multiple = 10.0
min_turnover_for_big_run = 0.15
max_volume_liquidity = 150.0
max_turnover = 30.0
min_average_trade_usd = 15.0
min_holders = 200
min_trades_24h = 300
min_recent_volume_share = 0.08
dead_check_above_multiple = 5.0
max_sell_tax_pct = 15.0
bundle_top10_pct = 50.0
caution_top10_pct = 25.0
thin_liquidity_ratio = 60.0
min_lore_group = 2
venues = {{}}

[cache]
pairs_ttl_seconds = 60
discovery_ttl_seconds = 600
safety_ttl_seconds = 3600
keyed_ttl_seconds = 900

[holders]
enabled = true
watchlist_limit = 2
cluster_top_holders = 100
max_wallet_history_calls_per_run = 200
history_concurrency = 2

[sources]
dexscreener_base_url = "https://api.dexscreener.test"
birdeye_base_url = "https://birdeye.test"
rugcheck_base_url = "https://api.rugcheck.test/v1"
helius_base_url = "https://helius.test"

[delivery]
telegram_enabled = false
html_enabled = true
telegram_digest = true
report_url = ""
{extra}
""",
        encoding="utf-8",
    )
    return load_settings(config)


@pytest.fixture
def settings(tmp_path: Path):
    return build_settings(tmp_path)


@pytest.fixture
def mover_settings(tmp_path: Path):
    return build_settings(tmp_path, "movers.json")
