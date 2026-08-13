from __future__ import annotations

import pytest

from pa_agent.trading.store import TradeStore


def _report(**updates) -> dict:
    report = {
        "strategy_version": "hs300_topdown_4321_intraday_v1",
        "scoring_version": "1.0.0",
        "status": "complete",
        "input_hash": "fixture-hash",
        "frame_count": 2,
        "eligible_count": 1,
    }
    report.update(updates)
    return report


def test_fixed_replay_is_idempotent_and_never_promotion_evidence(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")

    first = store.add_validation_run(_report(), dataset="fixed_replay")
    second = store.add_validation_run(_report(), dataset="fixed_replay")
    runs = store.list_validation_runs(
        strategy_version="hs300_topdown_4321_intraday_v1"
    )

    assert first == second
    assert len(runs) == 1
    assert runs[0]["dataset"] == "fixed_replay"
    assert not runs[0]["promotion_eligible"]
    assert runs[0]["report"]["eligible_count"] == 1


def test_fixed_replay_cannot_be_marked_promotion_eligible(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")

    with pytest.raises(ValueError, match="fixed replay"):
        store.add_validation_run(
            _report(), dataset="fixed_replay", promotion_eligible=True
        )


def test_strategy_transition_requires_promotion_evidence_or_explicit_approval(tmp_path) -> None:
    from pa_agent.trading.quant import StrategyState
    from pa_agent.trading.stability import (
        LiveActivationApproval,
        PerformanceEvidence,
        StrategyStabilityController,
    )

    store = TradeStore(tmp_path / "trades.db")
    fixed_id = store.add_validation_run(
        _report(), dataset="fixed_replay", promotion_eligible=False
    )
    candidate_evidence = PerformanceEvidence(
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
    candidate_transition = StrategyStabilityController().evaluate(
        StrategyState.CANDIDATE, candidate_evidence
    )

    with pytest.raises(ValueError, match="promotion-eligible validation evidence"):
        store.record_strategy_transition(
            candidate_transition,
            candidate_evidence,
            strategy_id="hs300_topdown_4321_intraday_v1",
            validation_run_id=fixed_id,
        )

    oos_id = store.add_validation_run(
        _report(
            input_hash="b" * 64,
            performance_evidence=candidate_evidence.model_dump(mode="json"),
        ),
        dataset="out_of_sample",
        promotion_eligible=True,
    )
    event_id = store.record_strategy_transition(
        candidate_transition,
        candidate_evidence,
        strategy_id="hs300_topdown_4321_intraday_v1",
        validation_run_id=oos_id,
    )
    assert event_id > 0
    assert store.current_strategy_state("hs300_topdown_4321_intraday_v1") == "shadow"

    shadow_evidence = PerformanceEvidence(
        dataset="shadow",
        trade_count=80,
        weeks=12,
        complete_months=3,
        all_complete_months_profitable=True,
        profit_factor=1.15,
        source_time_alignment_verified=True,
        execution_rules_verified=True,
    )
    approval = LiveActivationApproval(
        approved_at="2026-08-13T12:00:00+08:00",
        account_fingerprint="ths:fixture",
        initial_risk_pct=0.25,
        acknowledgment_version="small_live_v1",
    )
    active_transition = StrategyStabilityController().evaluate(
        StrategyState.SHADOW,
        shadow_evidence,
        live_approval=approval,
    )
    shadow_id = store.add_validation_run(
        _report(
            input_hash="c" * 64,
            performance_evidence=shadow_evidence.model_dump(mode="json"),
        ),
        dataset="shadow",
        promotion_eligible=True,
    )
    store.record_strategy_transition(
        active_transition,
        shadow_evidence,
        strategy_id="hs300_topdown_4321_intraday_v1",
        validation_run_id=shadow_id,
        live_approval=approval,
    )
    assert store.current_strategy_state("hs300_topdown_4321_intraday_v1") == "active"


def test_promotion_run_rejects_missing_or_mismatched_performance_evidence(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")

    with pytest.raises(ValueError, match="matching performance_evidence"):
        store.add_validation_run(
            _report(input_hash="d" * 64),
            dataset="out_of_sample",
            promotion_eligible=True,
        )


def test_strategy_state_store_rejects_forged_previous_state_and_skipped_stage(tmp_path) -> None:
    from pa_agent.trading.stability import PerformanceEvidence, StateTransition

    store = TradeStore(tmp_path / "trades.db")
    evidence = PerformanceEvidence(dataset="shadow", trade_count=80)

    with pytest.raises(ValueError, match="does not match stored state"):
        store.record_strategy_transition(
            StateTransition(
                previous="shadow",
                current="shadow",
                reasons=["forged"],
            ),
            evidence,
            strategy_id="hs300_topdown_4321_intraday_v1",
        )

    with pytest.raises(ValueError, match="invalid strategy state transition"):
        store.record_strategy_transition(
            StateTransition(
                previous="candidate",
                current="active",
                reasons=["skip_shadow"],
                automatic=False,
            ),
            evidence,
            strategy_id="hs300_topdown_4321_intraday_v1",
            live_approval={
                "approved_at": "2026-08-13T12:00:00+08:00",
                "account_fingerprint": "ths:fixture",
                "initial_risk_pct": 0.25,
                "acknowledgment_version": "small_live_v1",
            },
        )

    with pytest.raises(ValueError, match="matching performance_evidence"):
        store.add_validation_run(
            _report(
                input_hash="e" * 64,
                performance_evidence={"dataset": "shadow"},
            ),
            dataset="out_of_sample",
            promotion_eligible=True,
        )
