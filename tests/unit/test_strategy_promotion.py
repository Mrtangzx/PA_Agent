from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan, TradeResult
from pa_agent.trading.promotion import build_shadow_performance_evidence
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID


def _add_shadow_result(
    store: TradeStore,
    *,
    opened_at: str,
    closed_at: str,
    net_pnl: float | None,
    r_multiple: float,
    traced: bool = True,
) -> None:
    decision_id = store.add_decision(
        symbol="600519",
        timeframe="15m",
        asset_class=AssetClass.A_SHARE.value,
        original_decision={},
        final_decision={},
        meta={"strategy_version": TOPDOWN_STRATEGY_ID},
    )
    score = {
        "input_hash": uuid.uuid4().hex,
        "bar_closed_at": opened_at,
        "data_gaps": [],
        "hard_blocks": [],
    } if traced else {}
    plan = TradePlan(
        id=uuid.uuid4().hex,
        decision_event_id=decision_id,
        symbol="600519",
        timeframe="15m",
        asset_class=AssetClass.A_SHARE,
        direction="buy",
        order_type="limit",
        entry_price=100,
        stop_loss_price=95,
        take_profit_price=110,
        status=PlanStatus.PROPOSED,
        shadow_status="closed",
        strategy_version=TOPDOWN_STRATEGY_ID,
        risk_snapshot={"topdown_score": score},
    )
    store.add_plan(plan)
    store.add_result(TradeResult(
        id=uuid.uuid4().hex,
        plan_id=plan.id,
        dataset="shadow",
        outcome="win" if (net_pnl or 0) > 0 else "loss",
        entry_price=100,
        exit_price=110 if (net_pnl or 0) > 0 else 95,
        quantity=100,
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        r_multiple=r_multiple,
        opened_at=opened_at,
        closed_at=closed_at,
    ))


def test_shadow_evidence_is_derived_from_strategy_results_and_fails_closed(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    start = datetime.fromisoformat("2026-01-01T10:00:00+08:00")
    _add_shadow_result(
        store,
        opened_at=start.isoformat(),
        closed_at=(start + timedelta(days=91)).isoformat(),
        net_pnl=None,
        r_multiple=1,
        traced=False,
    )

    evidence, gaps = build_shadow_performance_evidence(store)

    assert evidence.trade_count == 1
    assert evidence.weeks == 13
    assert not evidence.source_time_alignment_verified
    assert not evidence.execution_rules_verified
    assert "shadow_net_pnl_or_fees_incomplete" in gaps
    assert "shadow_topdown_source_trace_incomplete" in gaps
    assert "fixed_execution_mechanism_validation_incomplete" in gaps


def test_shadow_evidence_uses_frozen_fixed_validation_and_complete_months(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    store.add_validation_run({
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "status": "complete",
        "input_hash": "fixed",
        "checks": [{"name": "mechanics", "passed": True}],
    }, dataset="fixed_replay")
    start = datetime.fromisoformat("2026-01-01T00:00:00+08:00")
    _add_shadow_result(
        store,
        opened_at=start.isoformat(),
        closed_at="2026-02-15T15:00:00+08:00",
        net_pnl=100,
        r_multiple=1,
    )
    _add_shadow_result(
        store,
        opened_at="2026-02-15T15:00:00+08:00",
        closed_at="2026-04-01T00:00:00+08:00",
        net_pnl=100,
        r_multiple=1,
    )

    evidence, gaps = build_shadow_performance_evidence(store)

    assert evidence.complete_months == 3
    assert not evidence.all_complete_months_profitable
    assert evidence.source_time_alignment_verified
    assert evidence.execution_rules_verified
    assert gaps == []


def test_generated_oos_report_is_persisted_but_small_sample_cannot_promote(
    tmp_path,
) -> None:
    from pa_agent.trading.oos_backtest import OosBacktestReport, OosBacktestTrade
    from pa_agent.trading.promotion import StrategyPromotionService
    from pa_agent.trading.quant import StrategyState
    from pa_agent.trading.stability import PerformanceEvidence

    store = TradeStore(tmp_path / "trades.db")
    evidence = PerformanceEvidence(
        dataset="out_of_sample",
        trade_count=1,
        expectancy_r=0,
        max_drawdown_pct=0,
        point_in_time_universe_verified=True,
        source_time_alignment_verified=True,
        execution_rules_verified=True,
        hotspot_sentiment_history_verified=True,
    )
    report = OosBacktestReport(
        status="complete",
        input_hash="a" * 64,
        performance_evidence=evidence.model_dump(mode="json"),
        gate_failures=["trade_count_below_200:1"],
        trades=[OosBacktestTrade(
            symbol="600519",
            pool_version="hs300-2026-08",
            signal_time="2026-08-11T15:00:00+08:00",
            score_time="2026-08-12T10:00:00+08:00",
            entered_at="2026-08-12T10:15:00+08:00",
            exited_at="2026-08-13T15:00:00+08:00",
            entry_price=100,
            exit_price=100,
            quantity=100,
            initial_stop=95,
            gross_pnl=0,
            fees=0,
            net_pnl=0,
            r_multiple=0,
            holding_days=2,
            exit_reason="fixture",
            score_input_hash="c" * 64,
        )],
        generated_at="2026-08-13T12:00:00+08:00",
    )

    run_id, transition = StrategyPromotionService(store).record_out_of_sample_report(
        report
    )

    assert run_id
    assert transition.current is StrategyState.CANDIDATE
    assert store.current_strategy_state(TOPDOWN_STRATEGY_ID) == "candidate"
    run = store.list_validation_runs(strategy_version=TOPDOWN_STRATEGY_ID)[0]
    assert run["dataset"] == "out_of_sample"
    assert not run["promotion_eligible"]


def test_oos_promotion_service_rejects_forged_eligible_flag(tmp_path) -> None:
    import pytest

    from pa_agent.trading.oos_backtest import OosBacktestReport
    from pa_agent.trading.promotion import StrategyPromotionService
    from pa_agent.trading.stability import PerformanceEvidence

    evidence = PerformanceEvidence(
        dataset="out_of_sample",
        trade_count=1,
        point_in_time_universe_verified=True,
        source_time_alignment_verified=True,
        execution_rules_verified=True,
        hotspot_sentiment_history_verified=True,
    )
    forged = OosBacktestReport(
        status="complete",
        input_hash="b" * 64,
        promotion_eligible=True,
        performance_evidence=evidence.model_dump(mode="json"),
        generated_at="2026-08-13T12:00:00+08:00",
    )

    with pytest.raises(ValueError, match="trade count|promotion-eligible"):
        StrategyPromotionService(store=TradeStore(tmp_path / "trades.db")).record_out_of_sample_report(
            forged
        )
