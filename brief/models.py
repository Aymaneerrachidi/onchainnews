from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class TransactionWindow:
    buys: int = 0
    sells: int = 0
    makers: int | None = None

    @property
    def total(self) -> int:
        return self.buys + self.sells

    @property
    def buy_ratio(self) -> float | None:
        return self.buys / self.total if self.total else None


@dataclass(slots=True)
class TokenSnapshot:
    mint: str
    symbol: str
    name: str
    chain_id: str
    pair_address: str
    url: str
    price_usd: float
    market_cap: float
    liquidity_usd: float
    volume_24h: float
    volume_6h: float
    price_change_24h: float
    price_change_6h: float
    pair_created_at: datetime | None
    dex_id: str = "unknown"
    price_change_1h: float = 0.0
    price_change_5m: float = 0.0
    volume_by_dex: dict[str, float] = field(default_factory=dict)
    txns_1h: TransactionWindow = field(default_factory=TransactionWindow)
    txns_6h: TransactionWindow = field(default_factory=TransactionWindow)
    socials: list[dict[str, str]] = field(default_factory=list)
    active_boosts: int = 0
    from_profile: bool = False
    meta_slugs: set[str] = field(default_factory=set)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class MetaSnapshot:
    slug: str
    name: str
    market_cap: float
    liquidity: float
    volume_24h: float
    token_count: int
    change_24h: float
    change_6h: float


@dataclass(slots=True)
class CTORecord:
    mint: str
    claim_date: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class SafetyReport:
    mint: str
    mint_authority_renounced: bool | None = None
    freeze_authority_disabled: bool | None = None
    lp_locked_or_burned_pct: float | None = None
    top10_pct: float | None = None
    risk_flags: list[str] = field(default_factory=list)
    excluded_accounts: set[str] = field(default_factory=set)
    excluded_owners: set[str] = field(default_factory=set)
    lp_vaults: tuple[str, ...] = ()
    creator: str | None = None
    source: str = "unavailable"


@dataclass(slots=True)
class Enrichment:
    holder_count: int | None = None
    holder_change_24h: int | None = None
    unique_makers_24h: int | None = None
    mint_authority_renounced: bool | None = None
    freeze_authority_disabled: bool | None = None
    supply_raw: float | None = None
    decimals: int | None = None
    social_resolves: bool | None = None
    social_account_age_days: float | None = None
    source: str = "unavailable"


@dataclass(slots=True)
class Signals:
    turnover: float
    acceleration: float
    buy_imbalance_1h: float | None
    buy_imbalance_6h: float | None
    liquidity_depth: float
    holder_growth_24h: int | None
    maker_quality: float | None
    age_hours: float | None
    cto_volume_since_claim: float | None = None
    cto_holder_change: int | None = None


@dataclass(slots=True)
class Candidate:
    token: TokenSnapshot
    signals: Signals
    safety: SafetyReport
    enrichment: Enrichment
    badges: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    follow_up_multiple: float | None = None
    cto: CTORecord | None = None
    exit_liquidity_sol: float | None = None
    strength_reasons: list[str] = field(default_factory=list)
    interest_reasons: list[str] = field(default_factory=list)
    editorial_gaps: list[str] = field(default_factory=list)
    recycled_label_count: int = 0
    # "NEW", "MOVER" or "CTO": which daily track put this name in the brief.
    track: str = "NEW"
    # Problems worth seeing but not worth hiding the coin over. A rug or a
    # bundle is excluded outright; everything else is labelled and shown.
    risk_labels: list[str] = field(default_factory=list)
    # Shared-narrative grouping, e.g. a meta slug or a common name token.
    lore: str = ""
    lore_is_fresh: bool = True
    # Distinct tracked KOL wallets that bought inside the window.
    kol_buyers: list[str] = field(default_factory=list)
    kol_sellers: list[str] = field(default_factory=list)
    kol_holders: list[str] = field(default_factory=list)
    kol_realised_sol: float = 0.0
    kol_sol_spent: float = 0.0
    run_multiple: float = 1.0
    faded_from_peak: float | None = None
    # One descriptive sentence assembled from measured values, never a forecast.
    read: str = ""


@dataclass(slots=True)
class Exclusion:
    token: TokenSnapshot
    reasons: list[str]
    stage: str


@dataclass(slots=True)
class SourceStatus:
    name: str
    available: bool
    detail: str = ""


@dataclass(slots=True)
class Scorecard:
    featured_count: int = 0
    featured_median_72h: float | None = None
    featured_up_pct_72h: float | None = None
    excluded_count: int = 0
    excluded_median_72h: float | None = None
    excluded_up_pct_72h: float | None = None
    featured_q1_72h: float | None = None
    featured_q3_72h: float | None = None
    featured_crash_pct_72h: float | None = None
    traded_count: int = 0
    traded_median_72h: float | None = None
    skipped_count: int = 0
    skipped_median_72h: float | None = None


@dataclass(slots=True)
class HolderBalance:
    owner: str
    amount: float
    accounts: tuple[str, ...] = ()


@dataclass(slots=True)
class HolderSnapshot:
    mint: str
    taken_at: datetime
    holder_count: int
    top10_pct: float
    top50_pct: float
    gini: float
    total_amount: float
    balances: list[HolderBalance]
    excluded_accounts: int = 0


@dataclass(slots=True)
class WalletTrace:
    owner: str
    first_funder: str | None
    first_funded_at: datetime | None
    wallet_created_at: datetime | None
    complete: bool = True


@dataclass(slots=True)
class AcquisitionTrace:
    mint: str
    owner: str
    first_acquired_at: datetime | None
    initial_amount: float | None


@dataclass(slots=True)
class OnChainFinding:
    mint: str
    symbol: str
    priority: int
    headline: str
    details: list[str] = field(default_factory=list)
    status: str = "available"

    @property
    def bubblemap_url(self) -> str:
        return f"https://app.bubblemaps.io/sol/token/{self.mint}"


@dataclass(slots=True)
class LaunchRecord:
    token: TokenSnapshot
    status: str
    reasons: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Brief:
    generated_at: datetime
    scorecard: Scorecard
    metas: list[MetaSnapshot]
    new_and_moving: list[Candidate]
    ctos: list[Candidate]
    follow_ups: list[Candidate]
    onchain: list[OnChainFinding]
    excluded: list[Exclusion]
    source_statuses: list[SourceStatus]
    movers: list[Candidate] = field(default_factory=list)
    # The day's record: every coin that ran, risk labelled rather than hidden.
    runners: list[Candidate] = field(default_factory=list)
    blocked_runners: list[Candidate] = field(default_factory=list)
    lore_groups: dict[str, list[Candidate]] = field(default_factory=dict)
    kol_flagged: list[Candidate] = field(default_factory=list)
    kol_wallet_count: int = 0
    # Where the tracked wallets actually made money in the window.
    kol_profit_table: list[tuple[str, str, float, int]] = field(default_factory=list)
    quality_alerts: list[str] = field(default_factory=list)
    weekly_notes: list[str] = field(default_factory=list)
    window_start: datetime | None = None
    launches_last_24h: list[LaunchRecord] = field(default_factory=list)
    discovered_solana_count: int = 0
    cleared_launch_count: int = 0
    discovery_note: str = ""
    strongest_definition: str = ""
    interesting_definition: str = ""
    selection_rule: str = ""
    raw_launch_count: int = 0
    indexed_launch_count: int = 0
    collector_started_at: datetime | None = None
