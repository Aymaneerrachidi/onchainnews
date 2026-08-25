from __future__ import annotations

import hashlib
import json
import sqlite3
import math
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from brief.models import AcquisitionTrace, HolderBalance, HolderSnapshot, Scorecard, WalletTrace


UTC = timezone.utc


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _pack(body: str) -> bytes:
    """Compress an archived response body.

    Holder pages from Helius are large JSON arrays and dominate the archive;
    storing them verbatim is what grew the database past a gigabyte in a single
    run. SQLite typing is per-value, so compressed rows sit alongside older
    plain-text ones and `_unpack` handles both.
    """
    return zlib.compress(body.encode("utf-8"), 6)


def _unpack(body: str | bytes) -> str:
    if isinstance(body, bytes):
        return zlib.decompress(body).decode("utf-8")
    return body


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def open_ledger(settings: Any) -> "Ledger":
    """Single place that applies archive settings to a ledger connection."""
    return Ledger(
        settings.path("run", "database_path"),
        compress_archive=bool(settings.get("run", "archive_compress", True)),
    )


class Ledger:
    def __init__(self, path: str | Path, *, compress_archive: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The launch collector, hourly pulse, interface and morning report are
        # intentionally separate processes sharing this database. WAL permits
        # that, but SQLite's short default busy timeout still surfaced transient
        # writer overlap as lost provider evidence. Wait for the active writer
        # instead of dropping the token currently being enriched.
        self.db = sqlite3.connect(self.path, timeout=30.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.compress_archive = compress_archive
        self._migrate()

    def _write_retry(self, operation, *, attempts: int = 8):
        """Run one atomic write unit, retrying transient SQLite contention.

        The collector, interface and report runner are separate processes that
        intentionally share this WAL database.  A busy timeout handles ordinary
        overlap, but a continuously active writer can still win the lock again
        immediately after the timeout.  Roll back the local connection before
        retrying so one failed write never poisons every later write in the run.
        """
        delay = 0.05
        for attempt in range(attempts):
            try:
                with self.db:
                    return operation()
            except sqlite3.OperationalError as exc:
                self.db.rollback()
                if "locked" not in str(exc).lower() or attempt + 1 >= attempts:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS featured (
              mint TEXT PRIMARY KEY,
              symbol TEXT,
              first_seen TEXT NOT NULL,
              last_featured TEXT NOT NULL,
              times_featured INTEGER NOT NULL,
              best_rank INTEGER NOT NULL,
              mcap_at_last_feature REAL NOT NULL,
              retired INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mint TEXT NOT NULL,
              symbol TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('featured', 'excluded')),
              observed_at TEXT NOT NULL,
              market_cap REAL NOT NULL,
              rank INTEGER,
              UNIQUE(mint, kind, observed_at)
            );
            CREATE TABLE IF NOT EXISTS forward_returns (
              observation_id INTEGER NOT NULL,
              horizon_hours INTEGER NOT NULL,
              measured_at TEXT NOT NULL,
              market_cap REAL NOT NULL,
              return_pct REAL NOT NULL,
              PRIMARY KEY(observation_id, horizon_hours),
              FOREIGN KEY(observation_id) REFERENCES observations(id)
            );
            CREATE TABLE IF NOT EXISTS api_cache (
              cache_key TEXT PRIMARY KEY,
              fetched_at TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              holder_count INTEGER NOT NULL,
              top10_pct REAL NOT NULL,
              top50_pct REAL NOT NULL,
              gini REAL NOT NULL,
              PRIMARY KEY (mint, taken_at)
            );
            CREATE TABLE IF NOT EXISTS balances (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              owner TEXT NOT NULL,
              amount REAL NOT NULL,
              PRIMARY KEY (mint, taken_at, owner)
            );
            CREATE TABLE IF NOT EXISTS snapshot_context (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              price_usd REAL,
              market_cap REAL,
              total_amount REAL NOT NULL,
              excluded_accounts INTEGER NOT NULL DEFAULT 0,
              pair_created_at TEXT,
              PRIMARY KEY (mint, taken_at)
            );
            CREATE TABLE IF NOT EXISTS watchlist (
              mint TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              added_at TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              reason TEXT NOT NULL DEFAULT 'manual'
            );
            CREATE TABLE IF NOT EXISTS wallet_traces (
              owner TEXT PRIMARY KEY,
              first_funder TEXT,
              first_funded_at TEXT,
              wallet_created_at TEXT,
              checked_at TEXT NOT NULL,
              complete INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS acquisition_traces (
              mint TEXT NOT NULL,
              owner TEXT NOT NULL,
              first_acquired_at TEXT,
              initial_amount REAL,
              checked_at TEXT NOT NULL,
              PRIMARY KEY (mint, owner)
            );
            CREATE TABLE IF NOT EXISTS snapshot_clusters (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              effective_top10_pct REAL,
              cluster_count INTEGER NOT NULL,
              coverage INTEGER NOT NULL,
              PRIMARY KEY (mint, taken_at)
            );
            CREATE TABLE IF NOT EXISTS clusters (
              cluster_id TEXT PRIMARY KEY,
              funder TEXT,
              wallets TEXT NOT NULL,
              first_seen TEXT NOT NULL,
              tokens_seen TEXT NOT NULL,
              outcomes TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pool_snapshots (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              vault_balances TEXT NOT NULL,
              liquidity_proxy REAL,
              dex_liquidity_usd REAL,
              PRIMARY KEY(mint,taken_at)
            );
            CREATE TABLE IF NOT EXISTS creator_snapshots (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              creator TEXT,
              linked_wallets TEXT NOT NULL,
              amount REAL NOT NULL,
              supply_pct REAL NOT NULL,
              PRIMARY KEY(mint,taken_at)
            );
            CREATE TABLE IF NOT EXISTS pair_history (
              mint TEXT NOT NULL,
              pair_address TEXT NOT NULL,
              dex_id TEXT NOT NULL,
              pair_created_at TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              PRIMARY KEY(mint,pair_address)
            );
            CREATE TABLE IF NOT EXISTS token_metrics (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              metric TEXT NOT NULL,
              value REAL NOT NULL,
              PRIMARY KEY(mint,taken_at,metric)
            );
            CREATE TABLE IF NOT EXISTS raw_responses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              captured_at TEXT NOT NULL,
              run_date TEXT NOT NULL,
              method TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              request_params TEXT NOT NULL,
              request_body TEXT,
              status INTEGER NOT NULL,
              response_body TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS raw_responses_lookup ON raw_responses(run_date,method,endpoint);
            CREATE TABLE IF NOT EXISTS early_wallets (
              mint TEXT NOT NULL,
              owner TEXT NOT NULL,
              acquired_at TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              PRIMARY KEY(mint,owner)
            );
            CREATE TABLE IF NOT EXISTS trade_feedback (
              mint TEXT PRIMARY KEY,
              decision TEXT NOT NULL CHECK(decision IN ('traded','skipped')),
              marked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS overlap_snapshots (
              mint_a TEXT NOT NULL,
              mint_b TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              overlap_pct REAL NOT NULL,
              PRIMARY KEY(mint_a,mint_b,taken_at)
            );
            CREATE TABLE IF NOT EXISTS market_context (
              taken_at TEXT PRIMARY KEY,
              sol_price_usd REAL
            );
            CREATE TABLE IF NOT EXISTS data_quality (
              run_date TEXT NOT NULL,
              field TEXT NOT NULL,
              null_count INTEGER NOT NULL,
              total_count INTEGER NOT NULL,
              mean_value REAL,
              PRIMARY KEY(run_date,field)
            );
            CREATE TABLE IF NOT EXISTS intelligence_events (
              event_key TEXT PRIMARY KEY,
              mint TEXT NOT NULL,
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watcher_samples (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              holder_count INTEGER,
              balances TEXT NOT NULL,
              creator_amount REAL,
              cluster_amount REAL,
              PRIMARY KEY(mint,taken_at)
            );
            CREATE TABLE IF NOT EXISTS launch_events (
              mint TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              signature TEXT NOT NULL,
              creator TEXT,
              created_at TEXT NOT NULL,
              slot INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_launch_events_created_at
              ON launch_events(created_at);
            CREATE TABLE IF NOT EXISTS collector_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_tokens (
              mint TEXT PRIMARY KEY,
              chain TEXT NOT NULL,
              symbol TEXT NOT NULL,
              name TEXT NOT NULL,
              created_at TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_pools (
              mint TEXT NOT NULL,
              pair_address TEXT NOT NULL,
              dex_id TEXT NOT NULL,
              created_at TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              PRIMARY KEY(mint,pair_address)
            );
            CREATE TABLE IF NOT EXISTS market_snapshots (
              mint TEXT NOT NULL,
              taken_at TEXT NOT NULL,
              provider TEXT NOT NULL,
              price_usd REAL,
              market_cap REAL,
              liquidity_usd REAL,
              volume_24h REAL,
              holder_count INTEGER,
              top10_pct REAL,
              raw_json TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY(mint,taken_at,provider)
            );
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_mint_time
              ON market_snapshots(mint,taken_at);
            CREATE TABLE IF NOT EXISTS market_milestones (
              mint TEXT NOT NULL,
              level TEXT NOT NULL,
              reached_at TEXT NOT NULL,
              market_cap REAL NOT NULL,
              source TEXT NOT NULL,
              PRIMARY KEY(mint,level)
            );
            CREATE TABLE IF NOT EXISTS lifecycle_events (
              event_key TEXT PRIMARY KEY,
              mint TEXT NOT NULL,
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_lifecycle_events_mint_time
              ON lifecycle_events(mint,occurred_at);
            CREATE TABLE IF NOT EXISTS wallet_events (
              event_key TEXT PRIMARY KEY,
              mint TEXT NOT NULL,
              wallet TEXT NOT NULL,
              wallet_kind TEXT NOT NULL,
              side TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              amount_usd REAL,
              realised_profit REAL,
              payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_events_mint_time
              ON wallet_events(mint,occurred_at);
            CREATE TABLE IF NOT EXISTS provider_health (
              provider TEXT PRIMARY KEY,
              last_success TEXT,
              last_failure TEXT,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              circuit_open_until TEXT,
              detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS daily_recaps (
              recap_date TEXT PRIMARY KEY,
              generated_at TEXT NOT NULL,
              window_start TEXT NOT NULL,
              window_end TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def record_provider_health(
        self, provider: str, ok: bool, now: datetime, detail: str = "", *,
        circuit_open_until: datetime | None = None,
    ) -> None:
        stamp = iso(now)
        def write() -> None:
            if ok:
                self.db.execute(
                    """INSERT INTO provider_health(provider,last_success,consecutive_failures,circuit_open_until,detail)
                       VALUES(?,?,0,NULL,?)
                       ON CONFLICT(provider) DO UPDATE SET last_success=excluded.last_success,
                       consecutive_failures=0,circuit_open_until=NULL,detail=excluded.detail""",
                    (provider, stamp, detail),
                )
            else:
                self.db.execute(
                    """INSERT INTO provider_health(provider,last_failure,consecutive_failures,circuit_open_until,detail)
                       VALUES(?,?,1,?,?)
                       ON CONFLICT(provider) DO UPDATE SET last_failure=excluded.last_failure,
                       consecutive_failures=provider_health.consecutive_failures+1,
                       circuit_open_until=excluded.circuit_open_until,detail=excluded.detail""",
                    (provider, stamp, iso(circuit_open_until) if circuit_open_until else None, detail),
                )

        self._write_retry(write)

    def provider_state(self, provider: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM provider_health WHERE provider=?", (provider,)
        ).fetchone()

    def provider_states(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM provider_health ORDER BY provider"
        ).fetchall()

    def record_market_snapshot(
        self, token: Any, now: datetime, *, provider: str,
        holder_count: int | None = None, top10_pct: float | None = None,
        raw: dict[str, Any] | None = None,
    ) -> list[str]:
        """Persist one market observation and return newly crossed milestones."""
        stamp = iso(now)
        created = iso(token.pair_created_at) if token.pair_created_at else None
        self.db.execute(
            """INSERT INTO lifecycle_tokens(mint,chain,symbol,name,created_at,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(mint) DO UPDATE SET chain=excluded.chain,symbol=excluded.symbol,
               name=excluded.name,created_at=COALESCE(lifecycle_tokens.created_at,excluded.created_at),
               last_seen=excluded.last_seen""",
            (token.mint, token.chain_id, token.symbol, token.name, created, stamp, stamp),
        )
        if token.pair_address:
            self.db.execute(
                """INSERT INTO lifecycle_pools(mint,pair_address,dex_id,created_at,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(mint,pair_address) DO UPDATE SET dex_id=excluded.dex_id,
                   created_at=COALESCE(lifecycle_pools.created_at,excluded.created_at),last_seen=excluded.last_seen""",
                (token.mint, token.pair_address, token.dex_id, created, stamp, stamp),
            )
        self.db.execute(
            """INSERT OR REPLACE INTO market_snapshots(
                 mint,taken_at,provider,price_usd,market_cap,liquidity_usd,volume_24h,
                 holder_count,top10_pct,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                token.mint, stamp, provider, token.price_usd, token.market_cap,
                token.liquidity_usd, token.volume_24h, holder_count, top10_pct,
                json.dumps(raw or {}, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        crossed: list[str] = []
        levels = (
            ("TIER_B_250K", 250_000.0), ("TIER_A_500K", 500_000.0),
            ("TIER_S_1M", 1_000_000.0),
            ("MAJOR_5M", 5_000_000.0), ("MAJOR_10M", 10_000_000.0),
        )
        for level, floor in levels:
            if float(token.market_cap or 0) < floor:
                continue
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO market_milestones(mint,level,reached_at,market_cap,source) VALUES(?,?,?,?,?)",
                (token.mint, level, stamp, token.market_cap, provider),
            )
            if cursor.rowcount:
                crossed.append(level)
                self.db.execute(
                    "INSERT OR IGNORE INTO lifecycle_events(event_key,mint,event_type,occurred_at,payload) VALUES(?,?,?,?,?)",
                    (
                        f"milestone:{token.mint}:{level}", token.mint, "milestone", stamp,
                        json.dumps({"level": level, "marketCap": token.market_cap, "source": provider}),
                    ),
                )
        self.db.commit()
        return crossed

    def lifecycle(self, mint: str, window_start: datetime, now: datetime) -> dict[str, Any] | None:
        rows = self.db.execute(
            """SELECT * FROM market_snapshots WHERE mint=? AND taken_at>=? AND taken_at<=?
               AND market_cap IS NOT NULL AND market_cap>0 ORDER BY taken_at""",
            (mint, iso(window_start), iso(now)),
        ).fetchall()
        if not rows:
            return None
        first = rows[0]
        latest = rows[-1]
        peak = max(rows, key=lambda row: float(row["market_cap"] or 0))
        events = self.db.execute(
            "SELECT event_type,occurred_at,payload FROM lifecycle_events WHERE mint=? AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at",
            (mint, iso(window_start), iso(now)),
        ).fetchall()
        return {
            "first_seen_at": first["taken_at"],
            "last_seen_at": latest["taken_at"],
            "start_market_cap": float(first["market_cap"]),
            "current_market_cap": float(latest["market_cap"]),
            "peak_market_cap": float(peak["market_cap"]),
            "peak_at": peak["taken_at"],
            "providers": sorted({str(row["provider"]) for row in rows}),
            "events": [
                {"type": row["event_type"], "occurredAt": row["occurred_at"], **json.loads(row["payload"] or "{}")}
                for row in events
            ],
        }

    def record_wallet_event(
        self, *, event_key: str, mint: str, wallet: str, wallet_kind: str,
        side: str, occurred_at: datetime, amount_usd: float | None,
        realised_profit: float | None = None, payload: dict[str, Any] | None = None,
    ) -> bool:
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO wallet_events(event_key,mint,wallet,wallet_kind,side,
               occurred_at,amount_usd,realised_profit,payload) VALUES(?,?,?,?,?,?,?,?,?)""",
            (event_key, mint, wallet, wallet_kind, side, iso(occurred_at), amount_usd,
             realised_profit, json.dumps(payload or {}, separators=(",", ":"))),
        )
        self.db.commit()
        return bool(cursor.rowcount)

    def wallet_events_for(self, mint: str, window_start: datetime, now: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM wallet_events WHERE mint=? AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at",
            (mint, iso(window_start), iso(now)),
        ).fetchall()

    def save_daily_recap(self, recap_date: str, generated_at: datetime, window_start: datetime, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO daily_recaps(recap_date,generated_at,window_start,window_end,payload) VALUES(?,?,?,?,?)",
            (recap_date, iso(generated_at), iso(window_start), iso(generated_at), json.dumps(payload, ensure_ascii=False)),
        )
        self.db.commit()

    def record_launch_event(
        self,
        mint: str,
        source: str,
        signature: str,
        creator: str | None,
        created_at: datetime,
        slot: int | None,
    ) -> bool:
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO launch_events(mint,source,signature,creator,created_at,slot)
            VALUES(?,?,?,?,?,?)
            """,
            (mint, source, signature, creator, iso(created_at), slot),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO collector_state(key,value) VALUES('last_event_at',?)",
            (iso(created_at),),
        )
        self.db.commit()
        return bool(cursor.rowcount)

    def launch_events_between(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM launch_events WHERE created_at>=? AND created_at<=? ORDER BY created_at DESC",
            (iso(start), iso(end)),
        ).fetchall()

    def collector_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM collector_state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_collector_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)", (key, value)
        )
        self.db.commit()

    @staticmethod
    def cache_key(method: str, url: str, params: Any, headers: Any) -> str:
        material = json.dumps([method, url, params or {}, headers or {}], sort_keys=True, default=str)
        return hashlib.sha256(material.encode()).hexdigest()

    def cache_get(self, key: str, ttl: int | None) -> Any | None:
        row = self.db.execute("SELECT fetched_at, payload FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        if ttl is not None and datetime.now(UTC) - fetched > timedelta(seconds=ttl):
            return None
        return json.loads(row["payload"])

    def cache_put(self, key: str, payload: Any) -> None:
        def write() -> None:
            self.db.execute(
                "INSERT INTO api_cache(cache_key, fetched_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
                (key, iso(datetime.now(UTC)), json.dumps(payload, separators=(",", ":"))),
            )

        self._write_retry(write)

    def feature_state(self, mint: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM featured WHERE mint = ?", (mint,)).fetchone()

    def add_watch(self, mint: str, symbol: str, now: datetime, reason: str = "manual") -> None:
        self.db.execute(
            "INSERT INTO watchlist(mint,symbol,added_at,active,reason) VALUES(?,?,?,1,?) "
            "ON CONFLICT(mint) DO UPDATE SET symbol=excluded.symbol,active=1,reason=CASE WHEN excluded.reason='manual' THEN 'manual' ELSE watchlist.reason END",
            (mint, symbol.upper(), iso(now), reason),
        )
        self.db.commit()

    def remove_watch(self, mint: str) -> bool:
        cursor = self.db.execute("UPDATE watchlist SET active=0 WHERE mint=?", (mint,))
        self.db.commit()
        return bool(cursor.rowcount)

    def watched(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM watchlist WHERE active=1 ORDER BY CASE reason WHEN 'manual' THEN 0 ELSE 1 END, added_at"
        ).fetchall()

    def fill_auto_watchlist(self, tokens: Iterable[Any], limit: int, now: datetime) -> list[sqlite3.Row]:
        active = self.watched()
        known = {row["mint"] for row in active}
        for token in tokens:
            if len(active) >= limit:
                break
            if token.mint in known:
                continue
            self.add_watch(token.mint, token.symbol, now, reason="auto")
            known.add(token.mint)
            active = self.watched()
        return active[:limit]

    def record_holder_snapshot(self, snapshot: HolderSnapshot, *, price_usd: float | None, market_cap: float | None, pair_created_at: datetime | None) -> None:
        stamp = iso(snapshot.taken_at)
        def write() -> None:
            self.db.execute(
                "INSERT OR REPLACE INTO snapshots(mint,taken_at,holder_count,top10_pct,top50_pct,gini) VALUES(?,?,?,?,?,?)",
                (snapshot.mint, stamp, snapshot.holder_count, snapshot.top10_pct, snapshot.top50_pct, snapshot.gini),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO snapshot_context(mint,taken_at,price_usd,market_cap,total_amount,excluded_accounts,pair_created_at) VALUES(?,?,?,?,?,?,?)",
                (snapshot.mint, stamp, price_usd, market_cap, snapshot.total_amount, snapshot.excluded_accounts, iso(pair_created_at) if pair_created_at else None),
            )
            self.db.execute("DELETE FROM balances WHERE mint=? AND taken_at=?", (snapshot.mint, stamp))
            self.db.executemany(
                "INSERT INTO balances(mint,taken_at,owner,amount) VALUES(?,?,?,?)",
                ((snapshot.mint, stamp, balance.owner, balance.amount) for balance in snapshot.balances),
            )

        self._write_retry(write)

    def snapshot_at_or_before(self, mint: str, when: datetime, *, exclude_taken_at: str | None = None) -> sqlite3.Row | None:
        query = """SELECT s.*,c.price_usd,c.market_cap,c.total_amount,c.excluded_accounts,c.pair_created_at
                   FROM snapshots s JOIN snapshot_context c USING(mint,taken_at)
                   WHERE s.mint=? AND s.taken_at<=?"""
        params: list[Any] = [mint, iso(when)]
        if exclude_taken_at:
            query += " AND s.taken_at<>?"
            params.append(exclude_taken_at)
        query += " ORDER BY s.taken_at DESC LIMIT 1"
        return self.db.execute(query, params).fetchone()

    def balances_for(self, mint: str, taken_at: str) -> dict[str, float]:
        return {
            row["owner"]: row["amount"]
            for row in self.db.execute("SELECT owner,amount FROM balances WHERE mint=? AND taken_at=?", (mint, taken_at))
        }

    def record_cluster_snapshot(self, mint: str, taken_at: datetime, effective_top10_pct: float | None, cluster_count: int, coverage: int) -> None:
        self._write_retry(lambda: self.db.execute(
            "INSERT OR REPLACE INTO snapshot_clusters(mint,taken_at,effective_top10_pct,cluster_count,coverage) VALUES(?,?,?,?,?)",
            (mint, iso(taken_at), effective_top10_pct, cluster_count, coverage),
        ))

    def cluster_at_or_before(self, mint: str, when: datetime, *, exclude_taken_at: str | None = None) -> sqlite3.Row | None:
        query = "SELECT * FROM snapshot_clusters WHERE mint=? AND taken_at<=?"
        params: list[Any] = [mint, iso(when)]
        if exclude_taken_at:
            query += " AND taken_at<>?"
            params.append(exclude_taken_at)
        query += " ORDER BY taken_at DESC LIMIT 1"
        return self.db.execute(query, params).fetchone()

    def wallet_trace(self, owner: str, max_age_days: int, now: datetime) -> WalletTrace | None:
        row = self.db.execute("SELECT * FROM wallet_traces WHERE owner=?", (owner,)).fetchone()
        if not row or now.astimezone(UTC) - datetime.fromisoformat(row["checked_at"]) > timedelta(days=max_age_days):
            return None
        return WalletTrace(
            owner=row["owner"], first_funder=row["first_funder"],
            first_funded_at=datetime.fromisoformat(row["first_funded_at"]) if row["first_funded_at"] else None,
            wallet_created_at=datetime.fromisoformat(row["wallet_created_at"]) if row["wallet_created_at"] else None,
            complete=bool(row["complete"]),
        )

    def save_wallet_trace(self, trace: WalletTrace, now: datetime) -> None:
        self._write_retry(lambda: self.db.execute(
            "INSERT OR REPLACE INTO wallet_traces(owner,first_funder,first_funded_at,wallet_created_at,checked_at,complete) VALUES(?,?,?,?,?,?)",
            (trace.owner, trace.first_funder, iso(trace.first_funded_at) if trace.first_funded_at else None, iso(trace.wallet_created_at) if trace.wallet_created_at else None, iso(now), int(trace.complete)),
        ))

    def acquisition_trace(self, mint: str, owner: str) -> AcquisitionTrace | None:
        row = self.db.execute("SELECT * FROM acquisition_traces WHERE mint=? AND owner=?", (mint, owner)).fetchone()
        if not row:
            return None
        return AcquisitionTrace(mint, owner, datetime.fromisoformat(row["first_acquired_at"]) if row["first_acquired_at"] else None, row["initial_amount"])

    def save_acquisition_trace(self, trace: AcquisitionTrace, now: datetime) -> None:
        self._write_retry(lambda: self.db.execute(
            "INSERT OR REPLACE INTO acquisition_traces(mint,owner,first_acquired_at,initial_amount,checked_at) VALUES(?,?,?,?,?)",
            (trace.mint, trace.owner, iso(trace.first_acquired_at) if trace.first_acquired_at else None, trace.initial_amount, iso(now)),
        ))

    def archive_response(
        self,
        *,
        method: str,
        endpoint: str,
        request_params: dict[str, Any] | None,
        request_body: dict[str, Any] | None,
        status: int,
        response_body: str,
        captured_at: datetime | None = None,
    ) -> None:
        captured_at = captured_at or datetime.now(UTC)
        safe_params = {
            key: value for key, value in (request_params or {}).items()
            if key.lower() not in {"api-key", "apikey", "key", "token"}
        }
        values = (
            iso(captured_at), captured_at.date().isoformat(), method.upper(), endpoint,
            json.dumps(safe_params, sort_keys=True),
            json.dumps(request_body, sort_keys=True) if request_body is not None else None,
            status, _pack(response_body) if self.compress_archive else response_body,
        )
        self._write_retry(lambda: self.db.execute(
            "INSERT INTO raw_responses(captured_at,run_date,method,endpoint,request_params,request_body,status,response_body) VALUES(?,?,?,?,?,?,?,?)",
            values,
        ))

    def prune_archive(self, now: datetime, retention_days: int, *, vacuum: bool = False) -> int:
        """Drop archived HTTP bodies older than the replay window.

        The archive exists so a past date can be re-scored offline. Keeping it
        forever is what grows the database into the gigabyte range, so anything
        older than the configured window is removed on every committed run.
        """
        if retention_days <= 0:
            return 0
        cutoff = (now.astimezone(UTC) - timedelta(days=retention_days)).date().isoformat()
        cursor = self.db.execute("DELETE FROM raw_responses WHERE run_date < ?", (cutoff,))
        removed = cursor.rowcount or 0
        self.db.commit()
        if vacuum:
            # The write-ahead log must be folded into the main file first, or
            # VACUUM rewrites a database that does not yet contain the changes
            # and the file on disk never shrinks.
            self.checkpoint()
            # VACUUM cannot run inside a transaction and needs free disk space
            # roughly equal to the current file size.
            self.db.execute("VACUUM")
            self.db.commit()
            self.checkpoint()
        return removed

    def checkpoint(self) -> bool:
        """Fold the write-ahead log into the database file.

        Returns False when another connection (the collector or the local
        interface) is attached, which is the usual reason the file on disk does
        not shrink after a vacuum.
        """
        row = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return not (row and row[0])

    def compact_archive(self, batch: int = 200) -> int:
        """Compress archive rows written before compression existed.

        Lossless: the same bodies come back through `_unpack`, so replay for
        every already-archived date keeps working.
        """
        converted = 0
        while True:
            rows = self.db.execute(
                "SELECT id,response_body FROM raw_responses WHERE typeof(response_body)='text' LIMIT ?",
                (batch,),
            ).fetchall()
            if not rows:
                return converted
            self.db.executemany(
                "UPDATE raw_responses SET response_body=? WHERE id=?",
                [(_pack(row["response_body"]), row["id"]) for row in rows],
            )
            self.db.commit()
            converted += len(rows)

    def database_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            companion = self.path.with_name(self.path.name + suffix)
            if companion.exists():
                total += companion.stat().st_size
        return total

    def replay_response(
        self,
        run_date: str,
        method: str,
        endpoint: str,
        request_params: dict[str, Any] | None = None,
        request_body: dict[str, Any] | None = None,
    ) -> Any | None:
        rows = self.db.execute(
            "SELECT request_params,request_body,response_body FROM raw_responses WHERE run_date=? AND method=? AND endpoint=? AND status BETWEEN 200 AND 299 ORDER BY captured_at DESC,id DESC",
            (run_date, method.upper(), endpoint),
        ).fetchall()
        safe_params = {
            key: value for key, value in (request_params or {}).items()
            if key.lower() not in {"api-key", "apikey", "key", "token"}
        }
        wanted_params = json.dumps(safe_params, sort_keys=True)
        wanted = json.dumps(request_body, sort_keys=True) if request_body is not None else None
        for row in rows:
            if row["request_params"] == wanted_params and (request_body is None or row["request_body"] == wanted):
                body = _unpack(row["response_body"])
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return body
        return None

    @staticmethod
    def _cluster_id(funder: str) -> str:
        return hashlib.sha256(funder.encode()).hexdigest()[:24]

    def register_cluster(self, funder: str, wallets: list[str], mint: str, now: datetime) -> str:
        cluster_id = self._cluster_id(funder)
        row = self.db.execute("SELECT * FROM clusters WHERE cluster_id=?", (cluster_id,)).fetchone()
        combined_wallets = sorted(set(wallets) | (set(json.loads(row["wallets"])) if row else set()))
        tokens = sorted({mint} | (set(json.loads(row["tokens_seen"])) if row else set()))
        outcomes = json.loads(row["outcomes"]) if row else {}
        self.db.execute(
            "INSERT OR REPLACE INTO clusters(cluster_id,funder,wallets,first_seen,tokens_seen,outcomes) VALUES(?,?,?,?,?,?)",
            (cluster_id, funder, json.dumps(combined_wallets), row["first_seen"] if row else iso(now), json.dumps(tokens), json.dumps(outcomes, sort_keys=True)),
        )
        self.db.commit()
        return cluster_id

    def sync_cluster_outcomes(self) -> None:
        rows = self.db.execute("SELECT * FROM clusters").fetchall()
        for row in rows:
            outcomes = json.loads(row["outcomes"])
            for mint in json.loads(row["tokens_seen"]):
                result = self.db.execute(
                    """SELECT f.return_pct FROM forward_returns f JOIN observations o ON o.id=f.observation_id
                       WHERE o.mint=? AND o.kind='featured' AND f.horizon_hours=168 ORDER BY o.observed_at DESC LIMIT 1""",
                    (mint,),
                ).fetchone()
                if result:
                    outcomes[mint] = result[0]
            self.db.execute("UPDATE clusters SET outcomes=? WHERE cluster_id=?", (json.dumps(outcomes, sort_keys=True), row["cluster_id"]))
        self.db.commit()

    def cluster_prior_history(self, funder: str, wallets: list[str], current_mint: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        wallet_set = set(wallets)
        for row in self.db.execute("SELECT * FROM clusters"):
            registered = set(json.loads(row["wallets"]))
            tokens = [mint for mint in json.loads(row["tokens_seen"]) if mint != current_mint]
            if tokens and (row["funder"] == funder or len(wallet_set & registered) >= 2):
                matches.append({
                    "cluster_id": row["cluster_id"], "funder": row["funder"],
                    "wallet_overlap": len(wallet_set & registered), "tokens": tokens,
                    "outcomes": json.loads(row["outcomes"]),
                })
        return matches

    def latest_pool_snapshot(self, mint: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM pool_snapshots WHERE mint=? ORDER BY taken_at DESC LIMIT 1", (mint,)).fetchone()

    def pool_at_or_before(self, mint: str, when: datetime) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM pool_snapshots WHERE mint=? AND taken_at<=? ORDER BY taken_at DESC LIMIT 1",
            (mint, iso(when)),
        ).fetchone()

    def record_pool_snapshot(self, mint: str, now: datetime, vault_balances: dict[str, float], liquidity_proxy: float | None, dex_liquidity_usd: float | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO pool_snapshots(mint,taken_at,vault_balances,liquidity_proxy,dex_liquidity_usd) VALUES(?,?,?,?,?)",
            (mint, iso(now), json.dumps(vault_balances, sort_keys=True), liquidity_proxy, dex_liquidity_usd),
        )
        self.db.commit()

    def creator_history(self, mint: str, limit: int = 4) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM creator_snapshots WHERE mint=? ORDER BY taken_at DESC LIMIT ?", (mint, limit)
        ).fetchall()

    def record_creator_snapshot(self, mint: str, now: datetime, creator: str | None, linked_wallets: list[str], amount: float, supply_pct: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO creator_snapshots(mint,taken_at,creator,linked_wallets,amount,supply_pct) VALUES(?,?,?,?,?,?)",
            (mint, iso(now), creator, json.dumps(sorted(linked_wallets)), amount, supply_pct),
        )
        self.db.commit()

    def record_pair_observation(self, mint: str, pair_address: str, dex_id: str, pair_created_at: datetime | None, now: datetime) -> sqlite3.Row | None:
        prior = self.db.execute(
            "SELECT * FROM pair_history WHERE mint=? AND pair_address<>? ORDER BY first_seen DESC LIMIT 1",
            (mint, pair_address),
        ).fetchone()
        stamp = iso(now)
        self.db.execute(
            "INSERT INTO pair_history(mint,pair_address,dex_id,pair_created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(mint,pair_address) DO UPDATE SET last_seen=excluded.last_seen,dex_id=excluded.dex_id",
            (mint, pair_address, dex_id, iso(pair_created_at) if pair_created_at else None, stamp, stamp),
        )
        self.db.commit()
        return prior

    def prior_pair_observation(self, mint: str, pair_address: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM pair_history WHERE mint=? AND pair_address<>? ORDER BY first_seen DESC LIMIT 1",
            (mint, pair_address),
        ).fetchone()

    def cluster_wallets_for_token(self, mint: str) -> set[str]:
        wallets: set[str] = set()
        for row in self.db.execute("SELECT wallets,tokens_seen FROM clusters"):
            if mint in json.loads(row["tokens_seen"]):
                wallets.update(json.loads(row["wallets"]))
        return wallets

    def latest_watcher_sample(self, mint: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM watcher_samples WHERE mint=? ORDER BY taken_at DESC LIMIT 1", (mint,)
        ).fetchone()

    def record_watcher_sample(
        self,
        mint: str,
        now: datetime,
        holder_count: int | None,
        balances: dict[str, float],
        creator_amount: float | None,
        cluster_amount: float | None,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO watcher_samples(mint,taken_at,holder_count,balances,creator_amount,cluster_amount) VALUES(?,?,?,?,?,?)",
            (mint, iso(now), holder_count, json.dumps(balances, sort_keys=True), creator_amount, cluster_amount),
        )
        self.db.commit()

    def metric_zscores(self, mint: str, metrics: dict[str, float], now: datetime, trailing_days: int, min_samples: int, *, record: bool = True) -> dict[str, float]:
        since = iso(now - timedelta(days=trailing_days))
        result: dict[str, float] = {}
        for name, current in metrics.items():
            values = [row[0] for row in self.db.execute(
                "SELECT value FROM token_metrics WHERE mint=? AND metric=? AND taken_at>=? ORDER BY taken_at",
                (mint, name, since),
            )]
            if len(values) >= min_samples:
                mean = sum(values) / len(values)
                variance = sum((value - mean) ** 2 for value in values) / len(values)
                if variance > 0:
                    result[name] = (current - mean) / variance ** 0.5
            if record:
                self.db.execute(
                    "INSERT OR REPLACE INTO token_metrics(mint,taken_at,metric,value) VALUES(?,?,?,?)",
                    (mint, iso(now), name, current),
                )
        if record:
            self.db.commit()
        return result

    def record_early_wallet(self, mint: str, owner: str, acquired_at: datetime, now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO early_wallets(mint,owner,acquired_at,recorded_at) VALUES(?,?,?,?)",
            (mint, owner, iso(acquired_at), iso(now)),
        )
        self.db.commit()

    def smart_money_matches(self, current_mint: str, owners: list[str], winner_return_pct: float, min_wins: int) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for owner in owners:
            wins = self.db.execute(
                """SELECT DISTINCT e.mint,f.return_pct FROM early_wallets e
                   JOIN observations o ON o.mint=e.mint AND o.kind='featured'
                   JOIN forward_returns f ON f.observation_id=o.id AND f.horizon_hours=168
                   WHERE e.owner=? AND e.mint<>? AND f.return_pct>=?""",
                (owner, current_mint, winner_return_pct),
            ).fetchall()
            if len(wins) >= min_wins:
                matches.append({"owner": owner, "wins": [(row["mint"], row["return_pct"]) for row in wins]})
        return matches

    def mark_trade(self, mint: str, decision: str, now: datetime) -> None:
        if decision not in {"traded", "skipped"}:
            raise ValueError("decision must be traded or skipped")
        self.db.execute(
            "INSERT OR REPLACE INTO trade_feedback(mint,decision,marked_at) VALUES(?,?,?)",
            (mint, decision, iso(now)),
        )
        self.db.commit()

    def record_overlap(self, mint_a: str, mint_b: str, now: datetime, overlap_pct: float) -> None:
        first, second = sorted((mint_a, mint_b))
        self.db.execute(
            "INSERT OR REPLACE INTO overlap_snapshots(mint_a,mint_b,taken_at,overlap_pct) VALUES(?,?,?,?)",
            (first, second, iso(now), overlap_pct),
        )
        self.db.commit()

    def previous_overlap(self, mint_a: str, mint_b: str, before: datetime) -> float | None:
        first, second = sorted((mint_a, mint_b))
        row = self.db.execute(
            "SELECT overlap_pct FROM overlap_snapshots WHERE mint_a=? AND mint_b=? AND taken_at<=? ORDER BY taken_at DESC LIMIT 1",
            (first, second, iso(before)),
        ).fetchone()
        return row[0] if row else None

    def record_market_context(self, now: datetime, sol_price_usd: float | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO market_context(taken_at,sol_price_usd) VALUES(?,?)", (iso(now), sol_price_usd))
        self.db.commit()

    def sol_correlation(self, mint: str, now: datetime, days: int = 30) -> float | None:
        rows = self.db.execute(
            """SELECT c.taken_at,c.price_usd,m.sol_price_usd
               FROM snapshot_context c
               JOIN market_context m ON substr(m.taken_at,1,10)=substr(c.taken_at,1,10)
               WHERE c.mint=? AND c.taken_at>=? AND c.price_usd>0 AND m.sol_price_usd>0
               ORDER BY c.taken_at""",
            (mint, iso(now - timedelta(days=days))),
        ).fetchall()
        by_day: dict[str, tuple[float, float]] = {}
        for row in rows:
            by_day[row["taken_at"][:10]] = (row["price_usd"], row["sol_price_usd"])
        ordered = [by_day[day] for day in sorted(by_day)]
        if len(ordered) < 7:
            return None
        token_returns = [math.log(ordered[index][0] / ordered[index - 1][0]) for index in range(1, len(ordered))]
        sol_returns = [math.log(ordered[index][1] / ordered[index - 1][1]) for index in range(1, len(ordered))]
        token_mean = sum(token_returns) / len(token_returns)
        sol_mean = sum(sol_returns) / len(sol_returns)
        covariance = sum((a - token_mean) * (b - sol_mean) for a, b in zip(token_returns, sol_returns))
        token_variance = sum((value - token_mean) ** 2 for value in token_returns)
        sol_variance = sum((value - sol_mean) ** 2 for value in sol_returns)
        denominator = (token_variance * sol_variance) ** .5
        return covariance / denominator if denominator else None

    def record_quality(self, run_date: str, field: str, null_count: int, total_count: int, mean_value: float | None) -> list[str]:
        previous = self.db.execute(
            "SELECT * FROM data_quality WHERE field=? AND run_date<? ORDER BY run_date DESC LIMIT 1", (field, run_date)
        ).fetchone()
        self.db.execute(
            "INSERT OR REPLACE INTO data_quality(run_date,field,null_count,total_count,mean_value) VALUES(?,?,?,?,?)",
            (run_date, field, null_count, total_count, mean_value),
        )
        self.db.commit()
        alerts: list[str] = []
        if previous and total_count:
            current_null = null_count / total_count
            prior_null = previous["null_count"] / previous["total_count"] if previous["total_count"] else 0
            if current_null - prior_null >= 0.2:
                alerts.append(f"{field} null rate jumped from {prior_null:.0%} to {current_null:.0%}")
            if mean_value is not None and previous["mean_value"] not in (None, 0):
                shift = mean_value / previous["mean_value"] - 1
                if abs(shift) >= 1:
                    alerts.append(f"{field} distribution mean shifted {shift:+.0%}")
        return alerts

    def record_event_once(self, event_key: str, mint: str, event_type: str, now: datetime, detail: str) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO intelligence_events(event_key,mint,event_type,occurred_at,detail) VALUES(?,?,?,?,?)",
            (event_key, mint, event_type, iso(now), detail),
        )
        self.db.commit()
        return bool(cursor.rowcount)

    def novelty(self, mint: str, market_cap: float, now: datetime, novelty_days: int, follow_up_multiple: float, retire_after: int) -> tuple[bool, str, float | None]:
        row = self.feature_state(mint)
        if not row:
            return True, "NEW", None
        last = datetime.fromisoformat(row["last_featured"])
        if last.astimezone(now.tzinfo).date() == now.date():
            return True, "TODAY", None
        if row["retired"] or row["times_featured"] >= retire_after:
            return False, "retired", None
        multiple = market_cap / row["mcap_at_last_feature"] if row["mcap_at_last_feature"] else 0
        if now.astimezone(UTC) - last < timedelta(days=novelty_days):
            if multiple >= follow_up_multiple:
                return True, "FOLLOW-UP", multiple
            return False, "seen recently", multiple
        return True, "FOLLOW-UP", multiple

    def recent_symbol_reuse(self, symbol: str, mint: str, now: datetime, days: int = 30) -> int:
        """Count distinct other mints observed recently under the same ticker."""
        since = iso(now - timedelta(days=days))
        row = self.db.execute(
            """
            SELECT COUNT(DISTINCT mint)
            FROM observations
            WHERE lower(symbol)=lower(?) AND mint<>? AND observed_at>=?
            """,
            (symbol, mint, since),
        ).fetchone()
        return int(row[0] or 0)

    def record_feature(self, mint: str, symbol: str, market_cap: float, rank: int, now: datetime) -> None:
        stamp = iso(now)
        existing = self.feature_state(mint)
        if existing:
            last = datetime.fromisoformat(existing["last_featured"])
            if last.astimezone(now.tzinfo).date() == now.date():
                self.db.execute(
                    """
                    UPDATE featured
                    SET symbol=?, last_featured=?, best_rank=MIN(best_rank, ?), mcap_at_last_feature=?
                    WHERE mint=?
                    """,
                    (symbol, stamp, rank, market_cap, mint),
                )
                self.db.commit()
                return
        self.db.execute(
            """
            INSERT INTO featured(mint, symbol, first_seen, last_featured, times_featured, best_rank, mcap_at_last_feature)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
              symbol=excluded.symbol, last_featured=excluded.last_featured,
              times_featured=featured.times_featured + 1,
              best_rank=MIN(featured.best_rank, excluded.best_rank),
              mcap_at_last_feature=excluded.mcap_at_last_feature
            """,
            (mint, symbol, stamp, stamp, rank, market_cap),
        )
        self._observation(mint, symbol, "featured", market_cap, rank, now)
        self.db.commit()

    def record_exclusion(self, mint: str, symbol: str, market_cap: float, now: datetime) -> None:
        self._observation(mint, symbol, "excluded", market_cap, None, now)
        self.db.commit()

    def _observation(self, mint: str, symbol: str, kind: str, market_cap: float, rank: int | None, now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO observations(mint, symbol, kind, observed_at, market_cap, rank) VALUES (?, ?, ?, ?, ?, ?)",
            (mint, symbol, kind, iso(now), market_cap, rank),
        )

    def due_observations(self, now: datetime) -> list[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT o.* FROM observations o
            WHERE o.observed_at >= ? AND EXISTS (
              SELECT 1 FROM (SELECT 24 h UNION ALL SELECT 72 UNION ALL SELECT 168) horizons
              WHERE datetime(o.observed_at, '+' || horizons.h || ' hours') <= datetime(?)
              AND NOT EXISTS (SELECT 1 FROM forward_returns f WHERE f.observation_id=o.id AND f.horizon_hours=horizons.h)
            )
            """,
            (iso(now - timedelta(days=40)), iso(now)),
        ).fetchall()
        return rows

    def record_forward_returns(self, observations: Iterable[sqlite3.Row], current_mcaps: dict[str, float], now: datetime) -> None:
        for row in observations:
            current = current_mcaps.get(row["mint"])
            if current is None or row["market_cap"] <= 0:
                continue
            age_hours = (now.astimezone(UTC) - datetime.fromisoformat(row["observed_at"])).total_seconds() / 3600
            for horizon in (24, 72, 168):
                if age_hours < horizon:
                    continue
                self.db.execute(
                    "INSERT OR IGNORE INTO forward_returns(observation_id, horizon_hours, measured_at, market_cap, return_pct) VALUES (?, ?, ?, ?, ?)",
                    (row["id"], horizon, iso(now), current, ((current / row["market_cap"]) - 1) * 100),
                )
        self.db.commit()

    def scorecard(self, now: datetime, days: int = 30) -> Scorecard:
        since = iso(now - timedelta(days=days))

        def metrics(kind: str) -> tuple[int, float | None, float | None]:
            count = self.db.execute(
                "SELECT COUNT(DISTINCT mint) FROM observations WHERE kind=? AND observed_at>=?", (kind, since)
            ).fetchone()[0]
            values = [
                row[0] for row in self.db.execute(
                    """SELECT f.return_pct FROM forward_returns f JOIN observations o ON o.id=f.observation_id
                    WHERE o.kind=? AND o.observed_at>=? AND f.horizon_hours=72""", (kind, since)
                ).fetchall()
            ]
            return count, median(values) if values else None, (sum(v > 0 for v in values) / len(values) * 100) if values else None

        fc, fm, fu = metrics("featured")
        ec, em, eu = metrics("excluded")
        featured_values = [
            row[0] for row in self.db.execute(
                """SELECT f.return_pct FROM forward_returns f JOIN observations o ON o.id=f.observation_id
                   WHERE o.kind='featured' AND o.observed_at>=? AND f.horizon_hours=72""", (since,)
            )
        ]

        def feedback(decision: str) -> tuple[int, float | None]:
            values = [row[0] for row in self.db.execute(
                """SELECT (
                     SELECT f.return_pct FROM observations o
                     JOIN forward_returns f ON f.observation_id=o.id AND f.horizon_hours=72
                     WHERE o.mint=t.mint AND o.kind='featured'
                     ORDER BY o.observed_at DESC LIMIT 1
                   ) AS outcome
                   FROM trade_feedback t
                   WHERE t.decision=? AND t.marked_at>=?
                   AND outcome IS NOT NULL""", (decision, since)
            )]
            return len(values), median(values) if values else None

        traded_count, traded_median = feedback("traded")
        skipped_count, skipped_median = feedback("skipped")
        return Scorecard(
            featured_count=fc,
            featured_median_72h=fm,
            featured_up_pct_72h=fu,
            excluded_count=ec,
            excluded_median_72h=em,
            excluded_up_pct_72h=eu,
            featured_q1_72h=percentile(featured_values, 0.25),
            featured_q3_72h=percentile(featured_values, 0.75),
            featured_crash_pct_72h=(sum(value < -90 for value in featured_values) / len(featured_values) * 100) if featured_values else None,
            traded_count=traded_count,
            traded_median_72h=traded_median,
            skipped_count=skipped_count,
            skipped_median_72h=skipped_median,
        )

    def weekly_retrospective(self, now: datetime) -> list[str]:
        current_start = iso(now - timedelta(days=7))
        prior_start = iso(now - timedelta(days=14))
        current_values = [row[0] for row in self.db.execute(
            """SELECT f.return_pct FROM forward_returns f JOIN observations o ON o.id=f.observation_id
               WHERE o.kind='featured' AND f.horizon_hours=72 AND o.observed_at>=?""", (current_start,)
        )]
        prior_values = [row[0] for row in self.db.execute(
            """SELECT f.return_pct FROM forward_returns f JOIN observations o ON o.id=f.observation_id
               WHERE o.kind='featured' AND f.horizon_hours=72 AND o.observed_at>=? AND o.observed_at<?""", (prior_start, current_start)
        )]
        notes: list[str] = []
        if current_values:
            notes.append(
                f"7d cohort: {len(current_values)} matured picks; median {median(current_values):+.1f}%; interquartile range {percentile(current_values, .25):+.1f}% to {percentile(current_values, .75):+.1f}%"
            )
            notes.append(f"{sum(value < -90 for value in current_values) / len(current_values):.0%} lost more than 90% within 72h")
        if current_values and prior_values:
            shift = median(current_values) - median(prior_values)
            notes.append(f"median outcome shifted {shift:+.1f} percentage points versus the prior week — {'regime improved' if shift > 0 else 'regime weakened'}")
        return notes

    def unretire(self, mint: str) -> bool:
        cursor = self.db.execute("UPDATE featured SET retired=0, times_featured=0 WHERE mint=?", (mint,))
        self.db.commit()
        return bool(cursor.rowcount)

    def stats(self) -> dict[str, int]:
        return {
            "featured_tokens": self.db.execute("SELECT COUNT(*) FROM featured").fetchone()[0],
            "observations": self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "forward_returns": self.db.execute("SELECT COUNT(*) FROM forward_returns").fetchone()[0],
            "cache_entries": self.db.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0],
            "holder_snapshots": self.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
            "balance_rows": self.db.execute("SELECT COUNT(*) FROM balances").fetchone()[0],
            "watched_tokens": self.db.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0],
            "wallet_traces": self.db.execute("SELECT COUNT(*) FROM wallet_traces").fetchone()[0],
            "global_clusters": self.db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0],
            "raw_responses": self.db.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0],
            "watcher_samples": self.db.execute("SELECT COUNT(*) FROM watcher_samples").fetchone()[0],
            "trade_feedback": self.db.execute("SELECT COUNT(*) FROM trade_feedback").fetchone()[0],
            "lifecycle_tokens": self.db.execute("SELECT COUNT(*) FROM lifecycle_tokens").fetchone()[0],
            "market_snapshots": self.db.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0],
            "market_milestones": self.db.execute("SELECT COUNT(*) FROM market_milestones").fetchone()[0],
            "wallet_events": self.db.execute("SELECT COUNT(*) FROM wallet_events").fetchone()[0],
            "daily_recaps": self.db.execute("SELECT COUNT(*) FROM daily_recaps").fetchone()[0],
            "database_megabytes": self.database_bytes() // (1024 * 1024),
        }
