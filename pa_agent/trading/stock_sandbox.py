"""Deterministic per-symbol trading-state projection for the A-share pool.

The sandbox is a read model.  It does not fetch data, call an LLM, authorize an
order, or mutate another symbol's state.  Every row is projected independently
from frozen universe, daily-signal, 15-minute score, hotspot and plan facts.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.trading.topdown import (
    MANUAL_EXCEPTION_STRATEGY_ID,
    TOPDOWN_STRATEGY_ID,
)


class StockSandboxState(StrEnum):
    ANALYSIS_ONLY = "analysis_only"
    DATA_INCOMPLETE = "data_incomplete"
    DAILY_OBSERVING = "daily_observing"
    DAILY_REJECTED = "daily_rejected"
    MAJOR_RISK_BLOCKED = "major_risk_blocked"
    INTRADAY_OBSERVING = "intraday_observing"
    WAIT_CONFIRMATION = "wait_confirmation"
    QUANT_TRADEABLE = "quant_tradeable"
    ACCOUNT_RISK_BLOCKED = "account_risk_blocked"
    AUTHORIZED = "authorized"
    WAITING_USER_CONFIRMATION = "waiting_user_confirmation"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    EXIT_REQUIRED = "exit_required"
    INVALIDATED = "invalidated"


STATE_LABELS: dict[StockSandboxState, str] = {
    StockSandboxState.ANALYSIS_ONLY: "只分析",
    StockSandboxState.DATA_INCOMPLETE: "数据待补齐",
    StockSandboxState.DAILY_OBSERVING: "等待日线扫描",
    StockSandboxState.DAILY_REJECTED: "日线未通过",
    StockSandboxState.MAJOR_RISK_BLOCKED: "重大风险阻断",
    StockSandboxState.INTRADAY_OBSERVING: "盘中观察",
    StockSandboxState.WAIT_CONFIRMATION: "等待连续确认",
    StockSandboxState.QUANT_TRADEABLE: "量化可交易",
    StockSandboxState.ACCOUNT_RISK_BLOCKED: "账户风控阻断",
    StockSandboxState.AUTHORIZED: "已通过账户风控",
    StockSandboxState.WAITING_USER_CONFIRMATION: "等待同花顺确认",
    StockSandboxState.SUBMITTED: "已委托",
    StockSandboxState.PARTIALLY_FILLED: "部分成交",
    StockSandboxState.FILLED: "持仓管理",
    StockSandboxState.EXIT_REQUIRED: "需要退出",
    StockSandboxState.INVALIDATED: "计划已失效",
}


class StockTradingSandboxSnapshot(BaseModel):
    symbol: str
    name: str
    pool_version: str
    observed_at: str
    market_session: str
    state: StockSandboxState
    state_label: str
    daily_status: str
    score_status: str
    index_score: float | None = None
    sentiment_score: float | None = None
    theme_score: float | None = None
    stock_score: float | None = None
    total_score: float | None = None
    consecutive_pass_count: int = 0
    hotspot_status: str
    hotspot_title: str = ""
    hard_blocks: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    plan_id: str | None = None
    plan_status: str = "none"
    account_risk_status: str = "not_evaluated"
    broker_status: str = "not_connected"
    trigger_price: float | None = None
    max_entry_price: float | None = None
    initial_stop: float | None = None
    valid_until: str = ""
    latest_price: float | None = None
    action: str
    action_priority: int
    input_hash: str

    @property
    def tradeable(self) -> bool:
        return self.state is StockSandboxState.QUANT_TRADEABLE


def project_stock_sandboxes(
    *,
    universe: dict[str, Any],
    signals: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    hotspots: dict[str, dict[str, Any]],
    latest_prices: dict[str, float] | None = None,
    broker_snapshot: Any = None,
    observed_at: str | None = None,
) -> list[StockTradingSandboxSnapshot]:
    """Project one isolated state row for every current pool member."""
    observed = observed_at or datetime.now().astimezone().isoformat()
    pool_version = str(universe.get("version") or "")
    symbols = [str(item) for item in universe.get("symbols") or [] if str(item)]
    members = {
        str(item.get("symbol") or ""): item
        for item in universe.get("members") or []
    }
    signal_by_symbol = _latest_daily_signals(signals, pool_version)
    score_by_symbol = _latest_by_symbol(scores, payload_key="snapshot")
    plan_by_symbol = _latest_plans(plans, pool_version)
    broker_status = _broker_status(broker_snapshot)
    market_session = _market_session(observed)
    current_prices = latest_prices or {}

    result: list[StockTradingSandboxSnapshot] = []
    for symbol in symbols:
        member = members.get(symbol) or {"symbol": symbol, "name": symbol}
        signal_record = signal_by_symbol.get(symbol) or {}
        signal = signal_record.get("decision") or {}
        score = score_by_symbol.get(symbol) or {}
        plan = plan_by_symbol.get(symbol) or {}
        hotspot_record = hotspots.get(symbol) or {}
        hotspot = hotspot_record.get("snapshot") or hotspot_record
        result.append(_project_one(
            symbol=symbol,
            member=member,
            pool_version=pool_version,
            observed_at=observed,
            market_session=market_session,
            signal=signal,
            signal_status=str(signal_record.get("status") or signal.get("status") or ""),
            score=score,
            plan=plan,
            hotspot=hotspot,
            broker_status=broker_status,
            universe_complete=bool(universe.get("data_complete", True)),
            latest_price=current_prices.get(symbol),
        ))
    return sorted(result, key=lambda item: (item.action_priority, item.symbol))


def _project_one(
    *,
    symbol: str,
    member: dict[str, Any],
    pool_version: str,
    observed_at: str,
    market_session: str,
    signal: dict[str, Any],
    signal_status: str,
    score: dict[str, Any],
    plan: dict[str, Any],
    hotspot: dict[str, Any],
    broker_status: str,
    universe_complete: bool,
    latest_price: float | None,
) -> StockTradingSandboxSnapshot:
    daily_status = _daily_status(member, signal_status)
    score_status = str(score.get("status") or "not_started")
    plan_status = str(plan.get("status") or "none")
    risk = plan.get("risk_snapshot") or {}
    negative_blocks = list(hotspot.get("negative_blocks") or [])
    hard_blocks = list(dict.fromkeys([
        *negative_blocks,
        *(score.get("hard_blocks") or []),
    ]))
    data_gaps = list(dict.fromkeys([
        *(score.get("data_gaps") or []),
        *(hotspot.get("data_gaps") or []),
    ]))
    state, action, priority = _state_and_action(
        member=member,
        universe_complete=universe_complete,
        signal_status=signal_status,
        score_status=score_status,
        plan_status=plan_status,
        risk=risk,
        hard_blocks=hard_blocks,
        data_gaps=data_gaps,
    )
    items = list(hotspot.get("items") or [])
    hotspot_title = str((items[0] if items else {}).get("title") or "")
    hotspot_status = (
        "重大风险" if negative_blocks
        else "数据不完整" if hotspot.get("data_gaps")
        else f"已跟踪 {len(items)} 条" if hotspot
        else "等待热点快照"
    )
    identity = {
        "symbol": symbol,
        "pool_version": pool_version,
        "state": state.value,
        "daily_status": daily_status,
        "score": {
            key: score.get(key)
            for key in (
                "bar_closed_at", "status", "index_score", "sentiment_score",
                "theme_score", "stock_score", "total_score",
                "consecutive_pass_count", "input_hash",
            )
        },
        "hotspot_hash": hotspot.get("source_hash"),
        "plan_id": plan.get("id"),
        "plan_status": plan_status,
        "broker_status": broker_status,
        "hard_blocks": hard_blocks,
        "data_gaps": data_gaps,
    }
    return StockTradingSandboxSnapshot(
        symbol=symbol,
        name=str(member.get("name") or symbol),
        pool_version=pool_version,
        observed_at=observed_at,
        market_session=market_session,
        state=state,
        state_label=STATE_LABELS[state],
        daily_status=daily_status,
        score_status=score_status,
        index_score=score.get("index_score"),
        sentiment_score=score.get("sentiment_score"),
        theme_score=score.get("theme_score"),
        stock_score=score.get("stock_score"),
        total_score=score.get("total_score"),
        consecutive_pass_count=int(score.get("consecutive_pass_count") or 0),
        hotspot_status=hotspot_status,
        hotspot_title=hotspot_title,
        hard_blocks=hard_blocks,
        data_gaps=data_gaps,
        plan_id=str(plan.get("id")) if plan.get("id") else None,
        plan_status=plan_status,
        account_risk_status=_account_risk_status(state, risk, broker_status),
        broker_status=broker_status,
        trigger_price=_float_or_none(signal.get("trigger_price") or plan.get("entry_price")),
        max_entry_price=_float_or_none(
            signal.get("max_entry_price") or risk.get("max_entry_price")
        ),
        initial_stop=_float_or_none(signal.get("initial_stop") or plan.get("stop_loss_price")),
        valid_until=str(signal.get("valid_until") or plan.get("valid_until") or ""),
        latest_price=(
            _float_or_none(latest_price)
            if latest_price is not None
            else _float_or_none(member.get("latest_price"))
        ),
        action=action,
        action_priority=priority,
        input_hash=_stable_hash(identity),
    )


def _state_and_action(
    *,
    member: dict[str, Any],
    universe_complete: bool,
    signal_status: str,
    score_status: str,
    plan_status: str,
    risk: dict[str, Any],
    hard_blocks: list[str],
    data_gaps: list[str],
) -> tuple[StockSandboxState, str, int]:
    if not member.get("authorization_eligible", True):
        return StockSandboxState.ANALYSIS_ONLY, "持续分析，不进入实盘授权", 70
    if not universe_complete:
        return StockSandboxState.DATA_INCOMPLETE, "补齐股票池数据", 80
    if hard_blocks and plan_status == "none":
        return StockSandboxState.MAJOR_RISK_BLOCKED, "复核重大负面事件", 1
    if hard_blocks or plan_status in {"invalidated", "expired", "cancelled", "rejected"}:
        return StockSandboxState.INVALIDATED, "检查重大风险或计划失效原因", 1
    if plan_status == "exit_detected":
        return StockSandboxState.EXIT_REQUIRED, "按既定退出规则处理", 2
    if plan_status in {"executed_open", "filled"}:
        return StockSandboxState.FILLED, "跟踪止损、止盈和时间退出", 15
    if plan_status == "partially_filled":
        return StockSandboxState.PARTIALLY_FILLED, "核对部分成交和剩余委托", 14
    if plan_status == "submitted":
        return StockSandboxState.SUBMITTED, "等待委托与成交同步", 13
    if plan_status == "awaiting_user_confirmation":
        return StockSandboxState.WAITING_USER_CONFIRMATION, "请在同花顺人工确认", 12
    if str(risk.get("authorization_status") or "") == "blocked":
        return StockSandboxState.ACCOUNT_RISK_BLOCKED, "查看账户与组合风控原因", 5
    if (
        bool(risk.get("live_authorized"))
        or str(risk.get("authorization_status") or "") == "authorized"
    ):
        return StockSandboxState.AUTHORIZED, "可安全预填到同花顺", 11
    if data_gaps:
        return StockSandboxState.DATA_INCOMPLETE, "等待缺失数据恢复", 80
    if score_status == "eligible_for_risk":
        return StockSandboxState.QUANT_TRADEABLE, "进入账户与组合风控", 10
    if signal_status.lower() == "allow":
        if score_status == "wait_confirmation":
            return StockSandboxState.WAIT_CONFIRMATION, "等待下一根15分钟确认", 20
        return StockSandboxState.INTRADAY_OBSERVING, "持续跟踪15分钟评分", 30
    if signal_status:
        return StockSandboxState.DAILY_REJECTED, "等待下一次日线条件重评", 60
    return StockSandboxState.DAILY_OBSERVING, "等待收盘后日线扫描", 65


def _latest_daily_signals(
    records: list[dict[str, Any]], pool_version: str
) -> dict[str, dict[str, Any]]:
    matching = [
        item for item in records
        if (
            str(item.get("pool_version") or "") == pool_version
            or str(
                ((item.get("decision") or {}).get("condition_snapshot") or {}).get(
                    "base_pool_version"
                )
                or ""
            ) == pool_version
        )
    ]
    latest_day = max(
        (str(item.get("signal_time") or "")[:10] for item in matching),
        default="",
    )
    result: dict[str, dict[str, Any]] = {}
    for item in matching:
        if str(item.get("signal_time") or "")[:10] != latest_day:
            continue
        result.setdefault(str(item.get("symbol") or ""), item)
    return result


def _latest_by_symbol(
    records: list[dict[str, Any]], *, payload_key: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        payload = item.get(payload_key) or {}
        symbol = str(item.get("symbol") or payload.get("symbol") or "")
        if symbol:
            result.setdefault(symbol, payload)
    return result


def _latest_plans(
    plans: list[dict[str, Any]], pool_version: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for plan in plans:
        if str(plan.get("strategy_version") or "") not in {
            TOPDOWN_STRATEGY_ID,
            MANUAL_EXCEPTION_STRATEGY_ID,
        }:
            continue
        risk = plan.get("risk_snapshot") or {}
        if (
            str(risk.get("pool_version") or "") != pool_version
            and str(risk.get("base_pool_version") or "") != pool_version
        ):
            continue
        result.setdefault(str(plan.get("symbol") or ""), plan)
    return result


def _daily_status(member: dict[str, Any], signal_status: str) -> str:
    if not member.get("authorization_eligible", True):
        return "只分析"
    if not signal_status:
        return "待扫描"
    return "通过" if signal_status.lower() == "allow" else "未通过"


def _broker_status(snapshot: Any) -> str:
    if snapshot is None:
        return "not_connected"
    connection = getattr(snapshot, "connection", None)
    value = getattr(connection, "status", "")
    return str(getattr(value, "value", value) or "unknown")


def _account_risk_status(
    state: StockSandboxState, risk: dict[str, Any], broker_status: str
) -> str:
    if bool(risk.get("live_authorized")):
        return "authorized"
    if state is StockSandboxState.AUTHORIZED:
        return "authorized"
    if state is StockSandboxState.ACCOUNT_RISK_BLOCKED:
        return "blocked"
    if state is StockSandboxState.QUANT_TRADEABLE:
        return "waiting_for_account" if broker_status == "not_connected" else "not_evaluated"
    return "not_applicable"


def _market_session(value: str) -> str:
    try:
        point = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return "unknown"
    if point.weekday() >= 5:
        return "closed"
    minutes = point.hour * 60 + point.minute
    if 9 * 60 + 15 <= minutes < 9 * 60 + 30:
        return "pre_open"
    if 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
        return "trading"
    if 13 * 60 <= minutes <= 15 * 60:
        return "trading"
    return "closed"


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
