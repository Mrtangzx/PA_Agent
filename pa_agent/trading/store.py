"""Versioned SQLite source of truth for decisions, plans and trade outcomes."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

from pa_agent.trading.models import Execution, InstrumentProfile, TradePlan, TradeResult

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 3


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
                    actual_mfe REAL NOT NULL DEFAULT 0,
                    actual_mae REAL NOT NULL DEFAULT 0,
                    actual_holding_bars INTEGER NOT NULL DEFAULT 0,
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
                CREATE INDEX IF NOT EXISTS idx_decision_symbol_time ON decision_events(symbol, created_at);
                CREATE INDEX IF NOT EXISTS idx_plans_status ON trade_plans(status, shadow_status);
                CREATE INDEX IF NOT EXISTS idx_events_plan_time ON trade_events(plan_id, event_at);
                CREATE INDEX IF NOT EXISTS idx_results_dataset ON trade_results(dataset, closed_at);
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
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    def health(self) -> dict[str, Any]:
        return {"available": self.available, "error": self.error, "path": str(self.db_path)}

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

    def update_plan(self, plan_id: str, **fields: Any) -> None:
        self._require_available()
        allowed = {
            "status", "shadow_status", "shadow_entry_price", "shadow_opened_at",
            "shadow_mfe", "shadow_mae", "shadow_holding_bars", "risk_snapshot_json",
            "valid_until", "last_price", "last_bar_at",
            "actual_mfe", "actual_mae", "actual_holding_bars",
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
                "OR status IN ('executed_open','exit_detected'))"
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
