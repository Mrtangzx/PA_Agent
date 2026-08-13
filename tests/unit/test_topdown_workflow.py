from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pa_agent.data.base import KlineBar
from pa_agent.trading.quant import SignalDecision, SignalStatus, StrategyContext
from pa_agent.trading.quant_workflow import QuantTradingWorkflow
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import (
    TOPDOWN_STRATEGY_ID,
    TopDownScoreSnapshot,
    TopDownScoreStatus,
)

NOW = "2026-08-12T10:00:00+08:00"


class _AlwaysAllow:
    def evaluate(self, context: StrategyContext) -> SignalDecision:
        return SignalDecision(
            status=SignalStatus.ALLOW,
            strategy_id="hs300_daily_pullback_v1",
            parameter_version="1.0.0",
            pool_version=context.pool_version,
            symbol=context.symbol,
            signal_time=context.signal_time,
            trigger_price=100,
            max_entry_price=101,
            initial_stop=95,
            valid_until="2026-08-13T15:00:00+08:00",
        )


def _context() -> StrategyContext:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = tuple(KlineBar(
        seq=80 - index,
        ts_open=(start + timedelta(days=index)).timestamp() * 1000,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1_000_000,
    ) for index in range(80))
    return StrategyContext(
        symbol="600519",
        bars=bars,
        index_bars=bars,
        market_breadth_pct=70,
        pool_version="hs300-2026-08",
        signal_time="2026-08-11T15:00:00+08:00",
    )


def _score(status: TopDownScoreStatus) -> TopDownScoreSnapshot:
    return TopDownScoreSnapshot(
        strategy_version=TOPDOWN_STRATEGY_ID,
        scoring_version="1.0.0",
        symbol="600519",
        pool_version="hs300-2026-08",
        bar_closed_at=NOW,
        index_score=31,
        sentiment_score=22,
        theme_score=14,
        stock_score=8,
        total_score=75,
        consecutive_pass_count=2 if status is TopDownScoreStatus.ELIGIBLE_FOR_RISK else 1,
        input_hash=(status.value[0] or "x") * 64,
        status=status,
    )


def test_topdown_workflow_does_not_create_plan_before_two_bar_gate(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    workflow = QuantTradingWorkflow(store, _AlwaysAllow())
    result = workflow.evaluate_topdown(_context(), _score(TopDownScoreStatus.WAIT_CONFIRMATION))
    assert result["plan_id"] is None
    assert store.list_plans() == []
    assert "topdown_wait_confirmation" in result["decision"].reasons


def test_topdown_workflow_creates_auditable_plan_after_gate(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    workflow = QuantTradingWorkflow(store, _AlwaysAllow())
    result = workflow.evaluate_topdown(_context(), _score(TopDownScoreStatus.ELIGIBLE_FOR_RISK))
    assert result["plan_id"]
    plan = store.get_plan(result["plan_id"])
    assert plan is not None
    assert plan["strategy_version"] == TOPDOWN_STRATEGY_ID
    assert plan["risk_snapshot"]["topdown_score"]["total_score"] == 75


def test_topdown_plan_creation_is_idempotent_for_same_score_hash(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    workflow = QuantTradingWorkflow(store, _AlwaysAllow())
    daily = _AlwaysAllow().evaluate(_context())
    score = _score(TopDownScoreStatus.ELIGIBLE_FOR_RISK)

    first = workflow.create_topdown_plan(daily, score)
    second = workflow.create_topdown_plan(daily, score)

    assert first["plan_id"] == second["plan_id"]
    assert len(store.list_plans(symbol="600519")) == 1


def test_topdown_workflow_does_not_duplicate_score_persistence(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    workflow = QuantTradingWorkflow(store, _AlwaysAllow())

    workflow.evaluate_topdown(_context(), _score(TopDownScoreStatus.ELIGIBLE_FOR_RISK))

    assert store.list_topdown_scores(symbol="600519") == []
