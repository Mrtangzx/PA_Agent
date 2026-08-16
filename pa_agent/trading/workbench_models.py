"""Consistent read models for the stock-pool-driven quant workbench.

The GUI must never assemble a selected stock from unrelated "latest" rows.
This module owns one selected-symbol context and projects every panel from the
same persisted revision.  It remains read-only with respect to trading facts;
personal watchlist and UI-preference mutations are explicit repository calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from pa_agent.data.ashare_common import ashare_session_open
from pa_agent.trading.stock_sandbox import StockSandboxState
from pa_agent.trading.topdown import (
    MANUAL_EXCEPTION_STRATEGY_ID,
    TOPDOWN_SCORING_VERSION,
    TOPDOWN_STRATEGY_ID,
)

OPERATIONAL_STRATEGIES = {TOPDOWN_STRATEGY_ID, MANUAL_EXCEPTION_STRATEGY_ID}


@dataclass(slots=True)
class PoolRowViewModel:
    symbol: str
    name: str
    membership: str
    in_system_pool: bool
    in_watchlist: bool
    in_ths_watchlist: bool
    in_personal_watchlist: bool
    forced_tracking: bool
    state: str
    state_label: str
    action: str
    action_priority: int
    latest_price: float | None = None
    pct_change: float | None = None
    total_score: float | None = None
    consecutive_pass_count: int = 0
    plan_id: str | None = None
    plan_status: str = "none"
    observed_at: str = ""
    primary_issue: str = ""
    categories: list[str] = field(default_factory=list)
    scan_reason: str = ""


@dataclass(slots=True)
class SelectedStockContext:
    symbol: str | None = None
    name: str = ""
    pool_version: str | None = None
    membership: str = "none"
    sandbox: dict[str, Any] | None = None
    daily_signal: dict[str, Any] | None = None
    score: dict[str, Any] | None = None
    previous_score: dict[str, Any] | None = None
    hotspot: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    risk: dict[str, Any] | None = None
    broker: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    reconciliation: dict[str, Any] | None = None
    daily_bars: list[dict[str, Any]] = field(default_factory=list)
    intraday_bars: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 0


@dataclass(slots=True)
class GlobalHealth:
    market_session: str = "已收盘"
    data_status: str = "待检查"
    pool_version: str = "尚未生成"
    strategy_state: str = "CANDIDATE"
    mode: str = "影子观察"
    broker_status: str = "未连接"
    feishu_status: str = "未配置"
    last_sync: str = "尚未同步"
    issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AccountSummary:
    total_equity: float | None = None
    available_cash: float | None = None
    position_value: float | None = None
    daily_pnl: float | None = None
    positions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    captured_at: str = ""
    complete: bool = False


@dataclass(slots=True)
class ValidationSummary:
    strategy_state: str = "candidate"
    validation_runs: int = 0
    current_epoch: dict[str, Any] | None = None
    blockers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QuantWorkbenchViewModel:
    global_health: GlobalHealth
    selected: SelectedStockContext
    pool_rows: list[PoolRowViewModel]
    action_counts: dict[str, int]
    account_summary: AccountSummary
    validation_summary: ValidationSummary
    selection_snapshot: dict[str, Any] | None = None


class SelectedStockContextController(QObject):
    """Single selection owner shared by monitor, account and validation views."""

    symbol_changed = pyqtSignal(str)
    context_changed = pyqtSignal(object)
    view_model_changed = pyqtSignal(object)
    context_error = pyqtSignal(str, str)

    def __init__(self, ctx: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = ctx.trade_store
        self._symbol = ""
        self._revision = 0
        self._last_view_model: QuantWorkbenchViewModel | None = None
        if self.store is not None and self.store.available:
            self._symbol = str(self.store.get_workbench_preference("selected_symbol", "") or "")

    @property
    def selected_symbol(self) -> str:
        return self._symbol

    @property
    def view_model(self) -> QuantWorkbenchViewModel | None:
        return self._last_view_model

    def select_symbol(self, symbol: str, *, persist: bool = True) -> None:
        code = str(symbol or "").strip()[-6:]
        if not code or code == self._symbol:
            if code:
                self.reload(scope="selected", symbol=code)
            return
        self._symbol = code
        self._revision += 1
        if persist and self.store is not None and self.store.available:
            self.store.save_workbench_preference("selected_symbol", code)
        self.symbol_changed.emit(code)
        self.reload(scope="selected", symbol=code)

    def reload(self, *, scope: str = "all", symbol: str | None = None) -> None:
        if self.store is None or not self.store.available:
            message = getattr(self.store, "error", "交易数据库不可用")
            self.context_error.emit(symbol or self._symbol, message)
            return
        try:
            rows, universe_record = self._pool_rows()
            available = {item.symbol for item in rows}
            if symbol and symbol in available:
                self._symbol = symbol
            if self._symbol not in available:
                self._symbol = rows[0].symbol if rows else ""
                if self._symbol:
                    self.store.save_workbench_preference("selected_symbol", self._symbol)
                    self.symbol_changed.emit(self._symbol)
            self._revision += 1
            selected = self._selected_context(
                self._symbol,
                rows,
                universe_record,
                revision=self._revision,
            )
            view_model = QuantWorkbenchViewModel(
                global_health=self._global_health(universe_record),
                selected=selected,
                pool_rows=rows,
                action_counts=self._action_counts(rows),
                account_summary=self._account_summary(),
                validation_summary=self._validation_summary(),
                selection_snapshot=self._selection_snapshot(),
            )
            self._last_view_model = view_model
            self.context_changed.emit(selected)
            self.view_model_changed.emit(view_model)
        except Exception as exc:  # noqa: BLE001
            self.context_error.emit(symbol or self._symbol, str(exc))

    def _pool_rows(
        self,
    ) -> tuple[list[PoolRowViewModel], dict[str, Any] | None]:
        universes = self.store.list_universe_snapshots(limit=1)
        universe_record = universes[0] if universes else None
        universe = dict((universe_record or {}).get("snapshot") or {})
        pool_version = str(universe.get("version") or "")
        system_symbols = {str(item) for item in universe.get("symbols") or [] if str(item)}
        member_by_symbol = {
            str(item.get("symbol") or ""): dict(item) for item in universe.get("members") or []
        }
        watchlist = {
            item["symbol"]: item for item in self.store.list_watchlist_members(active_only=True)
        }
        selection = self._selection_snapshot() or {}
        selection_by_symbol = {
            str(item.get("symbol") or ""): dict(item)
            for item in selection.get("candidates") or []
            if str(item.get("symbol") or "")
        }
        broker = self._broker_payload()
        forced_symbols = {
            str(item.get("symbol") or "")
            for item in broker.get("positions") or []
            if str(item.get("symbol") or "")
        }
        forced_symbols.update(
            str(item.get("symbol") or "")
            for item in self.store.list_plans(lifecycle_open=True, limit=2000)
            if str(item.get("symbol") or "")
        )
        sandbox_by_symbol = {
            str(item.get("symbol") or ""): dict(item.get("snapshot") or {})
            for item in self.store.list_stock_sandboxes(pool_version=pool_version, limit=2000)
        }
        symbols = system_symbols | set(watchlist) | forced_symbols | set(selection_by_symbol)
        rows: list[PoolRowViewModel] = []
        for code in symbols:
            watch = watchlist.get(code) or {}
            metadata = dict(watch.get("metadata") or {})
            active_sources = {
                str(item.get("source") or "")
                for item in watch.get("sources") or []
                if item.get("active")
            }
            source_by_name = {
                str(item.get("source") or ""): item
                for item in watch.get("sources") or []
                if item.get("active")
            }
            in_ths_watchlist = "ths_watchlist" in active_sources
            in_personal_watchlist = bool(
                active_sources & {"user", "user_watchlist"}
            )
            member = member_by_symbol.get(code) or metadata
            selection_item = selection_by_symbol.get(code) or {}
            if not member:
                member = selection_item
            sandbox = sandbox_by_symbol.get(code) or self._fallback_sandbox(
                code,
                str(watch.get("name") or member.get("name") or code),
                pool_version,
                outside_pool=code not in system_symbols,
            )
            forced = code in forced_symbols
            watched = code in watchlist
            ths_metadata = dict(
                (source_by_name.get("ths_watchlist") or {}).get("metadata") or {}
            )
            categories = [str(item) for item in ths_metadata.get("ths_categories") or []]
            if not categories:
                categories = [str(item) for item in selection_item.get("themes") or []]
            if code in system_symbols and in_ths_watchlist:
                membership = "系统策略池 · 同花顺自选"
            elif code in system_symbols and watched:
                membership = "系统池 · 已关注"
            elif code in system_symbols:
                membership = "系统策略池"
            elif in_ths_watchlist:
                membership = "同花顺自选 · 池外例外"
            elif forced:
                membership = "强制跟踪"
            elif selection_item:
                membership = "智能选股 · 池外观察"
            else:
                membership = "池外观察"
            issues = list(sandbox.get("hard_blocks") or []) or list(sandbox.get("data_gaps") or [])
            latest_price = sandbox.get("latest_price")
            if latest_price is None:
                latest_price = member.get("latest_price")
            rows.append(
                PoolRowViewModel(
                    symbol=code,
                    name=str(
                        sandbox.get("name") or watch.get("name") or member.get("name") or code
                    ),
                    membership=membership,
                    in_system_pool=code in system_symbols,
                    in_watchlist=watched,
                    in_ths_watchlist=in_ths_watchlist,
                    in_personal_watchlist=in_personal_watchlist,
                    forced_tracking=forced,
                    state=str(sandbox.get("state") or "analysis_only"),
                    state_label=str(sandbox.get("state_label") or "池外观察"),
                    action=str(sandbox.get("action") or "等待确定性数据"),
                    action_priority=int(sandbox.get("action_priority") or 70),
                    latest_price=_float_or_none(latest_price),
                    pct_change=_float_or_none(member.get("latest_pct_chg")),
                    total_score=_float_or_none(sandbox.get("total_score")),
                    consecutive_pass_count=int(sandbox.get("consecutive_pass_count") or 0),
                    plan_id=(str(sandbox.get("plan_id")) if sandbox.get("plan_id") else None),
                    plan_status=str(sandbox.get("plan_status") or "none"),
                    observed_at=str(sandbox.get("observed_at") or ""),
                    primary_issue=(
                        str(issues[0])
                        if issues else str(ths_metadata.get("ths_reason") or "")
                    ),
                    categories=categories,
                    scan_reason=(
                        str(ths_metadata.get("ths_reason") or "")
                        or "、".join(
                            str(item) for item in selection_item.get("strategy_tags") or []
                        )
                    ),
                )
            )
        return sorted(rows, key=lambda row: (row.action_priority, row.symbol)), universe_record

    @staticmethod
    def _fallback_sandbox(
        symbol: str,
        name: str,
        pool_version: str,
        *,
        outside_pool: bool,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "name": name,
            "pool_version": pool_version,
            "state": StockSandboxState.ANALYSIS_ONLY.value,
            "state_label": "池外观察" if outside_pool else "等待沙箱刷新",
            "action": ("等待池外例外评估" if outside_pool else "等待量化后台生成状态"),
            "action_priority": 70 if outside_pool else 80,
            "hard_blocks": [],
            "data_gaps": ["outside_system_pool"] if outside_pool else ["sandbox_pending"],
            "plan_status": "none",
            "observed_at": "",
        }

    def _selected_context(
        self,
        symbol: str,
        rows: list[PoolRowViewModel],
        universe_record: dict[str, Any] | None,
        *,
        revision: int,
    ) -> SelectedStockContext:
        if not symbol:
            return SelectedStockContext(revision=revision)
        row = next((item for item in rows if item.symbol == symbol), None)
        universe = dict((universe_record or {}).get("snapshot") or {})
        pool_version = str(universe.get("version") or "")
        sandbox_record = next(
            (
                item
                for item in self.store.list_stock_sandboxes(pool_version=pool_version, limit=2000)
                if str(item.get("symbol") or "") == symbol
            ),
            None,
        )
        sandbox = dict((sandbox_record or {}).get("snapshot") or {})
        if not sandbox and row is not None:
            sandbox = self._fallback_sandbox(
                symbol, row.name, pool_version, outside_pool=not row.in_system_pool
            )
        signals = self.store.list_quant_signals(limit=2000)
        daily_signal = next(
            (item for item in signals if str(item.get("symbol") or "") == symbol),
            None,
        )
        score_rows = self.store.list_topdown_scores(
            symbol=symbol,
            strategy_version=TOPDOWN_STRATEGY_ID,
            scoring_version=TOPDOWN_SCORING_VERSION,
            limit=2,
        )
        score = dict(score_rows[0].get("snapshot") or {}) if score_rows else None
        previous_score = dict(score_rows[1].get("snapshot") or {}) if len(score_rows) > 1 else None
        hotspot_record = self.store.latest_hotspot_snapshot(symbol)
        hotspot = dict((hotspot_record or {}).get("snapshot") or {}) or None
        plans = [
            item
            for item in self.store.list_plans(symbol=symbol, limit=100)
            if str(item.get("strategy_version") or "") in OPERATIONAL_STRATEGIES
        ]
        plan = plans[0] if plans else None
        broker = self._broker_payload()
        position = next(
            (
                item
                for item in broker.get("positions") or []
                if str(item.get("symbol") or "") == symbol
            ),
            None,
        )
        reconciliation = None
        if plan:
            links = self.store.list_broker_order_links()
            reconciliation = next(
                (item for item in links if item.get("plan_id") == plan.get("id")),
                None,
            )
        return SelectedStockContext(
            symbol=symbol,
            name=row.name if row else symbol,
            pool_version=pool_version or None,
            membership=row.membership if row else "none",
            sandbox=sandbox or None,
            daily_signal=daily_signal,
            score=score,
            previous_score=previous_score,
            hotspot=hotspot,
            plan=plan,
            risk=dict((plan or {}).get("risk_snapshot") or {}) or None,
            broker=broker or None,
            position=position,
            reconciliation=reconciliation,
            daily_bars=self._bars(symbol, kind="daily_bars", limit=80),
            intraday_bars=self._bars(symbol, kind="intraday_15m", limit=96),
            revision=revision,
        )

    def _bars(self, symbol: str, *, kind: str, limit: int) -> list[dict[str, Any]]:
        observations = self.store.list_oos_observations(
            kind=kind,
            symbol=symbol,
            limit=limit,
            descending=True,
        )
        rows: list[dict[str, Any]] = []
        for item in reversed(observations):
            payload = dict(item.get("payload") or {})
            payload.setdefault("time", item.get("effective_at"))
            rows.append(payload)
        return rows

    def _broker_payload(self) -> dict[str, Any]:
        runtime = getattr(self.ctx, "quant_runtime", None)
        snapshot = getattr(runtime, "broker_snapshot", None) if runtime else None
        if snapshot is not None:
            return (
                snapshot.model_dump(mode="json")
                if hasattr(snapshot, "model_dump")
                else dict(snapshot)
            )
        record = self.store.latest_broker_snapshot()
        return dict((record or {}).get("snapshot") or {})

    def _global_health(self, universe_record: dict[str, Any] | None) -> GlobalHealth:
        universe = dict((universe_record or {}).get("snapshot") or {})
        complete = bool((universe_record or {}).get("data_complete"))
        runtime = getattr(self.ctx, "quant_runtime", None)
        broker = self._broker_payload()
        connection = dict(broker.get("connection") or {})
        broker_status = str(connection.get("status") or "disconnected")
        feishu = getattr(self.ctx.settings, "feishu", None)
        feishu_ready = bool(
            feishu
            and getattr(feishu, "enabled", True)
            and str(getattr(feishu, "webhook_url", "") or "").strip()
        )
        state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        issues: list[str] = []
        if not complete:
            issues.append("股票池数据不完整")
        if runtime is None or not bool(getattr(runtime, "started", False)):
            issues.append("量化后台未运行")
        if not broker.get("complete"):
            issues.append("同花顺账户事实未完整核验")
        return GlobalHealth(
            market_session="交易中" if ashare_session_open() else "已收盘",
            data_status="完整" if complete else "数据不完整",
            pool_version=str(universe.get("version") or "尚未生成"),
            strategy_state=state.upper(),
            mode=(
                "小资金实盘"
                if state in {"active", "reduced"}
                and bool(self.ctx.settings.portfolio_risk.live_trading_enabled)
                else "影子观察"
            ),
            broker_status=_broker_status_label(broker_status, bool(broker.get("complete"))),
            feishu_status="已启用" if feishu_ready else "未配置",
            last_sync=str(broker.get("captured_at") or "尚未同步"),
            issues=issues,
        )

    def _account_summary(self) -> AccountSummary:
        broker = self._broker_payload()
        return AccountSummary(
            total_equity=_float_or_none(broker.get("total_equity")),
            available_cash=_float_or_none(broker.get("available_cash")),
            position_value=_float_or_none(broker.get("position_value")),
            daily_pnl=_float_or_none(broker.get("daily_pnl")),
            positions=list(broker.get("positions") or []),
            orders=list(broker.get("orders") or []),
            fills=list(broker.get("fills") or []),
            captured_at=str(broker.get("captured_at") or ""),
            complete=bool(broker.get("complete")),
        )

    def _validation_summary(self) -> ValidationSummary:
        state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        runs = self.store.list_validation_runs(strategy_version=TOPDOWN_STRATEGY_ID, limit=500)
        epoch_record = self.store.current_validation_epoch(strategy_version=TOPDOWN_STRATEGY_ID)
        epoch = dict((epoch_record or {}).get("epoch") or {}) or None
        blockers: list[str] = []
        if state not in {"active", "reduced"}:
            blockers.append("策略尚未达到真实交易晋级门槛")
        if not runs:
            blockers.append("尚无可用于晋级的验证记录")
        return ValidationSummary(
            strategy_state=state,
            validation_runs=len(runs),
            current_epoch=epoch,
            blockers=blockers,
        )

    def _selection_snapshot(self) -> dict[str, Any] | None:
        record = self.store.latest_stock_selection_snapshot()
        return dict((record or {}).get("snapshot") or {}) or None

    @staticmethod
    def _action_counts(rows: list[PoolRowViewModel]) -> dict[str, int]:
        counts = {
            "all": len(rows),
            "candidate": 0,
            "tradeable": 0,
            "position": 0,
            "exit": 0,
            "risk": 0,
        }
        for row in rows:
            if row.state in {"intraday_observing", "wait_confirmation"}:
                counts["candidate"] += 1
            if row.state in {"quant_tradeable", "authorized", "waiting_user_confirmation"}:
                counts["tradeable"] += 1
            if row.state in {"filled", "partially_filled"}:
                counts["position"] += 1
            if row.state == "exit_required":
                counts["exit"] += 1
            if row.state in {
                "major_risk_blocked",
                "account_risk_blocked",
                "invalidated",
                "data_incomplete",
            }:
                counts["risk"] += 1
        return counts


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _broker_status_label(status: str, complete: bool) -> str:
    if status == "connected_read_only" and complete:
        return "只读同步正常"
    return {
        "connected_read_only": "账户事实待核验",
        "login_required": "等待登录",
        "account_mismatch": "账户不匹配",
        "adapter_incompatible": "适配器需校准",
        "blocked_by_modal": "被弹窗阻断",
        "stale": "数据已过期",
        "error": "连接异常",
        "disconnected": "未连接",
    }.get(status, status or "未连接")
