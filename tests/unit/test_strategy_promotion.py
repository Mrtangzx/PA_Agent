from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan, TradeResult
from pa_agent.trading.promotion import build_shadow_performance_evidence
from pa_agent.trading.quant import StrategyState
from pa_agent.trading.stability import PerformanceEvidence, StrategyStabilityController
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_SCORING_VERSION, TOPDOWN_STRATEGY_ID
from pa_agent.trading.universe import CLOUD_AI_UNIVERSE_ID


def _add_shadow_result(
    store: TradeStore,
    *,
    opened_at: str,
    closed_at: str,
    net_pnl: float | None,
    r_multiple: float,
    traced: bool = True,
    scoring_version: str = TOPDOWN_SCORING_VERSION,
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
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "scoring_version": scoring_version,
        "pool_version": f"{CLOUD_AI_UNIVERSE_ID}-2026-08",
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


def _enter_shadow(store: TradeStore) -> str:
    evidence = PerformanceEvidence(
        dataset="out_of_sample",
        trade_count=200,
        expectancy_r=0.15,
        profit_factor=1.2,
        max_drawdown_pct=9,
        profitable_month_ratio=0.75,
        point_in_time_universe_verified=True,
        source_time_alignment_verified=True,
        execution_rules_verified=True,
        hotspot_sentiment_history_verified=True,
    )
    run_id = store.add_validation_run(
        {
            "strategy_version": TOPDOWN_STRATEGY_ID,
            "status": "complete",
            "input_hash": uuid.uuid4().hex,
            "performance_evidence": evidence.model_dump(mode="json"),
        },
        dataset="out_of_sample",
        promotion_eligible=True,
    )
    transition = StrategyStabilityController().evaluate(
        StrategyState.CANDIDATE,
        evidence,
    )
    store.record_strategy_transition(
        transition,
        evidence,
        strategy_id=TOPDOWN_STRATEGY_ID,
        validation_run_id=run_id,
    )
    return store.list_strategy_transitions(strategy_id=TOPDOWN_STRATEGY_ID)[0][
        "created_at"
    ]


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

    evidence, gaps = build_shadow_performance_evidence(
        store,
        as_of=start + timedelta(days=91),
    )

    assert evidence.trade_count == 0
    assert evidence.weeks == 0
    assert not evidence.source_time_alignment_verified
    assert not evidence.execution_rules_verified
    assert "shadow_state_not_entered" in gaps
    assert "shadow_state_not_current:candidate" in gaps
    assert "pre_shadow_results_excluded:1" in gaps
    assert "fixed_execution_mechanism_validation_incomplete" in gaps


def test_shadow_evidence_uses_frozen_fixed_validation_and_complete_months(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    shadow_started_at = datetime.fromisoformat(_enter_shadow(store))
    store.add_validation_run({
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "status": "complete",
        "input_hash": "fixed",
        "checks": [{"name": "mechanics", "passed": True}],
    }, dataset="fixed_replay")
    start = shadow_started_at + timedelta(days=1)
    _add_shadow_result(
        store,
        opened_at=start.isoformat(),
        closed_at=(start + timedelta(days=31)).isoformat(),
        net_pnl=100,
        r_multiple=1,
    )
    _add_shadow_result(
        store,
        opened_at=(start + timedelta(days=32)).isoformat(),
        closed_at=(start + timedelta(days=109)).isoformat(),
        net_pnl=100,
        r_multiple=1,
    )

    evidence, gaps = build_shadow_performance_evidence(
        store,
        as_of=start + timedelta(days=109),
    )

    assert evidence.complete_months == 3
    assert not evidence.all_complete_months_profitable
    assert evidence.source_time_alignment_verified
    assert evidence.execution_rules_verified
    assert gaps == []


def test_shadow_evidence_rejects_old_scoring_version(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    shadow_started_at = datetime.fromisoformat(_enter_shadow(store))
    opened_at = shadow_started_at + timedelta(days=1)
    closed_at = opened_at + timedelta(days=92)
    opened = opened_at.isoformat()
    closed = closed_at.isoformat()
    _add_shadow_result(
        store,
        opened_at=opened,
        closed_at=closed,
        net_pnl=100,
        r_multiple=1,
        scoring_version="1.0.0",
    )

    evidence, gaps = build_shadow_performance_evidence(store, as_of=closed_at)

    assert not evidence.source_time_alignment_verified
    assert "shadow_topdown_version_trace_incomplete" in gaps


def test_candidate_shadow_results_are_excluded_after_formal_shadow_entry(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    candidate_opened = datetime.now().astimezone() - timedelta(days=30)
    _add_shadow_result(
        store,
        opened_at=candidate_opened.isoformat(),
        closed_at=(candidate_opened + timedelta(days=1)).isoformat(),
        net_pnl=100,
        r_multiple=1,
    )
    shadow_started_at = datetime.fromisoformat(_enter_shadow(store))
    formal_opened = shadow_started_at + timedelta(minutes=1)
    _add_shadow_result(
        store,
        opened_at=formal_opened.isoformat(),
        closed_at=(formal_opened + timedelta(days=1)).isoformat(),
        net_pnl=100,
        r_multiple=1,
    )

    evidence, gaps = build_shadow_performance_evidence(
        store,
        as_of=formal_opened + timedelta(days=2),
    )

    assert evidence.trade_count == 1
    assert "pre_shadow_results_excluded:1" in gaps


def test_one_long_trade_cannot_manufacture_twelve_observation_weeks(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    shadow_started_at = datetime.fromisoformat(_enter_shadow(store))
    opened_at = shadow_started_at + timedelta(minutes=1)
    _add_shadow_result(
        store,
        opened_at=opened_at.isoformat(),
        closed_at=(opened_at + timedelta(days=91)).isoformat(),
        net_pnl=100,
        r_multiple=1,
    )

    evidence, gaps = build_shadow_performance_evidence(
        store,
        as_of=shadow_started_at + timedelta(days=7),
    )

    assert evidence.trade_count == 0
    assert evidence.weeks == 1
    assert "shadow_trade_timestamps_incomplete" in gaps


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
