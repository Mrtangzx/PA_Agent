"""Auditable strategy-state transitions from measured performance."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pa_agent.trading.quant import StrategyState


class PerformanceEvidence(BaseModel):
    dataset: str
    trade_count: int = Field(ge=0)
    weeks: float = Field(default=0, ge=0)
    expectancy_r: float | None = None
    profit_factor: float | None = None
    max_drawdown_pct: float | None = None
    profitable_month_ratio: float | None = Field(default=None, ge=0, le=1)
    complete_months: int = Field(default=0, ge=0)
    all_complete_months_profitable: bool = False
    rolling_60_expectancy_r: float | None = None
    slippage_model_breached: bool = False
    reconciliation_anomaly: bool = False
    point_in_time_universe_verified: bool = False
    source_time_alignment_verified: bool = False
    execution_rules_verified: bool = False
    hotspot_sentiment_history_verified: bool = False


class LiveActivationApproval(BaseModel):
    """Explicit, auditable user consent for the initial small-live stage."""

    approved_at: str = Field(min_length=1)
    account_fingerprint: str = Field(min_length=1)
    initial_risk_pct: Literal[0.25] = 0.25
    acknowledgment_version: str = Field(min_length=1)


class StateTransition(BaseModel):
    previous: StrategyState
    current: StrategyState
    reasons: list[str] = Field(default_factory=list)
    automatic: bool = True


class StrategyStabilityController:
    def evaluate(
        self,
        current: StrategyState,
        evidence: PerformanceEvidence,
        *,
        live_approval: LiveActivationApproval | None = None,
    ) -> StateTransition:
        reasons: list[str] = []
        drawdown = evidence.max_drawdown_pct or 0.0
        live_state = current in {
            StrategyState.ACTIVE,
            StrategyState.REDUCED,
            StrategyState.PAUSED,
        }
        if live_state and drawdown >= 10:
            return StateTransition(previous=current, current=StrategyState.RETIRED, reasons=["drawdown_10pct"])
        if (
            (live_state and drawdown >= 8)
            or evidence.slippage_model_breached
            or evidence.reconciliation_anomaly
            or (evidence.rolling_60_expectancy_r is not None and evidence.rolling_60_expectancy_r <= 0)
        ):
            if live_state and drawdown >= 8:
                reasons.append("drawdown_8pct")
            if evidence.slippage_model_breached:
                reasons.append("slippage_model_breached")
            if evidence.reconciliation_anomaly:
                reasons.append("broker_reconciliation_anomaly")
            if evidence.rolling_60_expectancy_r is not None and evidence.rolling_60_expectancy_r <= 0:
                reasons.append("rolling_60_expectancy_non_positive")
            return StateTransition(previous=current, current=StrategyState.PAUSED, reasons=reasons)
        if drawdown >= 5 and current in {StrategyState.ACTIVE, StrategyState.REDUCED}:
            return StateTransition(previous=current, current=StrategyState.REDUCED, reasons=["drawdown_5pct"])
        if current is StrategyState.CANDIDATE:
            passed = (
                evidence.dataset == "out_of_sample"
                and evidence.trade_count >= 200
                and (evidence.expectancy_r or -999) >= 0.15
                and (evidence.profit_factor or 0) >= 1.20
                and drawdown <= 10
                and (evidence.profitable_month_ratio or 0) >= 0.75
                and evidence.point_in_time_universe_verified
                and evidence.source_time_alignment_verified
                and evidence.execution_rules_verified
                and evidence.hotspot_sentiment_history_verified
            )
            if passed:
                return StateTransition(previous=current, current=StrategyState.SHADOW, reasons=["oos_gate_passed"])
        if current is StrategyState.SHADOW:
            passed = (
                evidence.dataset == "shadow"
                and evidence.trade_count >= 80
                and evidence.weeks >= 12
                and evidence.complete_months > 0
                and evidence.all_complete_months_profitable
                and (evidence.profit_factor or 0) >= 1.15
                and evidence.source_time_alignment_verified
                and evidence.execution_rules_verified
            )
            if passed:
                if live_approval is None:
                    return StateTransition(
                        previous=current,
                        current=StrategyState.SHADOW,
                        reasons=["awaiting_explicit_live_activation_approval"],
                        automatic=False,
                    )
                return StateTransition(
                    previous=current,
                    current=StrategyState.ACTIVE,
                    reasons=["shadow_gate_passed_user_approved_small_live"],
                    automatic=False,
                )
        return StateTransition(previous=current, current=current, reasons=["no_transition"])
