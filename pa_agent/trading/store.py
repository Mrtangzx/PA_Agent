"""Versioned SQLite source of truth for decisions, plans and trade outcomes."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pa_agent.trading.models import Execution, InstrumentProfile, TradePlan, TradeResult

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 19


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class TradeStore:
    """Small synchronous repository; every operation owns its SQLite connection."""

    def __init__(self, db_path: Path, *, legacy_dir: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.legacy_dir = Path(legacy_dir) if legacy_dir else self.db_path.parent
        self.available = False
        self.error = ""
        self._write_lock = threading.RLock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._migrate()
            self.available = True
            self.import_legacy_csvs()
        except Exception as exc:  # noqa: BLE001 - persistence is deliberately non-fatal
            self.error = str(exc)
            logger.exception("Trading database initialization failed; analysis remains available")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._write_lock, self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_imports (
                    file_hash TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decision_events (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    market_state TEXT NOT NULL DEFAULT '',
                    confidence REAL,
                    strategy_version TEXT NOT NULL DEFAULT '',
                    feature_version TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    app_git_commit TEXT NOT NULL DEFAULT '',
                    prompt_snapshot_json TEXT NOT NULL DEFAULT '[]',
                    analysis_record_ref TEXT NOT NULL DEFAULT '',
                    original_decision_json TEXT NOT NULL,
                    final_decision_json TEXT NOT NULL,
                    price_adjustments_json TEXT NOT NULL DEFAULT '[]',
                    audit_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS trade_plans (
                    id TEXT PRIMARY KEY,
                    decision_event_id TEXT NOT NULL REFERENCES decision_events(id),
                    analysis_record_ref TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss_price REAL NOT NULL,
                    take_profit_price REAL NOT NULL,
                    take_profit_price_2 REAL,
                    valid_until TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    shadow_status TEXT NOT NULL,
                    strategy_version TEXT NOT NULL DEFAULT '',
                    risk_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    shadow_entry_price REAL,
                    shadow_opened_at TEXT,
                    shadow_mfe REAL NOT NULL DEFAULT 0,
                    shadow_mae REAL NOT NULL DEFAULT 0,
                    shadow_holding_bars INTEGER NOT NULL DEFAULT 0,
                    shadow_active_stop REAL,
                    shadow_highest_close REAL,
                    shadow_time_exit_pending INTEGER NOT NULL DEFAULT 0,
                    actual_mfe REAL NOT NULL DEFAULT 0,
                    actual_mae REAL NOT NULL DEFAULT 0,
                    actual_holding_bars INTEGER NOT NULL DEFAULT 0,
                    actual_active_stop REAL,
                    actual_highest_close REAL,
                    actual_time_exit_pending INTEGER NOT NULL DEFAULT 0,
                    last_price REAL,
                    last_bar_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    executed_at TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    real_contract TEXT NOT NULL DEFAULT '',
                    fees REAL NOT NULL DEFAULT 0,
                    account_fingerprint TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    event_type TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    dataset TEXT NOT NULL DEFAULT 'plan',
                    price REAL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS trade_results (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    dataset TEXT NOT NULL CHECK(dataset IN ('shadow','actual')),
                    outcome TEXT NOT NULL,
                    entry_price REAL,
                    exit_price REAL,
                    quantity REAL,
                    gross_pnl REAL,
                    net_pnl REAL,
                    r_multiple REAL,
                    mfe_r REAL,
                    mae_r REAL,
                    holding_bars INTEGER,
                    ambiguous_same_bar INTEGER NOT NULL DEFAULT 0,
                    opened_at TEXT NOT NULL DEFAULT '',
                    closed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_id, dataset)
                );
                CREATE TABLE IF NOT EXISTS instrument_profiles (
                    symbol TEXT PRIMARY KEY,
                    asset_class TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_signals (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    parameter_version TEXT NOT NULL,
                    pool_version TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    plan_id TEXT REFERENCES trade_plans(id)
                );
                CREATE TABLE IF NOT EXISTS strategy_state_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    previous_state TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    validation_run_id TEXT NOT NULL DEFAULT '',
                    approval_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_snapshots (
                    id TEXT PRIMARY KEY,
                    account_fingerprint TEXT NOT NULL,
                    connection_status TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id TEXT PRIMARY KEY,
                    account_fingerprint TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    total_equity REAL NOT NULL,
                    available_cash REAL,
                    position_value REAL,
                    external_cash_flow REAL NOT NULL DEFAULT 0,
                    monthly_return_pct REAL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_cash_flows (
                    fingerprint TEXT PRIMARY KEY,
                    account_fingerprint TEXT NOT NULL,
                    broker_flow_id TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL CHECK(direction IN ('deposit','withdrawal')),
                    amount REAL NOT NULL CHECK(amount > 0),
                    occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'ths_ui',
                    raw_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_cash_flow_syncs (
                    id TEXT PRIMARY KEY,
                    account_fingerprint TEXT NOT NULL,
                    range_start TEXT NOT NULL,
                    range_end TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE(account_fingerprint,range_start,range_end,captured_at)
                );
                CREATE TABLE IF NOT EXISTS broker_order_links (
                    id TEXT PRIMARY KEY,
                    account_fingerprint TEXT NOT NULL,
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    broker_order_id TEXT NOT NULL,
                    broker_fill_ids_json TEXT NOT NULL DEFAULT '[]',
                    match_status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_fingerprint, plan_id, broker_order_id)
                );
                CREATE TABLE IF NOT EXISTS universe_snapshots (
                    version TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL DEFAULT '',
                    data_complete INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topdown_score_snapshots (
                    id TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    scoring_version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    pool_version TEXT NOT NULL,
                    bar_closed_at TEXT NOT NULL,
                    total_score REAL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_version,symbol,bar_closed_at,input_hash)
                );
                CREATE TABLE IF NOT EXISTS hotspot_snapshots (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(symbol,frozen_at,source_hash)
                );
                CREATE TABLE IF NOT EXISTS market_sentiment_snapshots (
                    id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    source_as_of TEXT NOT NULL DEFAULT '',
                    data_complete INTEGER NOT NULL DEFAULT 0,
                    source_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(captured_at,source_hash)
                );
                CREATE TABLE IF NOT EXISTS market_daily_prices (
                    as_of TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(as_of,symbol)
                );
                CREATE TABLE IF NOT EXISTS outside_pool_approvals (
                    id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    account_fingerprint TEXT NOT NULL,
                    effective_risk_pct REAL NOT NULL,
                    valid_until TEXT NOT NULL,
                    audit_reason TEXT NOT NULL,
                    approved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_broker_trades (
                    identity TEXT PRIMARY KEY,
                    broker_fill_id TEXT NOT NULL,
                    account_fingerprint TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    fees REAL NOT NULL DEFAULT 0,
                    filled_at TEXT NOT NULL,
                    fill_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    UNIQUE(account_fingerprint,broker_fill_id)
                );
                CREATE TABLE IF NOT EXISTS strategy_validation_runs (
                    id TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    status TEXT NOT NULL,
                    promotion_eligible INTEGER NOT NULL DEFAULT 0,
                    input_hash TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_version,dataset,input_hash)
                );
                CREATE TABLE IF NOT EXISTS validation_epochs (
                    epoch_id TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    pool_version TEXT NOT NULL,
                    member_hash TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    epoch_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_processed_bars (
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    timeframe TEXT NOT NULL,
                    bar_closed_at TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY(plan_id,timeframe,bar_closed_at)
                );
                CREATE TABLE IF NOT EXISTS oos_observations (
                    id TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    effective_at TEXT NOT NULL,
                    source_published_at TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    UNIQUE(strategy_version,kind,symbol,effective_at,payload_hash)
                );
                CREATE TABLE IF NOT EXISTS stock_sandbox_current (
                    symbol TEXT NOT NULL,
                    pool_version TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol,pool_version)
                );
                CREATE TABLE IF NOT EXISTS quant_notification_events (
                    event_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    bar_closed_at TEXT NOT NULL DEFAULT '',
                    plan_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_members (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'user',
                    active INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    added_at TEXT NOT NULL,
                    removed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_sources (
                    symbol TEXT NOT NULL REFERENCES watchlist_members(symbol),
                    source TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    added_at TEXT NOT NULL,
                    removed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol,source)
                );
                CREATE TABLE IF NOT EXISTS ths_watchlist_syncs (
                    id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    member_count INTEGER NOT NULL,
                    category_count INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_hash,captured_at)
                );
                CREATE TABLE IF NOT EXISTS ths_watchlist_scan_results (
                    scan_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    base_pool_version TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actionable_stage TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scan_id,symbol)
                );
                CREATE TABLE IF NOT EXISTS stock_selection_snapshots (
                    id TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_version,generated_at,input_hash)
                );
                CREATE TABLE IF NOT EXISTS workbench_preferences (
                    preference_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_symbol_time ON decision_events(symbol, created_at);
                CREATE INDEX IF NOT EXISTS idx_plans_status ON trade_plans(status, shadow_status);
                CREATE INDEX IF NOT EXISTS idx_events_plan_time ON trade_events(plan_id, event_at);
                CREATE INDEX IF NOT EXISTS idx_results_dataset ON trade_results(dataset, closed_at);
                CREATE INDEX IF NOT EXISTS idx_quant_signal_time ON quant_signals(strategy_id, signal_time);
                CREATE INDEX IF NOT EXISTS idx_broker_snapshot_time ON broker_snapshots(account_fingerprint, captured_at);
                CREATE INDEX IF NOT EXISTS idx_equity_snapshot_time ON equity_snapshots(account_fingerprint, captured_at);
                CREATE INDEX IF NOT EXISTS idx_broker_cash_flow_time ON broker_cash_flows(account_fingerprint,occurred_at);
                CREATE INDEX IF NOT EXISTS idx_broker_cash_flow_sync_time ON broker_cash_flow_syncs(account_fingerprint,captured_at);
                CREATE INDEX IF NOT EXISTS idx_topdown_symbol_time ON topdown_score_snapshots(strategy_version,symbol,bar_closed_at);
                CREATE INDEX IF NOT EXISTS idx_hotspot_symbol_time ON hotspot_snapshots(symbol,frozen_at);
                CREATE INDEX IF NOT EXISTS idx_sentiment_time ON market_sentiment_snapshots(captured_at);
                CREATE INDEX IF NOT EXISTS idx_market_daily_price_symbol ON market_daily_prices(symbol,as_of);
                CREATE INDEX IF NOT EXISTS idx_external_broker_time ON external_broker_trades(account_fingerprint,filled_at);
                CREATE INDEX IF NOT EXISTS idx_validation_strategy_time ON strategy_validation_runs(strategy_version,created_at);
                CREATE INDEX IF NOT EXISTS idx_lifecycle_bar_time ON lifecycle_processed_bars(timeframe,bar_closed_at);
                CREATE INDEX IF NOT EXISTS idx_oos_observation_time ON oos_observations(strategy_version,kind,effective_at);
                CREATE INDEX IF NOT EXISTS idx_validation_epoch_current ON validation_epochs(strategy_version,is_current,activated_at);
                CREATE INDEX IF NOT EXISTS idx_stock_sandbox_state ON stock_sandbox_current(pool_version,state,updated_at);
                CREATE INDEX IF NOT EXISTS idx_quant_notification_symbol ON quant_notification_events(symbol,event_type,created_at);
                CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist_members(active,updated_at);
                CREATE INDEX IF NOT EXISTS idx_watchlist_source_active
                    ON watchlist_sources(source,active,updated_at);
                CREATE INDEX IF NOT EXISTS idx_ths_watchlist_sync_time
                    ON ths_watchlist_syncs(captured_at);
                CREATE INDEX IF NOT EXISTS idx_ths_watchlist_scan_status
                    ON ths_watchlist_scan_results(scan_id,status,actionable_stage);
                CREATE INDEX IF NOT EXISTS idx_stock_selection_generated
                    ON stock_selection_snapshots(strategy_version,generated_at);
                """
            )
            plan_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(trade_plans)").fetchall()
            }
            if "last_price" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN last_price REAL")
            conn.execute(
                """INSERT OR IGNORE INTO watchlist_sources(
                       symbol,source,active,metadata_json,added_at,removed_at,updated_at
                   )
                   SELECT symbol,source,active,metadata_json,added_at,removed_at,updated_at
                   FROM watchlist_members"""
            )
            if "last_bar_at" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN last_bar_at TEXT")
            if "actual_mfe" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN actual_mfe REAL NOT NULL DEFAULT 0")
            if "actual_mae" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN actual_mae REAL NOT NULL DEFAULT 0")
            if "actual_holding_bars" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN actual_holding_bars INTEGER NOT NULL DEFAULT 0")
            if "shadow_active_stop" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN shadow_active_stop REAL")
            if "shadow_highest_close" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN shadow_highest_close REAL")
            if "shadow_time_exit_pending" not in plan_columns:
                conn.execute(
                    "ALTER TABLE trade_plans ADD COLUMN "
                    "shadow_time_exit_pending INTEGER NOT NULL DEFAULT 0"
                )
            if "actual_active_stop" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN actual_active_stop REAL")
            if "actual_highest_close" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN actual_highest_close REAL")
            if "actual_time_exit_pending" not in plan_columns:
                conn.execute(
                    "ALTER TABLE trade_plans ADD COLUMN "
                    "actual_time_exit_pending INTEGER NOT NULL DEFAULT 0"
                )
            execution_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(executions)").fetchall()
            }
            if "account_fingerprint" not in execution_columns:
                conn.execute(
                    "ALTER TABLE executions ADD COLUMN "
                    "account_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            state_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(strategy_state_events)").fetchall()
            }
            if "validation_run_id" not in state_columns:
                conn.execute(
                    "ALTER TABLE strategy_state_events "
                    "ADD COLUMN validation_run_id TEXT NOT NULL DEFAULT ''"
                )
            if "approval_json" not in state_columns:
                conn.execute(
                    "ALTER TABLE strategy_state_events "
                    "ADD COLUMN approval_json TEXT NOT NULL DEFAULT '{}'"
                )
            external_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(external_broker_trades)"
                ).fetchall()
            }
            if external_columns and "identity" not in external_columns:
                # v13 keyed only by broker_fill_id. Migrate without discarding
                # existing account data; v14 isolates identical broker IDs
                # emitted by different accounts/brokers.
                conn.executescript(
                    """
                    ALTER TABLE external_broker_trades
                    RENAME TO external_broker_trades_v13;
                    CREATE TABLE external_broker_trades (
                        identity TEXT PRIMARY KEY,
                        broker_fill_id TEXT NOT NULL,
                        account_fingerprint TEXT NOT NULL,
                        broker_order_id TEXT NOT NULL DEFAULT '',
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        price REAL NOT NULL,
                        quantity INTEGER NOT NULL,
                        fees REAL NOT NULL DEFAULT 0,
                        filled_at TEXT NOT NULL,
                        fill_json TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        UNIQUE(account_fingerprint,broker_fill_id)
                    );
                    INSERT INTO external_broker_trades(
                        identity,broker_fill_id,account_fingerprint,
                        broker_order_id,symbol,direction,price,quantity,fees,
                        filled_at,fill_json,first_seen_at
                    )
                    SELECT account_fingerprint || '|' || broker_fill_id,
                        broker_fill_id,account_fingerprint,broker_order_id,
                        symbol,direction,price,quantity,fees,filled_at,
                        fill_json,first_seen_at
                    FROM external_broker_trades_v13;
                    DROP TABLE external_broker_trades_v13;
                    CREATE INDEX IF NOT EXISTS idx_external_broker_time
                    ON external_broker_trades(account_fingerprint,filled_at);
                    """
                )
            broker_link_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(broker_order_links)").fetchall()
            }
            if broker_link_columns and "account_fingerprint" not in broker_link_columns:
                # Older links stored the account only inside details_json. Promote it
                # to a first-class key so identical broker order IDs from different
                # accounts can never update each other's lifecycle.
                conn.executescript(
                    """
                    ALTER TABLE broker_order_links RENAME TO broker_order_links_v14;
                    CREATE TABLE broker_order_links (
                        id TEXT PRIMARY KEY,
                        account_fingerprint TEXT NOT NULL,
                        plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                        broker_order_id TEXT NOT NULL,
                        broker_fill_ids_json TEXT NOT NULL DEFAULT '[]',
                        match_status TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(account_fingerprint,plan_id,broker_order_id)
                    );
                    INSERT INTO broker_order_links(
                        id,account_fingerprint,plan_id,broker_order_id,
                        broker_fill_ids_json,match_status,details_json,created_at,updated_at
                    )
                    SELECT id,
                        COALESCE(json_extract(details_json,'$.account_fingerprint'),''),
                        plan_id,broker_order_id,broker_fill_ids_json,match_status,
                        details_json,created_at,updated_at
                    FROM broker_order_links_v14;
                    DROP TABLE broker_order_links_v14;
                    """
                )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    def health(self) -> dict[str, Any]:
        return {"available": self.available, "error": self.error, "path": str(self.db_path)}

    def upsert_watchlist_member(
        self,
        *,
        symbol: str,
        name: str,
        source: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add or reactivate one personal A-share monitor entry.

        The personal watchlist is deliberately separate from versioned strategy
        universes.  Re-adding a symbol is idempotent and never rewrites strategy
        history, signals, plans, or fills.
        """
        self._require_available()
        code = str(symbol or "").strip()[-6:]
        if len(code) != 6 or not code.isdigit():
            raise ValueError("watchlist symbol must be a 6-digit A-share code")
        now = _now()
        payload = dict(metadata or {})
        with self._write_lock, self._connection() as conn:
            existing = conn.execute(
                "SELECT * FROM watchlist_members WHERE symbol=?", (code,)
            ).fetchone()
            added_at = str(existing["added_at"]) if existing else now
            existing_metadata = (
                json.loads(existing["metadata_json"] or "{}") if existing else {}
            )
            merged_metadata = {**existing_metadata, **payload}
            existing_source = str(existing["source"] or "") if existing else ""
            canonical_source = existing_source or str(source or "user")
            incoming_name = str(name or code).strip() or code
            existing_name = str(existing["name"] or "").strip() if existing else ""
            # External watchlist files contain codes but usually no security
            # names.  A background re-sync must not downgrade a previously
            # verified display name back to the six-digit code.
            resolved_name = (
                existing_name
                if incoming_name == code and existing_name and existing_name != code
                else incoming_name
            )
            conn.execute(
                """INSERT INTO watchlist_members(
                       symbol,name,source,active,metadata_json,added_at,removed_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       name=excluded.name,source=excluded.source,active=1,
                       metadata_json=excluded.metadata_json,removed_at='',
                       updated_at=excluded.updated_at""",
                (code, resolved_name, canonical_source, 1, _json(merged_metadata),
                 added_at, "", now),
            )
            source_row = conn.execute(
                "SELECT added_at FROM watchlist_sources WHERE symbol=? AND source=?",
                (code, str(source or "user")),
            ).fetchone()
            source_added_at = str(source_row["added_at"]) if source_row else now
            conn.execute(
                """INSERT INTO watchlist_sources(
                       symbol,source,active,metadata_json,added_at,removed_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,source) DO UPDATE SET
                       active=1,metadata_json=excluded.metadata_json,removed_at='',
                       updated_at=excluded.updated_at""",
                (
                    code,
                    str(source or "user"),
                    1,
                    _json(payload),
                    source_added_at,
                    "",
                    now,
                ),
            )
        return self.get_watchlist_member(code) or {}

    def remove_watchlist_member(
        self,
        symbol: str,
        *,
        deferred_reason: str = "",
        source: str = "",
    ) -> bool:
        """Deactivate a personal monitor entry without deleting its audit trail."""
        self._require_available()
        code = str(symbol or "").strip()[-6:]
        now = _now()
        with self._write_lock, self._connection() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM watchlist_members WHERE symbol=? AND active=1",
                (code,),
            ).fetchone()
            if row is None:
                return False
            metadata = json.loads(row["metadata_json"] or "{}")
            if deferred_reason:
                metadata["deferred_removal_reason"] = deferred_reason
            if source:
                cursor = conn.execute(
                    """UPDATE watchlist_sources
                       SET active=0,removed_at=?,updated_at=?
                       WHERE symbol=? AND source=? AND active=1""",
                    (now, now, code, source),
                )
            else:
                cursor = conn.execute(
                    """UPDATE watchlist_sources
                       SET active=0,removed_at=?,updated_at=?
                       WHERE symbol=? AND active=1""",
                    (now, now, code),
                )
            active_sources = conn.execute(
                "SELECT COUNT(*) FROM watchlist_sources WHERE symbol=? AND active=1",
                (code,),
            ).fetchone()[0]
            conn.execute(
                """UPDATE watchlist_members
                   SET active=?,metadata_json=?,removed_at=?,updated_at=?
                   WHERE symbol=?""",
                (
                    int(active_sources > 0),
                    _json(metadata),
                    "" if active_sources else now,
                    now,
                    code,
                ),
            )
        return cursor.rowcount > 0

    def get_watchlist_member(self, symbol: str) -> dict[str, Any] | None:
        self._require_available()
        code = str(symbol or "").strip()[-6:]
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM watchlist_members WHERE symbol=?", (code,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["active"] = bool(result["active"])
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def list_watchlist_members(
        self, *, active_only: bool = True, source: str = ""
    ) -> list[dict[str, Any]]:
        self._require_available()
        clauses: list[str] = []
        values: list[Any] = []
        if active_only:
            clauses.append("w.active=1")
        if source:
            clauses.append(
                "EXISTS(SELECT 1 FROM watchlist_sources s "
                "WHERE s.symbol=w.symbol AND s.source=? AND s.active=1)"
            )
            values.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT w.* FROM watchlist_members w {where}
                    ORDER BY w.active DESC,w.updated_at DESC,w.symbol ASC""",
                tuple(values),
            ).fetchall()
        result: list[dict[str, Any]] = []
        with self._connection() as conn:
            for row in rows:
                item = dict(row)
                item["active"] = bool(item["active"])
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                source_rows = conn.execute(
                    """SELECT source,active,metadata_json,added_at,removed_at,updated_at
                       FROM watchlist_sources WHERE symbol=? ORDER BY source""",
                    (item["symbol"],),
                ).fetchall()
                item["sources"] = [
                    {
                        **dict(source_row),
                        "active": bool(source_row["active"]),
                        "metadata": json.loads(source_row["metadata_json"] or "{}"),
                    }
                    for source_row in source_rows
                ]
                result.append(item)
        return result

    def sync_watchlist_source(
        self,
        source: str,
        members: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Mirror one external watchlist source without removing other origins."""
        self._require_available()
        source_name = str(source or "").strip()
        if not source_name:
            raise ValueError("watchlist source is required")
        wanted: set[str] = set()
        for member in members:
            code = str(member.get("symbol") or "").strip()[-6:]
            if len(code) != 6 or not code.isdigit():
                continue
            wanted.add(code)
            self.upsert_watchlist_member(
                symbol=code,
                name=str(member.get("name") or code),
                source=source_name,
                metadata=dict(member.get("metadata") or {}),
            )
        existing = self.list_watchlist_members(active_only=True, source=source_name)
        removed = 0
        for item in existing:
            if item["symbol"] not in wanted and self.remove_watchlist_member(
                item["symbol"], source=source_name
            ):
                removed += 1
        return {"active": len(wanted), "removed": removed}

    def add_ths_watchlist_sync(self, snapshot: Any) -> str:
        self._require_available()
        payload = (
            snapshot.model_dump(mode="json")
            if hasattr(snapshot, "model_dump") else dict(snapshot)
        )
        sync_id = str(payload.get("id") or uuid.uuid4().hex)
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO ths_watchlist_syncs(
                       id,captured_at,source_updated_at,source_hash,source_fingerprint,
                       status,member_count,category_count,snapshot_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    sync_id,
                    str(payload.get("captured_at") or _now()),
                    str(payload.get("source_updated_at") or ""),
                    str(payload.get("source_hash") or ""),
                    str(payload.get("source_fingerprint") or ""),
                    str(payload.get("status") or "complete"),
                    len(payload.get("members") or []),
                    len(payload.get("categories") or []),
                    _json(payload),
                    _now(),
                ),
            )
        return sync_id

    def latest_ths_watchlist_sync(self) -> dict[str, Any] | None:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM ths_watchlist_syncs ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
        return item

    def upsert_ths_watchlist_scan_result(
        self, scan_id: str, result: dict[str, Any]
    ) -> None:
        self._require_available()
        now = _now()
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO ths_watchlist_scan_results(
                       scan_id,symbol,source_hash,base_pool_version,signal_date,
                       status,actionable_stage,result_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scan_id,symbol) DO UPDATE SET
                       status=excluded.status,actionable_stage=excluded.actionable_stage,
                       result_json=excluded.result_json,updated_at=excluded.updated_at""",
                (
                    scan_id,
                    str(result.get("symbol") or ""),
                    str(result.get("source_hash") or ""),
                    str(result.get("base_pool_version") or ""),
                    str(result.get("signal_date") or ""),
                    str(result.get("status") or ""),
                    str(result.get("actionable_stage") or ""),
                    _json(result),
                    now,
                    now,
                ),
            )

    def list_ths_watchlist_scan_results(
        self, *, scan_id: str = "", limit: int = 2000
    ) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE scan_id=?" if scan_id else ""
        params: tuple[Any, ...] = (scan_id, limit) if scan_id else (limit,)
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT * FROM ths_watchlist_scan_results {where}
                    ORDER BY CASE actionable_stage
                        WHEN 'actionable' THEN 0
                        WHEN 'next_session_candidate' THEN 1
                        WHEN 'intraday_observation' THEN 2
                        WHEN 'not_ready' THEN 3 ELSE 4 END,
                        symbol LIMIT ?""",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json") or "{}")
            result.append(item)
        return result

    def latest_ths_watchlist_scan_results(self) -> list[dict[str, Any]]:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute(
                """SELECT scan_id FROM ths_watchlist_scan_results
                   ORDER BY updated_at DESC LIMIT 1"""
            ).fetchone()
        return self.list_ths_watchlist_scan_results(scan_id=str(row[0])) if row else []

    def save_workbench_preference(self, key: str, value: Any) -> None:
        self._require_available()
        if not str(key or "").strip():
            raise ValueError("workbench preference key is required")
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO workbench_preferences(preference_key,value_json,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(preference_key) DO UPDATE SET
                       value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (str(key), _json(value), _now()),
            )

    def get_workbench_preference(self, key: str, default: Any = None) -> Any:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM workbench_preferences WHERE preference_key=?",
                (str(key),),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    def add_oos_observation(
        self,
        *,
        strategy_version: str,
        kind: str,
        effective_at: str,
        source_published_at: str,
        source_kind: str,
        source_url: str,
        payload: dict[str, Any],
        symbol: str = "",
        captured_at: str = "",
    ) -> str:
        """Append one immutable, source-timed production OOS observation."""
        self._require_available()
        if not all((strategy_version, kind, effective_at, source_published_at, source_kind)):
            raise ValueError("OOS observation requires strategy, kind and source times")
        effective = datetime.fromisoformat(str(effective_at).replace("Z", "+00:00"))
        published = datetime.fromisoformat(
            str(source_published_at).replace("Z", "+00:00")
        )
        if effective.tzinfo is None or published.tzinfo is None:
            raise ValueError("OOS observation timestamps must include timezone")
        if published > effective:
            raise ValueError("OOS observation source cannot be from the future")
        payload_json = _json(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        identity = "|".join((
            strategy_version, kind, symbol, effective.isoformat(), payload_hash,
        ))
        observation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with self._write_lock, self._connection() as conn:
            # The first point-in-time observation is the frozen fact.  A later
            # retry must fill missing symbols, not rewrite already observed
            # rows with data that changed after the original close.
            existing = conn.execute(
                """SELECT id FROM oos_observations
                   WHERE strategy_version=? AND kind=? AND symbol=?
                     AND effective_at=?
                   ORDER BY captured_at,id LIMIT 1""",
                (strategy_version, kind, symbol, effective.isoformat()),
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            conn.execute(
                """INSERT OR IGNORE INTO oos_observations(
                    id,strategy_version,kind,symbol,effective_at,source_published_at,
                    source_kind,source_url,payload_hash,payload_json,captured_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id, strategy_version, kind, symbol,
                    effective.isoformat(), published.isoformat(), source_kind,
                    source_url, payload_hash, payload_json, captured_at or _now(),
                ),
            )
            return observation_id

    def upsert_validation_epoch(self, epoch: Any, *, make_current: bool) -> None:
        """Persist one epoch and atomically select it as the current owner."""
        payload = (
            epoch.model_dump(mode="json")
            if hasattr(epoch, "model_dump")
            else dict(epoch)
        )
        epoch_id = str(payload["epoch_id"])
        strategy_version = str(payload["strategy_version"])
        now = _now()
        with self._write_lock, self._connection() as conn:
            if make_current:
                conn.execute(
                    "UPDATE validation_epochs SET is_current=0, updated_at=? "
                    "WHERE strategy_version=? AND is_current=1 AND epoch_id<>?",
                    (now, strategy_version, epoch_id),
                )
            conn.execute(
                """INSERT INTO validation_epochs(
                       epoch_id,strategy_version,pool_version,member_hash,
                       activated_at,status,is_current,epoch_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(epoch_id) DO UPDATE SET
                       pool_version=excluded.pool_version,
                       member_hash=excluded.member_hash,
                       activated_at=excluded.activated_at,
                       status=excluded.status,
                       is_current=excluded.is_current,
                       epoch_json=excluded.epoch_json,
                       updated_at=excluded.updated_at""",
                (
                    epoch_id,
                    strategy_version,
                    str(payload["pool_version"]),
                    str(payload["member_hash"]),
                    str(payload["activated_at"]),
                    str(payload.get("status") or "collecting"),
                    int(make_current),
                    _json(payload),
                    now,
                    now,
                ),
            )

    def current_validation_epoch(
        self, *, strategy_version: str = "cloud_ai_topdown_4321_intraday_v1"
    ) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT * FROM validation_epochs
                   WHERE strategy_version=? AND is_current=1
                   ORDER BY activated_at DESC LIMIT 1""",
                (strategy_version,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["epoch"] = json.loads(result.pop("epoch_json"))
        result["is_current"] = bool(result["is_current"])
        return result

    def list_validation_epochs(
        self,
        *,
        strategy_version: str = "cloud_ai_topdown_4321_intraday_v1",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM validation_epochs WHERE strategy_version=?
                   ORDER BY activated_at DESC LIMIT ?""",
                (strategy_version, int(limit)),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["epoch"] = json.loads(item.pop("epoch_json"))
            item["is_current"] = bool(item["is_current"])
            result.append(item)
        return result

    def list_oos_observations(
        self,
        *,
        strategy_version: str = "",
        kind: str = "",
        symbol: str = "",
        since: str = "",
        limit: int = 10_000,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        self._require_available()
        filters: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("strategy_version", strategy_version), ("kind", kind), ("symbol", symbol),
        ):
            if value:
                filters.append(f"{column}=?")
                params.append(value)
        if since:
            filters.append("effective_at>=?")
            params.append(since)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._connection() as conn:
            order = "DESC" if descending else "ASC"
            rows = conn.execute(
                f"""SELECT * FROM oos_observations {where}
                    ORDER BY effective_at {order},kind,symbol LIMIT ?""",
                (*params, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    def oos_observation_coverage(self, *, strategy_version: str) -> dict[str, Any]:
        self._require_available()
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT kind,COUNT(*) AS record_count,
                          COUNT(DISTINCT CASE WHEN symbol<>'' THEN symbol END) AS symbols,
                          MIN(effective_at) AS period_start,MAX(effective_at) AS period_end
                   FROM oos_observations WHERE strategy_version=? GROUP BY kind""",
                (strategy_version,),
            ).fetchall()
            constituent_row = conn.execute(
                """SELECT payload_json FROM oos_observations
                   WHERE strategy_version=? AND kind='historical_constituents'
                   ORDER BY effective_at DESC LIMIT 1""",
                (strategy_version,),
            ).fetchone()
        coverage = {
            str(row["kind"]): {
                "record_count": int(row["record_count"]),
                "symbols": int(row["symbols"]),
                "period_start": str(row["period_start"] or ""),
                "period_end": str(row["period_end"] or ""),
            }
            for row in rows
        }
        if constituent_row is not None and "historical_constituents" in coverage:
            payload = json.loads(str(constituent_row["payload_json"] or "{}"))
            coverage["historical_constituents"]["symbols"] = len(
                set(str(item) for item in payload.get("symbols") or [])
            )
        return coverage

    def upsert_universe_snapshot(
        self,
        snapshot: Any,
        *,
        source_updated_at: str = "",
        data_complete: bool = True,
    ) -> str:
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        from pa_agent.data.ashare_common import is_a_share_stock_symbol

        symbols = [str(item) for item in payload.get("symbols") or []]
        symbols.extend(
            str(item.get("symbol") or "")
            for item in payload.get("members") or []
            if isinstance(item, dict)
        )
        invalid = sorted({
            symbol.strip()
            for symbol in symbols
            if not is_a_share_stock_symbol(symbol)
        })
        if invalid:
            raise ValueError(
                "交易股票池仅允许A股股票，拒绝保存非A股或非股票标的: "
                + ", ".join(invalid)
            )
        version = str(payload.get("version", ""))
        if not version:
            raise ValueError("universe version is required")
        with self._write_lock, self._connection() as conn:
            existing = conn.execute(
                "SELECT snapshot_json FROM universe_snapshots WHERE version=?",
                (version,),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(existing["snapshot_json"] or "{}")
                managed_kinds = {"user_managed_a_share_universe"}
                if (
                    existing_payload != payload
                    and (
                        str(existing_payload.get("source_kind") or "") in managed_kinds
                        or str(payload.get("source_kind") or "") in managed_kinds
                    )
                ):
                    raise ValueError(
                        "版本化私人股票池快照不可原地覆盖，请生成新的股票池版本"
                    )
            conn.execute(
                """INSERT INTO universe_snapshots(
                    version,as_of,source_updated_at,data_complete,snapshot_json,created_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(version) DO UPDATE SET
                    as_of=excluded.as_of,source_updated_at=excluded.source_updated_at,
                    data_complete=excluded.data_complete,snapshot_json=excluded.snapshot_json""",
                (
                    version,
                    str(payload.get("as_of", "")),
                    source_updated_at,
                    int(data_complete),
                    _json(payload),
                    _now(),
                ),
            )
        return version

    def list_universe_snapshots(self, *, limit: int = 24) -> list[dict[str, Any]]:
        self._require_available()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM universe_snapshots ORDER BY as_of DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            result.append(item)
        return result

    def add_topdown_score(self, snapshot: Any) -> str:
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        snapshot_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO topdown_score_snapshots(
                    id,strategy_version,scoring_version,symbol,pool_version,bar_closed_at,
                    total_score,status,input_hash,snapshot_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    str(payload.get("strategy_version", "")),
                    str(payload.get("scoring_version", "")),
                    str(payload.get("symbol", "")),
                    str(payload.get("pool_version", "")),
                    str(payload.get("bar_closed_at", "")),
                    payload.get("total_score"),
                    str(payload.get("status", "")),
                    str(payload.get("input_hash", "")),
                    _json(payload),
                    _now(),
                ),
            )
            row = conn.execute(
                """SELECT id FROM topdown_score_snapshots
                   WHERE strategy_version=? AND symbol=? AND bar_closed_at=? AND input_hash=?""",
                (
                    str(payload.get("strategy_version", "")),
                    str(payload.get("symbol", "")),
                    str(payload.get("bar_closed_at", "")),
                    str(payload.get("input_hash", "")),
                ),
            ).fetchone()
        return str(row[0])

    def add_validation_run(
        self,
        report: Any,
        *,
        dataset: str,
        promotion_eligible: bool = False,
    ) -> str:
        self._require_available()
        payload = report.model_dump(mode="json") if hasattr(report, "model_dump") else dict(report)
        strategy_version = str(payload.get("strategy_version") or "")
        status = str(payload.get("status") or "")
        input_hash = str(payload.get("input_hash") or "")
        if not strategy_version or not status or not input_hash:
            raise ValueError("validation report requires strategy_version, status and input_hash")
        if dataset == "fixed_replay" and promotion_eligible:
            raise ValueError("fixed replay cannot be used for strategy promotion")
        if promotion_eligible:
            if dataset not in {"out_of_sample", "shadow"} or status != "complete":
                raise ValueError(
                    "promotion evidence must be a complete out_of_sample or shadow run"
                )
            performance = payload.get("performance_evidence")
            if not isinstance(performance, dict) or performance.get("dataset") != dataset:
                raise ValueError(
                    "promotion evidence requires matching performance_evidence"
                )
        run_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO strategy_validation_runs(
                    id,strategy_version,dataset,status,promotion_eligible,input_hash,
                    report_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    strategy_version,
                    dataset,
                    status,
                    int(promotion_eligible),
                    input_hash,
                    _json(payload),
                    _now(),
                ),
            )
            row = conn.execute(
                """SELECT id FROM strategy_validation_runs
                   WHERE strategy_version=? AND dataset=? AND input_hash=?""",
                (strategy_version, dataset, input_hash),
            ).fetchone()
        return str(row[0])

    def list_validation_runs(
        self, *, strategy_version: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE strategy_version=?" if strategy_version else ""
        params: tuple[Any, ...] = (
            (strategy_version, limit) if strategy_version else (limit,)
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT * FROM strategy_validation_runs {where}
                    ORDER BY created_at DESC LIMIT ?""",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["promotion_eligible"] = bool(item["promotion_eligible"])
            item["report"] = json.loads(item.pop("report_json") or "{}")
            result.append(item)
        return result

    def get_validation_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one exact validation run used by a strategy transition."""
        self._require_available()
        if not run_id:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM strategy_validation_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["promotion_eligible"] = bool(item["promotion_eligible"])
        item["report"] = json.loads(item.pop("report_json") or "{}")
        return item

    def latest_topdown_score(
        self,
        symbol: str = "",
        *,
        strategy_version: str = "",
        scoring_version: str = "",
        pool_version: str = "",
    ) -> dict[str, Any] | None:
        self._require_available()
        filters: list[str] = []
        params_list: list[Any] = []
        for column, value in (
            ("symbol", symbol),
            ("strategy_version", strategy_version),
            ("scoring_version", scoring_version),
            ("pool_version", pool_version),
        ):
            if value:
                filters.append(f"{column}=?")
                params_list.append(value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params = tuple(params_list)
        with self._connection() as conn:
            row = conn.execute(
                f"""SELECT * FROM topdown_score_snapshots {where}
                    ORDER BY bar_closed_at DESC,created_at DESC LIMIT 1""",
                params,
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json") or "{}")
        return result

    def list_topdown_scores(
        self,
        *,
        symbol: str = "",
        strategy_version: str = "",
        scoring_version: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self._require_available()
        filters: list[str] = []
        params_list: list[Any] = []
        for column, value in (
            ("symbol", symbol),
            ("strategy_version", strategy_version),
            ("scoring_version", scoring_version),
        ):
            if value:
                filters.append(f"{column}=?")
                params_list.append(value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params = (*params_list, limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT * FROM topdown_score_snapshots {where}
                    ORDER BY bar_closed_at DESC LIMIT ?""",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            result.append(item)
        return result

    def upsert_stock_sandbox(self, snapshot: Any) -> dict[str, Any] | None:
        """Persist one symbol's latest independent state and return its prior state."""
        self._require_available()
        payload = (
            snapshot.model_dump(mode="json")
            if hasattr(snapshot, "model_dump")
            else dict(snapshot)
        )
        symbol = str(payload.get("symbol") or "")
        pool_version = str(payload.get("pool_version") or "")
        if not symbol or not pool_version:
            raise ValueError("stock sandbox requires symbol and pool_version")
        now = _now()
        with self._write_lock, self._connection() as conn:
            previous = conn.execute(
                """SELECT snapshot_json FROM stock_sandbox_current
                   WHERE symbol=? AND pool_version=?""",
                (symbol, pool_version),
            ).fetchone()
            conn.execute(
                """INSERT INTO stock_sandbox_current(
                    symbol,pool_version,observed_at,state,input_hash,snapshot_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,pool_version) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    state=excluded.state,
                    input_hash=excluded.input_hash,
                    snapshot_json=excluded.snapshot_json,
                    updated_at=excluded.updated_at""",
                (
                    symbol,
                    pool_version,
                    str(payload.get("observed_at") or now),
                    str(payload.get("state") or ""),
                    str(payload.get("input_hash") or ""),
                    _json(payload),
                    now,
                    now,
                ),
            )
        return json.loads(previous[0]) if previous else None

    def list_stock_sandboxes(
        self, *, pool_version: str = "", limit: int = 500
    ) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE pool_version=?" if pool_version else ""
        params: tuple[Any, ...] = (
            (pool_version, limit) if pool_version else (limit,)
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT * FROM stock_sandbox_current {where}
                    ORDER BY updated_at DESC,symbol ASC LIMIT ?""",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            result.append(item)
        return result

    def claim_quant_notification(
        self,
        *,
        event_key: str,
        symbol: str,
        event_type: str,
        bar_closed_at: str = "",
        plan_id: str = "",
        details: dict[str, Any] | None = None,
        retry_failed: bool = False,
        max_attempts: int = 1,
        retry_after_seconds: int = 60,
        recover_pending_after_seconds: int = 300,
    ) -> bool:
        """Atomically claim or recover a bounded notification attempt.

        Delivered events are immutable.  Failed events may be retried only
        when explicitly requested, after a backoff and up to ``max_attempts``.
        A stale pending row can be reclaimed after a longer timeout so an app
        crash between claim and completion does not suppress the alert forever.
        """
        self._require_available()
        if not event_key:
            raise ValueError("notification event_key is required")
        now = _now()
        initial_details = {**(details or {}), "attempt_count": 1}
        with self._write_lock, self._connection() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO quant_notification_events(
                    event_key,symbol,event_type,bar_closed_at,plan_id,status,
                    details_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    event_key,
                    symbol,
                    event_type,
                    bar_closed_at,
                    plan_id,
                    "pending",
                    _json(initial_details),
                    now,
                    now,
                ),
            )
            if cursor.rowcount > 0:
                return True
            if not retry_failed or max_attempts <= 1:
                return False
            row = conn.execute(
                """SELECT status,details_json,updated_at
                   FROM quant_notification_events WHERE event_key=?""",
                (event_key,),
            ).fetchone()
            if row is None or str(row["status"]) == "delivered":
                return False
            existing = json.loads(row["details_json"] or "{}")
            attempts = max(1, int(existing.get("attempt_count") or 1))
            if attempts >= max(1, int(max_attempts)):
                return False
            try:
                updated_at = datetime.fromisoformat(str(row["updated_at"]))
                current = datetime.fromisoformat(now)
            except ValueError:
                return False
            status = str(row["status"])
            retry_delay = (
                max(0, int(recover_pending_after_seconds))
                if status == "pending"
                else max(0, int(retry_after_seconds))
            )
            if status not in {"failed", "pending"}:
                return False
            if current < updated_at + timedelta(seconds=retry_delay):
                return False
            retry_details = {
                **existing,
                **(details or {}),
                "attempt_count": attempts + 1,
                "retry_started_at": now,
            }
            retry = conn.execute(
                """UPDATE quant_notification_events
                   SET status='pending',details_json=?,updated_at=?
                   WHERE event_key=? AND status=? AND updated_at=?""",
                (
                    _json(retry_details),
                    now,
                    event_key,
                    status,
                    str(row["updated_at"]),
                ),
            )
            return retry.rowcount > 0

    def finish_quant_notification(
        self, event_key: str, *, delivered: bool, details: dict[str, Any] | None = None
    ) -> None:
        self._require_available()
        with self._write_lock, self._connection() as conn:
            row = conn.execute(
                "SELECT details_json FROM quant_notification_events WHERE event_key=?",
                (event_key,),
            ).fetchone()
            existing = json.loads(row["details_json"] or "{}") if row else {}
            merged = {**existing, **(details or {})}
            conn.execute(
                """UPDATE quant_notification_events
                   SET status=?,details_json=?,updated_at=? WHERE event_key=?""",
                (
                    "delivered" if delivered else "failed",
                    _json(merged),
                    _now(),
                    event_key,
                ),
            )

    def list_quant_notifications(self, *, limit: int = 200) -> list[dict[str, Any]]:
        self._require_available()
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM quant_notification_events
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            result.append(item)
        return result

    def add_hotspot_snapshot(self, snapshot: Any) -> str:
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        snapshot_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO hotspot_snapshots(
                    id,symbol,frozen_at,source_hash,snapshot_json,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    str(payload.get("symbol", "")),
                    str(payload.get("frozen_at", "")),
                    str(payload.get("source_hash", "")),
                    _json(payload),
                    _now(),
                ),
            )
            row = conn.execute(
                """SELECT id FROM hotspot_snapshots
                   WHERE symbol=? AND frozen_at=? AND source_hash=?""",
                (
                    str(payload.get("symbol", "")),
                    str(payload.get("frozen_at", "")),
                    str(payload.get("source_hash", "")),
                ),
            ).fetchone()
        return str(row[0])

    def latest_hotspot_snapshot(self, symbol: str) -> dict[str, Any] | None:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute(
                """SELECT * FROM hotspot_snapshots WHERE symbol=?
                   ORDER BY frozen_at DESC,created_at DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json") or "{}")
        return result

    def add_stock_selection_snapshot(self, snapshot: Any) -> str:
        """Persist one deterministic discovery scan without creating trade facts."""
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        snapshot_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO stock_selection_snapshots(
                    id,strategy_version,generated_at,status,input_hash,snapshot_json,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    str(payload.get("strategy_version") or ""),
                    str(payload.get("generated_at") or ""),
                    str(payload.get("status") or ""),
                    str(payload.get("input_hash") or ""),
                    _json(payload),
                    _now(),
                ),
            )
            row = conn.execute(
                """SELECT id FROM stock_selection_snapshots
                   WHERE strategy_version=? AND generated_at=? AND input_hash=?""",
                (
                    str(payload.get("strategy_version") or ""),
                    str(payload.get("generated_at") or ""),
                    str(payload.get("input_hash") or ""),
                ),
            ).fetchone()
        return str(row[0])

    def latest_stock_selection_snapshot(
        self, strategy_version: str = ""
    ) -> dict[str, Any] | None:
        self._require_available()
        where = "WHERE strategy_version=?" if strategy_version else ""
        params = (strategy_version,) if strategy_version else ()
        with self._connection() as conn:
            row = conn.execute(
                f"""SELECT * FROM stock_selection_snapshots {where}
                    ORDER BY generated_at DESC,created_at DESC LIMIT 1""",
                params,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json") or "{}")
        return result

    def add_market_sentiment_snapshot(self, snapshot: Any) -> str:
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        snapshot_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO market_sentiment_snapshots(
                    id,captured_at,source_as_of,data_complete,source_hash,snapshot_json,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    str(payload.get("captured_at", "")),
                    str(payload.get("source_as_of", "")),
                    int(bool(payload.get("data_complete"))),
                    str(payload.get("source_hash", "")),
                    _json(payload),
                    _now(),
                ),
            )
            row = conn.execute(
                """SELECT id FROM market_sentiment_snapshots
                   WHERE captured_at=? AND source_hash=?""",
                (str(payload.get("captured_at", "")), str(payload.get("source_hash", ""))),
            ).fetchone()
        return str(row[0])

    def list_market_sentiment_snapshots(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._require_available()
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM market_sentiment_snapshots
                   ORDER BY captured_at DESC,created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            result.append(item)
        return result

    def update_market_daily_prices_and_high_low(
        self,
        rows: list[dict[str, Any]],
        *,
        as_of: str,
        captured_at: str,
        lookback_sessions: int = 20,
    ) -> tuple[int | None, int | None, dict[str, Any]]:
        """Persist today's full-market prices and compare with prior sessions.

        Today's rows are excluded from the baseline.  Until at least 20 prior
        dates and 3,000 comparable symbols exist, no counts are returned.
        """
        self._require_available()
        clean = {
            str(item.get("code") or ""): float(item["price"])
            for item in rows
            if str(item.get("code") or "").isdigit()
            and item.get("price") is not None
            and float(item["price"]) > 0
        }
        with self._write_lock, self._connection() as conn:
            dates = [
                str(item[0]) for item in conn.execute(
                    """SELECT DISTINCT as_of FROM market_daily_prices
                       WHERE as_of < ? ORDER BY as_of DESC LIMIT ?""",
                    (as_of, max(1, int(lookback_sessions))),
                ).fetchall()
            ]
            history: dict[str, list[float]] = {}
            if len(dates) >= lookback_sessions:
                placeholders = ",".join("?" for _ in dates)
                for item in conn.execute(
                    f"""SELECT symbol,price FROM market_daily_prices
                        WHERE as_of IN ({placeholders})""",
                    tuple(dates),
                ).fetchall():
                    history.setdefault(str(item[0]), []).append(float(item[1]))
            conn.executemany(
                """INSERT INTO market_daily_prices(as_of,symbol,price,captured_at)
                   VALUES(?,?,?,?) ON CONFLICT(as_of,symbol) DO UPDATE SET
                   price=excluded.price,captured_at=excluded.captured_at""",
                [(as_of, symbol, price, captured_at) for symbol, price in clean.items()],
            )
        comparable = {
            symbol: values for symbol, values in history.items()
            if symbol in clean and len(values) >= lookback_sessions
        }
        details = {
            "as_of": as_of,
            "stored_count": len(clean),
            "prior_sessions": len(dates),
            "comparable_count": len(comparable),
            "lookback_sessions": lookback_sessions,
        }
        if len(dates) < lookback_sessions:
            details["reason"] = "market_price_history_requires_20_prior_sessions"
            return None, None, details
        if len(comparable) < 3000:
            details["reason"] = "market_price_history_coverage_below_3000"
            return None, None, details
        new_high = sum(clean[symbol] >= max(values) for symbol, values in comparable.items())
        new_low = sum(clean[symbol] <= min(values) for symbol, values in comparable.items())
        return new_high, new_low, details

    def market_daily_price_dates(self, *, limit: int = 30) -> list[str]:
        """Return the newest distinct full-market snapshot dates."""
        self._require_available()
        safe_limit = max(1, int(limit))
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT as_of FROM market_daily_prices
                   ORDER BY as_of DESC LIMIT ?""",
                (safe_limit,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def market_daily_history_for_symbols(
        self,
        symbols: set[str],
        *,
        before_as_of: str,
        limit_sessions: int,
    ) -> dict[str, list[float]]:
        """Return real closes for the latest explicit sessions before a date."""
        self._require_available()
        clean_symbols = sorted({str(value) for value in symbols if str(value)})
        required = max(1, int(limit_sessions))
        if not clean_symbols:
            return {}
        with self._connection() as conn:
            dates = [
                str(row[0])
                for row in conn.execute(
                    """SELECT DISTINCT as_of FROM market_daily_prices
                       WHERE as_of<? ORDER BY as_of DESC LIMIT ?""",
                    (str(before_as_of)[:10], required),
                ).fetchall()
            ]
            if len(dates) != required:
                return {}
            date_placeholders = ",".join("?" for _ in dates)
            symbol_placeholders = ",".join("?" for _ in clean_symbols)
            rows = conn.execute(
                f"""SELECT symbol,as_of,price FROM market_daily_prices
                    WHERE as_of IN ({date_placeholders})
                      AND symbol IN ({symbol_placeholders})
                    ORDER BY symbol,as_of""",
                (*dates, *clean_symbols),
            ).fetchall()
        result: dict[str, list[float]] = {}
        for row in rows:
            result.setdefault(str(row[0]), []).append(float(row[2]))
        return result

    def upsert_market_daily_price_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        captured_at: str,
    ) -> int:
        """Persist verified historical closes without manufacturing missing days."""
        self._require_available()
        clean: dict[tuple[str, str], float] = {}
        for item in rows:
            as_of = str(item.get("as_of") or "")[:10]
            symbol = str(item.get("symbol") or "")
            price = item.get("price")
            if (
                len(as_of) == 10
                and symbol.isdigit()
                and len(symbol) == 6
                and price is not None
                and float(price) > 0
            ):
                clean[(as_of, symbol)] = float(price)
        if not clean:
            return 0
        with self._write_lock, self._connection() as conn:
            conn.executemany(
                """INSERT INTO market_daily_prices(as_of,symbol,price,captured_at)
                   VALUES(?,?,?,?) ON CONFLICT(as_of,symbol) DO UPDATE SET
                   price=excluded.price,captured_at=excluded.captured_at""",
                [
                    (as_of, symbol, price, captured_at)
                    for (as_of, symbol), price in clean.items()
                ],
            )
        return len(clean)

    def complete_market_history_symbols(
        self,
        symbols: set[str],
        *,
        session_dates: list[str],
    ) -> set[str]:
        """Return symbols already covering every requested closed session."""
        self._require_available()
        if not symbols or not session_dates:
            return set()
        first, last = min(session_dates), max(session_dates)
        required = len(set(session_dates))
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT symbol,COUNT(DISTINCT as_of) AS sessions
                   FROM market_daily_prices
                   WHERE as_of>=? AND as_of<=?
                   GROUP BY symbol HAVING sessions>=?""",
                (first, last, required),
            ).fetchall()
        return {str(row[0]) for row in rows if str(row[0]) in symbols}

    def market_daily_price_coverage(
        self,
        *,
        session_dates: list[str],
    ) -> dict[str, int]:
        """Count distinct real symbols for each explicitly requested session."""
        self._require_available()
        dates = list(dict.fromkeys(str(value)[:10] for value in session_dates if value))
        if not dates:
            return {}
        result = {value: 0 for value in dates}
        for offset in range(0, len(dates), 400):
            batch = dates[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            with self._connection() as conn:
                rows = conn.execute(
                    f"""SELECT as_of,COUNT(DISTINCT symbol)
                        FROM market_daily_prices
                        WHERE as_of IN ({placeholders}) GROUP BY as_of""",
                    tuple(batch),
                ).fetchall()
            for row in rows:
                result[str(row[0])] = int(row[1])
        return result

    def add_outside_pool_approval(
        self,
        *,
        review_id: str,
        plan_id: str,
        account_fingerprint: str,
        effective_risk_pct: float,
        valid_until: str,
        audit_reason: str,
    ) -> str:
        self._require_available()
        approval_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO outside_pool_approvals(
                    id,review_id,plan_id,account_fingerprint,effective_risk_pct,
                    valid_until,audit_reason,approved_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    approval_id, review_id, plan_id, account_fingerprint,
                    effective_risk_pct, valid_until, audit_reason, _now(),
                ),
            )
        return approval_id

    def valid_outside_pool_approval(
        self,
        *,
        plan_id: str,
        account_fingerprint: str,
        at_time: str | None = None,
    ) -> dict[str, Any] | None:
        self._require_available()
        point = at_time or _now()
        with self._connection() as conn:
            row = conn.execute(
                """SELECT * FROM outside_pool_approvals
                   WHERE plan_id=? AND account_fingerprint=? AND valid_until>=?
                   ORDER BY approved_at DESC LIMIT 1""",
                (plan_id, account_fingerprint, point),
            ).fetchone()
        return dict(row) if row else None

    def add_quant_signal(self, decision: Any, *, plan_id: str | None = None) -> str:
        """Persist a deterministic signal independently from AI decisions."""
        self._require_available()
        payload = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else dict(decision)
        signal_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            identity = (
                str(payload.get("strategy_id", "")),
                str(payload.get("parameter_version", "")),
                str(payload.get("pool_version", "")),
                str(payload.get("symbol", "")),
                str(payload.get("signal_time", "")),
            )
            existing = conn.execute(
                """SELECT id FROM quant_signals
                   WHERE strategy_id=? AND parameter_version=? AND pool_version=?
                     AND symbol=? AND signal_time=?""",
                identity,
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            conn.execute(
                """INSERT INTO quant_signals(
                    id,created_at,symbol,strategy_id,parameter_version,pool_version,
                    signal_time,status,decision_json,plan_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal_id, _now(), str(payload.get("symbol", "")),
                    str(payload.get("strategy_id", "")), str(payload.get("parameter_version", "")),
                    str(payload.get("pool_version", "")), str(payload.get("signal_time", "")),
                    str(payload.get("status", "")), _json(payload), plan_id,
                ),
            )
        return signal_id

    def list_quant_signals(self, *, strategy_id: str = "", limit: int = 500) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE strategy_id=?" if strategy_id else ""
        params: tuple[Any, ...] = (strategy_id, limit) if strategy_id else (limit,)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM quant_signals {where} ORDER BY signal_time DESC LIMIT ?", params
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["decision"] = json.loads(item.pop("decision_json") or "{}")
            result.append(item)
        return result

    def record_strategy_transition(
        self,
        transition: Any,
        evidence: Any,
        *,
        strategy_id: str,
        validation_run_id: str = "",
        live_approval: Any | None = None,
    ) -> int:
        self._require_available()
        transition_data = transition.model_dump(mode="json") if hasattr(transition, "model_dump") else dict(transition)
        evidence_data = evidence.model_dump(mode="json") if hasattr(evidence, "model_dump") else dict(evidence)
        approval_data = (
            live_approval.model_dump(mode="json")
            if hasattr(live_approval, "model_dump")
            else dict(live_approval or {})
        )
        previous_state = str(transition_data.get("previous", ""))
        current_state = str(transition_data.get("current", ""))
        promotion = (previous_state, current_state) in {
            ("candidate", "shadow"),
            ("shadow", "active"),
        }
        with self._write_lock, self._connection() as conn:
            latest = conn.execute(
                """SELECT current_state FROM strategy_state_events
                   WHERE strategy_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()
            stored_state = str(latest[0]) if latest else "candidate"
            if previous_state != stored_state:
                raise ValueError(
                    "strategy transition previous state does not match stored state"
                )
            allowed_edges = {
                "candidate": {"candidate", "shadow"},
                "shadow": {"shadow", "active", "paused", "retired"},
                "active": {"active", "reduced", "paused", "retired"},
                "reduced": {"reduced", "paused", "retired"},
                "paused": {"paused", "shadow", "retired"},
                "retired": {"retired"},
            }
            if current_state not in allowed_edges.get(previous_state, set()):
                raise ValueError(
                    f"invalid strategy state transition: {previous_state}->{current_state}"
                )
            if promotion:
                validation = conn.execute(
                    """SELECT strategy_version,dataset,status,promotion_eligible,report_json
                       FROM strategy_validation_runs WHERE id=?""",
                    (validation_run_id,),
                ).fetchone()
                expected_dataset = (
                    "out_of_sample" if current_state == "shadow" else "shadow"
                )
                if (
                    validation is None
                    or str(validation["strategy_version"]) != strategy_id
                    or str(validation["dataset"]) != expected_dataset
                    or str(validation["status"]) != "complete"
                    or not bool(validation["promotion_eligible"])
                ):
                    raise ValueError(
                        "strategy promotion requires matching complete "
                        "promotion-eligible validation evidence"
                    )
                stored_evidence = json.loads(validation["report_json"] or "{}").get(
                    "performance_evidence"
                )
                if stored_evidence != evidence_data:
                    raise ValueError(
                        "strategy transition evidence does not match validation report"
                    )
            if current_state == "active":
                required = {
                    "approved_at",
                    "account_fingerprint",
                    "initial_risk_pct",
                    "acknowledgment_version",
                }
                if not required.issubset(approval_data):
                    raise ValueError(
                        "active strategy transition requires explicit live activation approval"
                    )
            cursor = conn.execute(
                """INSERT INTO strategy_state_events(
                    strategy_id,previous_state,current_state,reasons_json,evidence_json,
                    validation_run_id,approval_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    strategy_id,
                    previous_state,
                    current_state,
                    _json(transition_data.get("reasons", [])),
                    _json(evidence_data),
                    validation_run_id,
                    _json(approval_data),
                    _now(),
                ),
            )
        return int(cursor.lastrowid)

    def list_strategy_transitions(
        self, *, strategy_id: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE strategy_id=?" if strategy_id else ""
        params: tuple[Any, ...] = (
            (strategy_id, limit) if strategy_id else (limit,)
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT * FROM strategy_state_events {where}
                    ORDER BY created_at DESC,id DESC LIMIT ?""",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            item["approval"] = json.loads(item.pop("approval_json") or "{}")
            result.append(item)
        return result

    def current_strategy_state(self, strategy_id: str, *, default: str = "candidate") -> str:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute(
                """SELECT current_state FROM strategy_state_events
                   WHERE strategy_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()
        return str(row[0]) if row else default

    def add_broker_snapshot(self, snapshot: Any) -> str:
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        snapshot_id = uuid.uuid4().hex
        connection = payload.get("connection") or {}
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO broker_snapshots(
                    id,account_fingerprint,connection_status,captured_at,complete,snapshot_json,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot_id, str(payload.get("account_fingerprint", "")),
                    str(connection.get("status", "")), str(payload.get("captured_at", _now())),
                    int(bool(payload.get("complete"))), _json(payload), _now(),
                ),
            )
        return snapshot_id

    def latest_broker_snapshot(self, account_fingerprint: str = "") -> dict[str, Any] | None:
        self._require_available()
        where = "WHERE account_fingerprint=?" if account_fingerprint else ""
        params: tuple[Any, ...] = (account_fingerprint,) if account_fingerprint else ()
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT * FROM broker_snapshots {where} ORDER BY captured_at DESC LIMIT 1", params
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json") or "{}")
        return result

    def add_equity_snapshot(
        self,
        snapshot: Any,
        *,
        external_cash_flow: float = 0.0,
        monthly_return_pct: float | None = None,
    ) -> str:
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        if payload.get("total_equity") is None:
            raise ValueError("complete total equity required")
        snapshot_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO equity_snapshots(
                    id,account_fingerprint,captured_at,total_equity,available_cash,position_value,
                    external_cash_flow,monthly_return_pct,snapshot_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id, str(payload.get("account_fingerprint", "")),
                    str(payload.get("captured_at", _now())), float(payload["total_equity"]),
                    payload.get("available_cash"), payload.get("position_value"),
                    external_cash_flow, monthly_return_pct, _json(payload), _now(),
                ),
            )
        return snapshot_id

    @staticmethod
    def _cash_flow_fingerprint(account_fingerprint: str, payload: dict[str, Any]) -> str:
        canonical = "|".join((
            account_fingerprint.strip(),
            str(payload.get("occurred_at") or "").strip(),
            str(payload.get("direction") or "").strip().lower(),
            f"{float(payload.get('amount') or 0):.8f}",
            str(payload.get("description") or "").strip(),
        ))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def upsert_broker_cash_flows(
        self,
        account_fingerprint: str,
        cash_flows: list[Any],
        *,
        captured_at: str,
        range_start: str,
        range_end: str,
        complete: bool,
        warnings: list[str] | None = None,
    ) -> list[str]:
        """Persist one explicitly bounded broker cash-flow query, including empty results."""
        self._require_available()
        account = account_fingerprint.strip()
        if not account:
            raise ValueError("account fingerprint is required for broker cash flows")
        captured = datetime.fromisoformat(captured_at)
        start = datetime.fromisoformat(range_start)
        end = datetime.fromisoformat(range_end)
        if start > end or end > captured:
            raise ValueError("invalid broker cash-flow query range")
        payloads: list[dict[str, Any]] = []
        fingerprints: list[str] = []
        for flow in cash_flows:
            payload = flow.model_dump(mode="json") if hasattr(flow, "model_dump") else dict(flow)
            direction = str(payload.get("direction") or "").strip().lower()
            amount = float(payload.get("amount") or 0)
            occurred_at = str(payload.get("occurred_at") or "").strip()
            if direction not in {"deposit", "withdrawal"}:
                raise ValueError("broker cash-flow direction must be deposit or withdrawal")
            if amount <= 0:
                raise ValueError("broker cash-flow amount must be positive")
            occurred = datetime.fromisoformat(occurred_at)
            if occurred < start or occurred > end:
                raise ValueError("broker cash flow is outside the declared query range")
            payload["direction"] = direction
            payload["amount"] = amount
            payload["occurred_at"] = occurred_at
            payloads.append(payload)
            fingerprints.append(self._cash_flow_fingerprint(account, payload))

        sync_id = uuid.uuid4().hex
        seen_at = _now()
        with self._write_lock, self._connection() as conn:
            for fingerprint, payload in zip(fingerprints, payloads, strict=True):
                conn.execute(
                    """INSERT INTO broker_cash_flows(
                        fingerprint,account_fingerprint,broker_flow_id,direction,amount,
                        occurred_at,status,description,source,raw_json,first_seen_at,last_seen_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        status=excluded.status,source=excluded.source,raw_json=excluded.raw_json,
                        last_seen_at=excluded.last_seen_at""",
                    (
                        fingerprint, account, str(payload.get("broker_flow_id") or ""),
                        payload["direction"], payload["amount"], payload["occurred_at"],
                        str(payload.get("status") or "confirmed"),
                        str(payload.get("description") or ""),
                        str(payload.get("source") or "ths_ui"), _json(payload), seen_at, seen_at,
                    ),
                )
            conn.execute(
                """INSERT INTO broker_cash_flow_syncs(
                    id,account_fingerprint,range_start,range_end,captured_at,complete,
                    row_count,warnings_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_fingerprint,range_start,range_end,captured_at) DO UPDATE SET
                    complete=excluded.complete,row_count=excluded.row_count,
                    warnings_json=excluded.warnings_json""",
                (
                    sync_id, account, range_start, range_end, captured_at, int(complete),
                    len(payloads), _json(warnings or []), seen_at,
                ),
            )
        return fingerprints

    def list_broker_cash_flows(
        self,
        *,
        account_fingerprint: str = "",
        start_at: str = "",
        end_at: str = "",
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self._require_available()
        clauses: list[str] = []
        values: list[Any] = []
        if account_fingerprint:
            clauses.append("account_fingerprint=?")
            values.append(account_fingerprint)
        if start_at:
            clauses.append("occurred_at>=?")
            values.append(start_at)
        if end_at:
            clauses.append("occurred_at<=?")
            values.append(end_at)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM broker_cash_flows {where} ORDER BY occurred_at LIMIT ?",
                tuple(values),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(item.pop("raw_json") or "{}")
            result.append(item)
        return result

    def list_broker_cash_flow_syncs(
        self, *, account_fingerprint: str = "", limit: int = 1000
    ) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE account_fingerprint=?" if account_fingerprint else ""
        params: tuple[Any, ...] = (
            (account_fingerprint, limit) if account_fingerprint else (limit,)
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM broker_cash_flow_syncs {where} ORDER BY captured_at DESC LIMIT ?",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
            result.append(item)
        return result

    def cash_flow_between(
        self, account_fingerprint: str, *, after: str, through: str
    ) -> float:
        """Return signed confirmed external flow for ``after < occurred_at <= through``."""
        self._require_available()
        with self._connection() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(CASE direction
                       WHEN 'deposit' THEN amount ELSE -amount END),0)
                   FROM broker_cash_flows
                   WHERE account_fingerprint=? AND occurred_at>? AND occurred_at<=?
                     AND lower(status) IN ('confirmed','success','successful','已成','成功','已确认')""",
                (account_fingerprint, after, through),
            ).fetchone()
        return float(row[0] or 0)

    def cash_flow_history_complete(
        self, account_fingerprint: str, *, range_start: str, range_end: str
    ) -> bool:
        """Prove that one successful broker query covers the required interval."""
        required_start = datetime.fromisoformat(range_start)
        required_end = datetime.fromisoformat(range_end)
        for item in self.list_broker_cash_flow_syncs(
            account_fingerprint=account_fingerprint
        ):
            if not item.get("complete"):
                continue
            start = datetime.fromisoformat(str(item["range_start"]))
            end = datetime.fromisoformat(str(item["range_end"]))
            if start <= required_start and end >= required_end:
                return True
        return False

    def record_broker_financial_snapshot(self, snapshot: Any) -> str:
        """Persist flows first, then attach exactly-once interval flow to equity."""
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        account = str(payload.get("account_fingerprint") or "")
        captured_at = str(payload.get("captured_at") or "")
        range_start = str(payload.get("cash_flow_range_start") or "")
        range_end = str(payload.get("cash_flow_range_end") or "")
        if range_start and range_end:
            self.upsert_broker_cash_flows(
                account,
                list(payload.get("cash_flows") or []),
                captured_at=captured_at,
                range_start=range_start,
                range_end=range_end,
                complete=bool(payload.get("cash_flow_complete")),
                warnings=list(payload.get("warnings") or []),
            )
        if payload.get("total_equity") is None:
            raise ValueError("complete total equity required")
        with self._connection() as conn:
            previous = conn.execute(
                """SELECT captured_at FROM equity_snapshots
                   WHERE account_fingerprint=? AND captured_at<?
                   ORDER BY captured_at DESC LIMIT 1""",
                (account, captured_at),
            ).fetchone()
        external_flow = (
            self.cash_flow_between(account, after=str(previous[0]), through=captured_at)
            if previous else 0.0
        )
        with self._write_lock, self._connection() as conn:
            existing = conn.execute(
                """SELECT id FROM equity_snapshots
                   WHERE account_fingerprint=? AND captured_at=? LIMIT 1""",
                (account, captured_at),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE equity_snapshots SET total_equity=?,available_cash=?,
                       position_value=?,external_cash_flow=?,snapshot_json=? WHERE id=?""",
                    (
                        float(payload["total_equity"]), payload.get("available_cash"),
                        payload.get("position_value"), external_flow, _json(payload), existing[0],
                    ),
                )
                return str(existing[0])
        return self.add_equity_snapshot(snapshot, external_cash_flow=external_flow)

    def list_equity_snapshots(
        self, *, account_fingerprint: str = "", limit: int = 2000
    ) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE account_fingerprint=?" if account_fingerprint else ""
        params: tuple[Any, ...] = (
            (account_fingerprint, limit) if account_fingerprint else (limit,)
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM equity_snapshots {where} ORDER BY captured_at LIMIT ?", params
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            result.append(item)
        return result

    def link_broker_order(
        self,
        reconciliation: Any,
        *,
        account_fingerprint: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        self._require_available()
        if not account_fingerprint:
            raise ValueError("broker order link requires account fingerprint")
        payload = reconciliation.model_dump(mode="json") if hasattr(reconciliation, "model_dump") else dict(reconciliation)
        orders = list(payload.get("matched_order_ids") or [])
        if len(orders) != 1:
            raise ValueError("a unique broker order is required")
        link_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO broker_order_links(
                    id,account_fingerprint,plan_id,broker_order_id,broker_fill_ids_json,
                    match_status,details_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_fingerprint,plan_id,broker_order_id) DO UPDATE SET
                    broker_fill_ids_json=excluded.broker_fill_ids_json,
                    match_status=excluded.match_status,details_json=excluded.details_json,
                    updated_at=excluded.updated_at""",
                (
                    link_id, account_fingerprint, str(payload.get("plan_id", "")), orders[0],
                    _json(payload.get("matched_fill_ids", [])), str(payload.get("status", "")),
                    _json({**(details or {}), "account_fingerprint": account_fingerprint}),
                    _now(), _now(),
                ),
            )
        return link_id

    def list_broker_order_links(
        self, *, account_fingerprint: str = ""
    ) -> list[dict[str, Any]]:
        """Return decoded broker links used to prevent duplicate/manual fill attribution."""
        self._require_available()
        with self._connection() as conn:
            if account_fingerprint:
                rows = conn.execute(
                    "SELECT * FROM broker_order_links WHERE account_fingerprint=? "
                    "ORDER BY updated_at DESC",
                    (account_fingerprint,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM broker_order_links ORDER BY updated_at DESC"
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["broker_fill_ids"] = json.loads(item.pop("broker_fill_ids_json") or "[]")
            item["details"] = json.loads(item.pop("details_json") or "{}")
            result.append(item)
        return result

    def linked_broker_fill_ids(
        self, *, account_fingerprint: str = ""
    ) -> set[str]:
        return {
            str(fill_id)
            for link in self.list_broker_order_links(
                account_fingerprint=account_fingerprint
            )
            for fill_id in link.get("broker_fill_ids", [])
            if fill_id
        }

    def add_external_broker_trade(self, fill: Any, *, account_fingerprint: str) -> bool:
        """Persist an unmatched TongHuaShun fill without assigning it to a strategy plan."""
        self._require_available()
        payload = fill.model_dump(mode="json") if hasattr(fill, "model_dump") else dict(fill)
        fill_id = str(payload.get("broker_fill_id", ""))
        if not fill_id:
            raise ValueError("external broker fill id is required")
        if not account_fingerprint:
            raise ValueError("external broker account fingerprint is required")
        identity = hashlib.sha256(
            f"{account_fingerprint}|{fill_id}".encode("utf-8")
        ).hexdigest()
        with self._write_lock, self._connection() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO external_broker_trades(
                    identity,broker_fill_id,account_fingerprint,broker_order_id,
                    symbol,direction,price,quantity,fees,filled_at,fill_json,
                    first_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity, fill_id, account_fingerprint,
                    str(payload.get("broker_order_id", "")),
                    str(payload.get("symbol", "")), str(payload.get("direction", "")),
                    float(payload.get("price") or 0), int(payload.get("quantity") or 0),
                    float(payload.get("fees") or 0), str(payload.get("filled_at", "")),
                    _json(payload), _now(),
                ),
            )
        return cursor.rowcount > 0

    def list_external_broker_trades(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        self._require_available()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM external_broker_trades ORDER BY filled_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(row), "fill": json.loads(row["fill_json"])} for row in rows]

    def upsert_broker_execution(
        self,
        *,
        plan_id: str,
        fills: list[Any],
        plan_status: str,
        event_type: str,
        broker_order_id: str,
        account_fingerprint: str,
    ) -> None:
        """Apply real broker fills idempotently; broker data remains the source of truth."""
        self._require_available()
        if not account_fingerprint:
            raise ValueError("broker execution requires account fingerprint")
        payloads = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in fills
        ]
        quantity = sum(float(item.get("quantity") or 0) for item in payloads)
        total_value = sum(
            float(item.get("price") or 0) * float(item.get("quantity") or 0)
            for item in payloads
        )
        fees = sum(float(item.get("fees") or 0) for item in payloads)
        executed_at = max(
            (str(item.get("filled_at") or "") for item in payloads), default=_now()
        ) or _now()
        execution_id = f"broker-{plan_id}"
        with self._write_lock, self._connection() as conn:
            plan = conn.execute("SELECT id FROM trade_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise ValueError("trade plan not found")
            link = conn.execute(
                """SELECT id FROM broker_order_links
                   WHERE account_fingerprint=? AND plan_id=? AND broker_order_id=?""",
                (account_fingerprint, plan_id, broker_order_id),
            ).fetchone()
            if link is None:
                raise ValueError("broker execution does not match an account-scoped order link")
            if quantity > 0:
                average_price = total_value / quantity
                conn.execute(
                    """INSERT INTO executions(
                        id,plan_id,executed_at,price,quantity,real_contract,fees,
                        account_fingerprint,note,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET executed_at=excluded.executed_at,
                        price=excluded.price,quantity=excluded.quantity,fees=excluded.fees,
                        account_fingerprint=excluded.account_fingerprint,note=excluded.note""",
                    (
                        execution_id, plan_id, executed_at, average_price, quantity, "", fees,
                        account_fingerprint,
                        f"同花顺真实成交；委托 {broker_order_id}", _now(),
                    ),
                )
            conn.execute(
                "UPDATE trade_plans SET status=?,updated_at=? WHERE id=?",
                (plan_status, _now(), plan_id),
            )
            previous = conn.execute(
                """SELECT details_json FROM trade_events WHERE plan_id=? AND event_type=?
                   ORDER BY id DESC LIMIT 1""",
                (plan_id, event_type),
            ).fetchone()
            details = {
                "broker_order_id": broker_order_id,
                "account_fingerprint": account_fingerprint,
                "broker_fill_ids": [item.get("broker_fill_id") for item in payloads],
                "quantity": quantity,
                "fees": fees,
            }
            if previous is None or json.loads(previous[0] or "{}") != details:
                conn.execute(
                    """INSERT INTO trade_events(
                        plan_id,event_type,event_at,dataset,price,details_json
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        plan_id, event_type, executed_at, "actual",
                        (total_value / quantity if quantity else None), _json(details),
                    ),
                )

    def add_decision(
        self,
        *,
        symbol: str,
        timeframe: str,
        asset_class: str,
        original_decision: dict[str, Any],
        final_decision: dict[str, Any],
        meta: dict[str, Any],
        market_state: str = "",
        confidence: float | None = None,
        analysis_record_ref: str = "",
        price_adjustments: list[dict[str, Any]] | None = None,
        audit_reason: str = "",
        decision_id: str | None = None,
    ) -> str:
        self._require_available()
        decision_id = decision_id or uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO decision_events(
                    id,created_at,symbol,timeframe,asset_class,market_state,confidence,
                    strategy_version,feature_version,model_name,app_git_commit,
                    prompt_snapshot_json,analysis_record_ref,original_decision_json,
                    final_decision_json,price_adjustments_json,audit_reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, _now(), symbol, timeframe, asset_class, market_state, confidence,
                    str(meta.get("strategy_version", "")), str(meta.get("feature_version", "")),
                    str(meta.get("model_name", "")), str(meta.get("app_git_commit", "")),
                    _json(meta.get("prompt_snapshot", [])), analysis_record_ref,
                    _json(original_decision), _json(final_decision),
                    _json(price_adjustments or []), audit_reason,
                ),
            )
        return decision_id

    def add_plan(self, plan: TradePlan) -> str:
        self._require_available()
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO trade_plans(
                    id,decision_event_id,analysis_record_ref,symbol,timeframe,asset_class,direction,
                    order_type,entry_price,stop_loss_price,take_profit_price,take_profit_price_2,
                    valid_until,status,shadow_status,strategy_version,risk_snapshot_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan.id, plan.decision_event_id, plan.analysis_record_ref, plan.symbol,
                    plan.timeframe, str(plan.asset_class), plan.direction, plan.order_type,
                    plan.entry_price, plan.stop_loss_price, plan.take_profit_price,
                    plan.take_profit_price_2, plan.valid_until, str(plan.status), plan.shadow_status,
                    plan.strategy_version, _json(plan.risk_snapshot), plan.created_at, _now(),
                ),
            )
            conn.execute(
                "INSERT INTO trade_events(plan_id,event_type,event_at,dataset,details_json) VALUES(?,?,?,?,?)",
                (plan.id, "created", plan.created_at, "plan", "{}"),
            )
        return plan.id

    def append_event(
        self,
        plan_id: str,
        event_type: str,
        *,
        dataset: str = "plan",
        event_at: str | None = None,
        price: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        self._require_available()
        with self._write_lock, self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO trade_events(plan_id,event_type,event_at,dataset,price,details_json) VALUES(?,?,?,?,?,?)",
                (plan_id, event_type, event_at or _now(), dataset, price, _json(details or {})),
            )
            return int(cur.lastrowid)

    def claim_lifecycle_bar(
        self, *, plan_id: str, timeframe: str, bar_closed_at: str
    ) -> bool:
        """Atomically claim one closed bar so restarts cannot process it twice."""
        self._require_available()
        with self._write_lock, self._connection() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO lifecycle_processed_bars(
                    plan_id,timeframe,bar_closed_at,processed_at
                ) VALUES(?,?,?,?)""",
                (plan_id, timeframe, bar_closed_at, _now()),
            )
        return cursor.rowcount > 0

    def update_plan(self, plan_id: str, **fields: Any) -> None:
        self._require_available()
        allowed = {
            "status", "shadow_status", "shadow_entry_price", "shadow_opened_at",
            "shadow_mfe", "shadow_mae", "shadow_holding_bars", "risk_snapshot_json",
            "shadow_active_stop", "shadow_highest_close", "shadow_time_exit_pending",
            "valid_until", "last_price", "last_bar_at",
            "actual_mfe", "actual_mae", "actual_holding_bars",
            "actual_active_stop", "actual_highest_close", "actual_time_exit_pending",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = _now()
        clause = ",".join(f"{key}=?" for key in updates)
        with self._write_lock, self._connection() as conn:
            conn.execute(f"UPDATE trade_plans SET {clause} WHERE id=?", (*updates.values(), plan_id))

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM trade_plans WHERE id=?", (plan_id,)).fetchone()
        return self._decode_plan(row) if row else None

    def list_plans(
        self,
        *,
        statuses: list[str] | None = None,
        shadow_open: bool = False,
        lifecycle_open: bool = False,
        symbol: str = "",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self._require_available()
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if shadow_open:
            where.append("shadow_status IN ('proposed','entry_touched','open','exit_detected')")
        if lifecycle_open:
            where.append(
                "(shadow_status IN ('proposed','entry_touched','open','exit_detected') "
                "OR status IN ('partially_filled','executed_open','exit_detected'))"
            )
        if symbol:
            where.append("symbol=?")
            params.append(symbol)
        sql = "SELECT * FROM trade_plans"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_plan(row) for row in rows]

    def confirm_execution(self, execution: Execution) -> str:
        self._require_available()
        with self._write_lock, self._connection() as conn:
            plan = conn.execute("SELECT * FROM trade_plans WHERE id=?", (execution.plan_id,)).fetchone()
            if plan is None:
                raise ValueError("trade plan not found")
            if plan["status"] != "proposed":
                raise ValueError(f"plan status {plan['status']} cannot be executed")
            if plan["asset_class"] == "cn_futures":
                from pa_agent.trading.profiles import is_continuous_futures_symbol

                if not execution.real_contract or is_continuous_futures_symbol(execution.real_contract):
                    raise ValueError("国内期货成交必须选择真实合约，不能使用主力连续合约")
                if not float(execution.quantity).is_integer():
                    raise ValueError("国内期货实际数量必须为整数手")
            conn.execute(
                """INSERT INTO executions(id,plan_id,executed_at,price,quantity,real_contract,fees,note,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    execution.id, execution.plan_id, execution.executed_at, execution.price,
                    execution.quantity, execution.real_contract, execution.fees, execution.note, _now(),
                ),
            )
            conn.execute(
                "UPDATE trade_plans SET status='executed_open',updated_at=? WHERE id=?",
                (_now(), execution.plan_id),
            )
            conn.execute(
                "INSERT INTO trade_events(plan_id,event_type,event_at,dataset,price,details_json) VALUES(?,?,?,?,?,?)",
                (
                    execution.plan_id, "executed", execution.executed_at, "actual", execution.price,
                    _json({"quantity": execution.quantity, "fees": execution.fees,
                           "real_contract": execution.real_contract, "note": execution.note}),
                ),
            )
        return execution.id

    def get_execution(self, plan_id: str) -> dict[str, Any] | None:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE plan_id=? ORDER BY executed_at DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        return dict(row) if row else None

    def ignore_plan(self, plan_id: str, reason: str) -> None:
        self.update_plan(plan_id, status="ignored")
        self.append_event(plan_id, "ignored", details={"reason": reason})

    def add_result(self, result: TradeResult) -> str:
        self._require_available()
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO trade_results(
                    id,plan_id,dataset,outcome,entry_price,exit_price,quantity,gross_pnl,net_pnl,
                    r_multiple,mfe_r,mae_r,holding_bars,ambiguous_same_bar,opened_at,closed_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(plan_id,dataset) DO UPDATE SET
                    outcome=excluded.outcome,entry_price=excluded.entry_price,
                    exit_price=excluded.exit_price,quantity=excluded.quantity,
                    gross_pnl=excluded.gross_pnl,net_pnl=excluded.net_pnl,
                    r_multiple=excluded.r_multiple,mfe_r=excluded.mfe_r,mae_r=excluded.mae_r,
                    holding_bars=excluded.holding_bars,
                    ambiguous_same_bar=excluded.ambiguous_same_bar,closed_at=excluded.closed_at""",
                (
                    result.id, result.plan_id, result.dataset, result.outcome, result.entry_price,
                    result.exit_price, result.quantity, result.gross_pnl, result.net_pnl,
                    result.r_multiple, result.mfe_r, result.mae_r, result.holding_bars,
                    int(result.ambiguous_same_bar), result.opened_at, result.closed_at, _now(),
                ),
            )
        return result.id

    def confirm_exit(
        self,
        plan_id: str,
        *,
        exited_at: str,
        exit_price: float,
        exit_fees: float = 0.0,
        note: str = "",
    ) -> str:
        self._require_available()
        with self._connection() as conn:
            plan = conn.execute("SELECT * FROM trade_plans WHERE id=?", (plan_id,)).fetchone()
            execution = conn.execute(
                "SELECT * FROM executions WHERE plan_id=? ORDER BY executed_at DESC LIMIT 1", (plan_id,)
            ).fetchone()
        if not plan or not execution:
            raise ValueError("open execution not found")
        direction_long = _is_long(plan["direction"])
        multiplier = 1.0
        profile = self.get_profile(execution["real_contract"] or plan["symbol"])
        if profile is None:
            profile = self.get_profile(plan["symbol"])
        if profile and profile.asset_class.value == "cn_futures" and profile.contract_multiplier:
            multiplier = profile.contract_multiplier
        signed_move = exit_price - execution["price"] if direction_long else execution["price"] - exit_price
        gross = signed_move * execution["quantity"] * multiplier
        net = gross - execution["fees"] - exit_fees
        risk_amount = abs(execution["price"] - plan["stop_loss_price"]) * execution["quantity"] * multiplier
        result = TradeResult(
            id=uuid.uuid4().hex, plan_id=plan_id, dataset="actual",
            outcome="win" if net > 0 else "loss" if net < 0 else "flat",
            entry_price=execution["price"], exit_price=exit_price, quantity=execution["quantity"],
            gross_pnl=gross, net_pnl=net, r_multiple=(net / risk_amount if risk_amount else None),
            mfe_r=(float(plan["actual_mfe"] or 0) / abs(execution["price"] - plan["stop_loss_price"]) if execution["price"] != plan["stop_loss_price"] else None),
            mae_r=(float(plan["actual_mae"] or 0) / abs(execution["price"] - plan["stop_loss_price"]) if execution["price"] != plan["stop_loss_price"] else None),
            holding_bars=int(plan["actual_holding_bars"] or 0),
            opened_at=execution["executed_at"], closed_at=exited_at,
        )
        self.add_result(result)
        self.update_plan(plan_id, status="closed")
        self.append_event(
            plan_id, "exit_confirmed", dataset="actual", event_at=exited_at, price=exit_price,
            details={"exit_fees": exit_fees, "note": note},
        )
        return result.id

    def upsert_profile(self, profile: InstrumentProfile) -> None:
        self._require_available()
        now = _now()
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO instrument_profiles(symbol,asset_class,profile_json,confirmed_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
                   asset_class=excluded.asset_class,profile_json=excluded.profile_json,updated_at=excluded.updated_at""",
                (profile.symbol, profile.asset_class.value, profile.model_dump_json(), now, now),
            )

    def get_profile(self, symbol: str) -> InstrumentProfile | None:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute("SELECT profile_json FROM instrument_profiles WHERE symbol=?", (symbol,)).fetchone()
        return InstrumentProfile.model_validate_json(row[0]) if row else None

    def list_events(self, plan_id: str) -> list[dict[str, Any]]:
        self._require_available()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_events WHERE plan_id=? ORDER BY event_at,id", (plan_id,)
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        self._require_available()
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM decision_events WHERE id=?", (decision_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in (
            "prompt_snapshot_json", "original_decision_json", "final_decision_json",
            "price_adjustments_json",
        ):
            result[key.removesuffix("_json")] = json.loads(result.pop(key) or "null")
        return result

    def list_results(self, *, dataset: Literal["shadow", "actual"] | None = None) -> list[dict[str, Any]]:
        self._require_available()
        params: tuple[Any, ...] = ()
        where = ""
        if dataset is not None:
            where = "WHERE r.dataset=?"
            params = (dataset,)
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT r.*,p.symbol,p.timeframe,p.asset_class,p.direction,p.order_type,
                           p.strategy_version,p.status,p.shadow_status
                    FROM trade_results r JOIN trade_plans p ON p.id=r.plan_id
                    {where} ORDER BY r.closed_at DESC""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_profiles(self) -> list[InstrumentProfile]:
        self._require_available()
        with self._connection() as conn:
            rows = conn.execute("SELECT profile_json FROM instrument_profiles ORDER BY symbol").fetchall()
        return [InstrumentProfile.model_validate_json(row[0]) for row in rows]

    def statistics(
        self,
        *,
        dataset: Literal["shadow", "actual"],
        asset_class: str = "",
        symbol: str = "",
        timeframe: str = "",
        market_state: str = "",
        order_type: str = "",
        strategy_version: str = "",
    ) -> dict[str, Any]:
        """Calculate non-mixed headline statistics for one explicit dataset."""
        self._require_available()
        filters = {
            "p.asset_class": asset_class, "p.symbol": symbol, "p.timeframe": timeframe,
            "d.market_state": market_state, "p.order_type": order_type,
            "p.strategy_version": strategy_version,
        }
        clauses = [f"{column}=?" for column, value in filters.items() if value]
        filter_values = [value for value in filters.values() if value]
        filter_sql = (" AND " + " AND ".join(clauses)) if clauses else ""
        plan_filter_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT r.* FROM trade_results r
                    JOIN trade_plans p ON p.id=r.plan_id
                    JOIN decision_events d ON d.id=p.decision_event_id
                    WHERE r.dataset=? {filter_sql} ORDER BY r.closed_at""",
                (dataset, *filter_values),
            ).fetchall()
            signal_count = conn.execute(
                f"""SELECT COUNT(*) FROM trade_plans p
                    JOIN decision_events d ON d.id=p.decision_event_id {plan_filter_sql}""",
                filter_values,
            ).fetchone()[0]
            touched_count = conn.execute(
                f"""SELECT COUNT(DISTINCT e.plan_id) FROM trade_events e
                    JOIN trade_plans p ON p.id=e.plan_id
                    JOIN decision_events d ON d.id=p.decision_event_id
                    WHERE e.event_type='entry_touched' AND e.dataset='shadow' {filter_sql}""",
                filter_values,
            ).fetchone()[0]
            ignored_count = conn.execute(
                f"""SELECT COUNT(*) FROM trade_plans p
                    JOIN decision_events d ON d.id=p.decision_event_id
                    WHERE p.status='ignored' {filter_sql}""",
                filter_values,
            ).fetchone()[0]
        values = [dict(row) for row in rows]
        wins = [r for r in values if (r["net_pnl"] if r["net_pnl"] is not None else r["gross_pnl"] or 0) > 0]
        losses = [r for r in values if (r["net_pnl"] if r["net_pnl"] is not None else r["gross_pnl"] or 0) < 0]
        r_values = [float(r["r_multiple"]) for r in values if r["r_multiple"] is not None]
        pnl_values = [float(r["net_pnl"] or 0) for r in values]
        peak = equity_curve = drawdown = 0.0
        loss_streak = max_loss_streak = 0
        for pnl in pnl_values:
            equity_curve += pnl
            peak = max(peak, equity_curve)
            drawdown = max(drawdown, peak - equity_curve)
            if pnl < 0:
                loss_streak += 1
                max_loss_streak = max(max_loss_streak, loss_streak)
            else:
                loss_streak = 0
        gross_profit = sum(max(0.0, pnl) for pnl in pnl_values)
        gross_loss = abs(sum(min(0.0, pnl) for pnl in pnl_values))
        return {
            "dataset": dataset,
            "signal_count": signal_count,
            "result_count": len(values),
            "trigger_rate": touched_count / signal_count if signal_count else None,
            "ignore_rate": ignored_count / signal_count if signal_count else None,
            "win_rate": len(wins) / len(values) if values else None,
            "average_win_r": _avg([float(r["r_multiple"]) for r in wins if r["r_multiple"] is not None]),
            "average_loss_r": _avg([float(r["r_multiple"]) for r in losses if r["r_multiple"] is not None]),
            "net_expectancy_r": _avg(r_values),
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "max_drawdown": drawdown,
            "max_consecutive_losses": max_loss_streak,
            "average_mfe_r": _avg([float(r["mfe_r"]) for r in values if r["mfe_r"] is not None]),
            "average_mae_r": _avg([float(r["mae_r"]) for r in values if r["mae_r"] is not None]),
            "average_holding_bars": _avg([float(r["holding_bars"]) for r in values if r["holding_bars"] is not None]),
        }

    def export_csv(
        self,
        output: Path,
        *,
        dataset: Literal["shadow", "actual"],
        asset_class: str = "",
        symbol: str = "",
        timeframe: str = "",
        market_state: str = "",
        order_type: str = "",
        strategy_version: str = "",
    ) -> Path:
        self._require_available()
        output.parent.mkdir(parents=True, exist_ok=True)
        filters = {
            "p.asset_class": asset_class, "p.symbol": symbol, "p.timeframe": timeframe,
            "d.market_state": market_state, "p.order_type": order_type,
            "p.strategy_version": strategy_version,
        }
        clauses = [f"{column}=?" for column, value in filters.items() if value]
        filter_values = [value for value in filters.values() if value]
        filter_sql = (" AND " + " AND ".join(clauses)) if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT p.symbol,p.timeframe,p.asset_class,p.direction,p.order_type,p.entry_price AS planned_entry,
                          p.stop_loss_price,p.take_profit_price,p.strategy_version,r.*
                   FROM trade_results r JOIN trade_plans p ON p.id=r.plan_id
                   JOIN decision_events d ON d.id=p.decision_event_id
                   WHERE r.dataset=? """ + filter_sql + " ORDER BY r.closed_at",
                (dataset, *filter_values),
            ).fetchall()
        fieldnames = list(rows[0].keys()) if rows else ["dataset"]
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        return output

    def import_legacy_csvs(self) -> int:
        """Idempotently import old snapshots without inferring executions or outcomes."""
        if not self.available or not self.legacy_dir.exists():
            return 0
        imported = 0
        for path in sorted(self.legacy_dir.glob("*.csv")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self._connection() as conn:
                if conn.execute("SELECT 1 FROM legacy_imports WHERE file_hash=?", (digest,)).fetchone():
                    continue
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                for index, row in enumerate(rows):
                    self._import_legacy_row(row, path, digest, index)
                with self._write_lock, self._connection() as conn:
                    conn.execute(
                        "INSERT INTO legacy_imports(file_hash,source_path,imported_at,row_count) VALUES(?,?,?,?)",
                        (digest, str(path), _now(), len(rows)),
                    )
                imported += len(rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Legacy trade CSV import failed for %s: %s", path, exc)
        return imported

    def _import_legacy_row(self, row: dict[str, str], path: Path, digest: str, index: int) -> None:
        from pa_agent.trading.models import PlanStatus
        from pa_agent.trading.profiles import infer_asset_class

        symbol = row.get("symbol", "")
        direction = row.get("order_direction", "")
        try:
            entry = float(row.get("entry_price") or 0)
            stop = float(row.get("stop_loss_price") or 0)
            target = float(row.get("take_profit_price") or 0)
        except ValueError:
            return
        if not entry or not stop or not target:
            return
        decision_id = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy:{digest}:{index}:decision").hex
        plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy:{digest}:{index}:plan").hex
        asset = infer_asset_class(symbol)
        self.add_decision(
            decision_id=decision_id, symbol=symbol, timeframe=row.get("timeframe", ""),
            asset_class=asset.value, original_decision=row, final_decision=row,
            meta={"strategy_version": "legacy_import"},
            analysis_record_ref=f"legacy_csv:{path}", audit_reason="legacy_import; actual result unknown",
        )
        self.add_plan(TradePlan(
            id=plan_id, decision_event_id=decision_id, analysis_record_ref=f"legacy_csv:{path}",
            symbol=symbol, timeframe=row.get("timeframe", ""), asset_class=asset,
            direction=direction, order_type=row.get("order_type", "legacy_import"),
            entry_price=entry, stop_loss_price=stop, take_profit_price=target,
            take_profit_price_2=_float_or_none(row.get("take_profit_price_2")),
            status=PlanStatus.PROPOSED, shadow_status="unknown", strategy_version="legacy_import",
            created_at=row.get("record_time") or _now(),
        ))

    @staticmethod
    def _decode_plan(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["risk_snapshot"] = json.loads(result.pop("risk_snapshot_json") or "{}")
        return result

    def _require_available(self) -> None:
        if not self.available:
            raise RuntimeError(self.error or "trading database unavailable")


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_long(direction: Any) -> bool:
    text = str(direction or "").strip().lower()
    return "多" in text or text in {"long", "buy", "bull"}
