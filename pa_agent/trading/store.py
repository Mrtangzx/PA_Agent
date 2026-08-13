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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pa_agent.trading.models import Execution, InstrumentProfile, TradePlan, TradeResult

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 12


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
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    broker_order_id TEXT NOT NULL,
                    broker_fill_ids_json TEXT NOT NULL DEFAULT '[]',
                    match_status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(plan_id, broker_order_id)
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
                    broker_fill_id TEXT PRIMARY KEY,
                    account_fingerprint TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    fees REAL NOT NULL DEFAULT 0,
                    filled_at TEXT NOT NULL,
                    fill_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS lifecycle_processed_bars (
                    plan_id TEXT NOT NULL REFERENCES trade_plans(id),
                    timeframe TEXT NOT NULL,
                    bar_closed_at TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY(plan_id,timeframe,bar_closed_at)
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
                """
            )
            plan_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(trade_plans)").fetchall()
            }
            if "last_price" not in plan_columns:
                conn.execute("ALTER TABLE trade_plans ADD COLUMN last_price REAL")
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
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    def health(self) -> dict[str, Any]:
        return {"available": self.available, "error": self.error, "path": str(self.db_path)}

    def upsert_universe_snapshot(
        self,
        snapshot: Any,
        *,
        source_updated_at: str = "",
        data_complete: bool = True,
    ) -> str:
        self._require_available()
        payload = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
        version = str(payload.get("version", ""))
        if not version:
            raise ValueError("universe version is required")
        with self._write_lock, self._connection() as conn:
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

    def latest_topdown_score(self, symbol: str = "") -> dict[str, Any] | None:
        self._require_available()
        where = "WHERE symbol=?" if symbol else ""
        params: tuple[Any, ...] = (symbol,) if symbol else ()
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

    def list_topdown_scores(self, *, symbol: str = "", limit: int = 500) -> list[dict[str, Any]]:
        self._require_available()
        where = "WHERE symbol=?" if symbol else ""
        params: tuple[Any, ...] = (symbol, limit) if symbol else (limit,)
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

    def link_broker_order(self, reconciliation: Any, *, details: dict[str, Any] | None = None) -> str:
        self._require_available()
        payload = reconciliation.model_dump(mode="json") if hasattr(reconciliation, "model_dump") else dict(reconciliation)
        orders = list(payload.get("matched_order_ids") or [])
        if len(orders) != 1:
            raise ValueError("a unique broker order is required")
        link_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO broker_order_links(
                    id,plan_id,broker_order_id,broker_fill_ids_json,match_status,details_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(plan_id,broker_order_id) DO UPDATE SET
                    broker_fill_ids_json=excluded.broker_fill_ids_json,
                    match_status=excluded.match_status,details_json=excluded.details_json,
                    updated_at=excluded.updated_at""",
                (
                    link_id, str(payload.get("plan_id", "")), orders[0],
                    _json(payload.get("matched_fill_ids", [])), str(payload.get("status", "")),
                    _json(details or {}), _now(), _now(),
                ),
            )
        return link_id

    def list_broker_order_links(self) -> list[dict[str, Any]]:
        """Return decoded broker links used to prevent duplicate/manual fill attribution."""
        self._require_available()
        with self._connection() as conn:
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

    def linked_broker_fill_ids(self) -> set[str]:
        return {
            str(fill_id)
            for link in self.list_broker_order_links()
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
        with self._write_lock, self._connection() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO external_broker_trades(
                    broker_fill_id,account_fingerprint,broker_order_id,symbol,direction,price,
                    quantity,fees,filled_at,fill_json,first_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fill_id, account_fingerprint, str(payload.get("broker_order_id", "")),
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
    ) -> None:
        """Apply real broker fills idempotently; broker data remains the source of truth."""
        self._require_available()
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
            if quantity > 0:
                average_price = total_value / quantity
                conn.execute(
                    """INSERT INTO executions(
                        id,plan_id,executed_at,price,quantity,real_contract,fees,note,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET executed_at=excluded.executed_at,
                        price=excluded.price,quantity=excluded.quantity,fees=excluded.fees,
                        note=excluded.note""",
                    (
                        execution_id, plan_id, executed_at, average_price, quantity, "", fees,
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
