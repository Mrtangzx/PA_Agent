from pa_agent.trading.quant import StrategyState
from pa_agent.trading.stability import (
    LiveActivationApproval,
    PerformanceEvidence,
    StrategyStabilityController,
)


def test_candidate_requires_full_out_of_sample_gate() -> None:
    transition = StrategyStabilityController().evaluate(
        StrategyState.CANDIDATE,
        PerformanceEvidence(
            dataset="out_of_sample", trade_count=200, expectancy_r=0.15,
            profit_factor=1.2, max_drawdown_pct=9, profitable_month_ratio=0.75,
            point_in_time_universe_verified=True,
            source_time_alignment_verified=True,
            execution_rules_verified=True,
            hotspot_sentiment_history_verified=True,
        ),
    )
    assert transition.current is StrategyState.SHADOW


def test_reconciliation_anomaly_pauses_active_strategy() -> None:
    transition = StrategyStabilityController().evaluate(
        StrategyState.ACTIVE,
        PerformanceEvidence(dataset="actual", trade_count=30, reconciliation_anomaly=True),
    )
    assert transition.current is StrategyState.PAUSED


def _passing_shadow_evidence() -> PerformanceEvidence:
    return PerformanceEvidence(
        dataset="shadow",
        trade_count=80,
        weeks=12,
        complete_months=3,
        all_complete_months_profitable=True,
        profit_factor=1.15,
        source_time_alignment_verified=True,
        execution_rules_verified=True,
    )


def test_candidate_cannot_promote_when_historical_hotspot_evidence_is_missing() -> None:
    transition = StrategyStabilityController().evaluate(
        StrategyState.CANDIDATE,
        PerformanceEvidence(
            dataset="out_of_sample",
            trade_count=500,
            expectancy_r=0.5,
            profit_factor=2,
            max_drawdown_pct=2,
            profitable_month_ratio=0.9,
            point_in_time_universe_verified=True,
            source_time_alignment_verified=True,
            execution_rules_verified=True,
            hotspot_sentiment_history_verified=False,
        ),
    )

    assert transition.current is StrategyState.CANDIDATE


def test_passing_shadow_gate_never_activates_without_explicit_user_approval() -> None:
    transition = StrategyStabilityController().evaluate(
        StrategyState.SHADOW,
        _passing_shadow_evidence(),
    )

    assert transition.current is StrategyState.SHADOW
    assert not transition.automatic
    assert transition.reasons == ["awaiting_explicit_live_activation_approval"]


def test_passing_shadow_gate_can_activate_with_auditable_user_approval() -> None:
    transition = StrategyStabilityController().evaluate(
        StrategyState.SHADOW,
        _passing_shadow_evidence(),
        live_approval=LiveActivationApproval(
            approved_at="2026-08-13T12:00:00+08:00",
            account_fingerprint="ths:fixture",
            initial_risk_pct=0.25,
            acknowledgment_version="small_live_v1",
        ),
    )

    assert transition.current is StrategyState.ACTIVE
    assert not transition.automatic
    assert transition.reasons == ["shadow_gate_passed_user_approved_small_live"]
