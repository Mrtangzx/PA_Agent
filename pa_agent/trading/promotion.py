"""System-derived promotion evidence and explicit live activation workflow."""
from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.trading.quant import StrategyState
from pa_agent.trading.stability import (
    LiveActivationApproval,
    PerformanceEvidence,
    StateTransition,
    StrategyStabilityController,
)
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID


class PromotionEvidenceReport(BaseModel):
    strategy_version: str
    dataset: str
    status: str
    input_hash: str
    performance_evidence: dict[str, Any]
    promotion_eligible: bool = False
    data_gaps: list[str] = Field(default_factory=list)
    generated_at: str


def build_shadow_performance_evidence(
    store: Any,
    *,
    strategy_id: str = TOPDOWN_STRATEGY_ID,
) -> tuple[PerformanceEvidence, list[str]]:
    """Derive shadow evidence exclusively from persisted, auditable results."""
    rows = [
        item
        for item in store.list_results(dataset="shadow")
        if item.get("strategy_version") == strategy_id
    ]
    gaps: list[str] = []
    if not rows:
        gaps.append("shadow_trade_results_missing")
    opened = [_parse_time(item.get("opened_at")) for item in rows]
    closed = [_parse_time(item.get("closed_at")) for item in rows]
    valid_opened = [item for item in opened if item is not None]
    valid_closed = [item for item in closed if item is not None]
    if len(valid_opened) != len(rows) or len(valid_closed) != len(rows):
        gaps.append("shadow_trade_timestamps_incomplete")
    observation_start = min(valid_opened) if valid_opened else None
    observation_end = max(valid_closed) if valid_closed else None
    weeks = (
        max(0.0, (observation_end - observation_start).total_seconds() / 604800)
        if observation_start and observation_end
        else 0.0
    )

    r_values = [item.get("r_multiple") for item in rows]
    if any(value is None for value in r_values):
        gaps.append("shadow_r_multiple_incomplete")
    complete_r = [float(value) for value in r_values if value is not None]
    net_values = [item.get("net_pnl") for item in rows]
    costs_complete = bool(rows) and all(value is not None for value in net_values)
    if rows and not costs_complete:
        gaps.append("shadow_net_pnl_or_fees_incomplete")
    complete_net = [float(value) for value in net_values if value is not None]
    profit = sum(max(0.0, value) for value in complete_net)
    loss = abs(sum(min(0.0, value) for value in complete_net))
    profit_factor = (
        profit / loss if costs_complete and loss > 0
        else float("inf") if costs_complete and profit > 0
        else None
    )

    complete_months = _complete_month_keys(observation_start, observation_end)
    monthly_pnl = {month: 0.0 for month in complete_months}
    if costs_complete:
        for item in rows:
            closed_at = _parse_time(item.get("closed_at"))
            if closed_at is not None and closed_at.strftime("%Y-%m") in monthly_pnl:
                monthly_pnl[closed_at.strftime("%Y-%m")] += float(item["net_pnl"])
    all_months_profitable = bool(monthly_pnl) and costs_complete and all(
        value > 0 for value in monthly_pnl.values()
    )

    plan_sources_complete = bool(rows)
    for item in rows:
        plan = store.get_plan(str(item.get("plan_id") or "")) or {}
        score = (plan.get("risk_snapshot") or {}).get("topdown_score") or {}
        if not (
            score.get("input_hash")
            and score.get("bar_closed_at")
            and not score.get("data_gaps")
            and not score.get("hard_blocks")
        ):
            plan_sources_complete = False
            break
    if rows and not plan_sources_complete:
        gaps.append("shadow_topdown_source_trace_incomplete")

    fixed_runs = [
        item
        for item in store.list_validation_runs(strategy_version=strategy_id, limit=20)
        if item.get("dataset") == "fixed_replay"
    ]
    mechanism_verified = any(
        item.get("status") == "complete"
        and not item.get("promotion_eligible")
        and (item.get("report") or {}).get("checks")
        and all(
            bool(check.get("passed"))
            for check in (item.get("report") or {}).get("checks", [])
        )
        for item in fixed_runs
    )
    if not mechanism_verified:
        gaps.append("fixed_execution_mechanism_validation_incomplete")

    rolling = complete_r[-60:]
    evidence = PerformanceEvidence(
        dataset="shadow",
        trade_count=len(rows),
        weeks=round(weeks, 6),
        expectancy_r=(sum(complete_r) / len(complete_r) if complete_r else None),
        profit_factor=profit_factor,
        profitable_month_ratio=(
            sum(value > 0 for value in monthly_pnl.values()) / len(monthly_pnl)
            if monthly_pnl and costs_complete
            else None
        ),
        complete_months=len(complete_months),
        all_complete_months_profitable=all_months_profitable,
        rolling_60_expectancy_r=(sum(rolling) / len(rolling) if len(rolling) == 60 else None),
        source_time_alignment_verified=plan_sources_complete,
        execution_rules_verified=mechanism_verified and costs_complete,
    )
    return evidence, list(dict.fromkeys(gaps))


class StrategyPromotionService:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.controller = StrategyStabilityController()

    def refresh_shadow_report(
        self, *, strategy_id: str = TOPDOWN_STRATEGY_ID
    ) -> PromotionEvidenceReport:
        evidence, gaps = build_shadow_performance_evidence(
            self.store, strategy_id=strategy_id
        )
        transition = self.controller.evaluate(StrategyState.SHADOW, evidence)
        eligible = transition.reasons == ["awaiting_explicit_live_activation_approval"]
        generated_at = datetime.now().astimezone().isoformat()
        payload = {
            "strategy_version": strategy_id,
            "dataset": "shadow",
            "performance_evidence": evidence.model_dump(mode="json"),
            "data_gaps": gaps,
        }
        report = PromotionEvidenceReport(
            strategy_version=strategy_id,
            dataset="shadow",
            status="complete" if not gaps else "data_incomplete",
            input_hash=_stable_hash(payload),
            performance_evidence=evidence.model_dump(mode="json"),
            promotion_eligible=eligible and not gaps,
            data_gaps=gaps,
            generated_at=generated_at,
        )
        self.store.add_validation_run(
            report,
            dataset="shadow",
            promotion_eligible=report.promotion_eligible,
        )
        return report

    def record_out_of_sample_report(
        self,
        report: Any,
        *,
        strategy_id: str = TOPDOWN_STRATEGY_ID,
    ) -> tuple[str, StateTransition]:
        """Persist one generated OOS report and apply only a proven promotion.

        The transition is derived again from the report's exact persisted
        performance evidence.  Callers cannot promote by passing a boolean or
        a second, mismatching evidence object.
        """
        payload = (
            report.model_dump(mode="json")
            if hasattr(report, "model_dump")
            else dict(report)
        )
        if payload.get("strategy_version") != strategy_id:
            raise ValueError("OOS report strategy does not match promotion target")
        from pa_agent.trading.oos_backtest import validate_oos_report_consistency

        evidence = validate_oos_report_consistency(report)
        transition = self.controller.evaluate(StrategyState.CANDIDATE, evidence)
        computed_eligible = (
            payload.get("status") == "complete"
            and not payload.get("data_gaps")
            and transition.current is StrategyState.SHADOW
            and transition.reasons == ["oos_gate_passed"]
        )
        if bool(payload.get("promotion_eligible")) != computed_eligible:
            raise ValueError("OOS report promotion flag does not match measured evidence")
        run_id = self.store.add_validation_run(
            report,
            dataset="out_of_sample",
            promotion_eligible=computed_eligible,
        )
        current = StrategyState(self.store.current_strategy_state(strategy_id))
        if computed_eligible:
            if current is not StrategyState.CANDIDATE:
                raise ValueError("OOS promotion requires current CANDIDATE state")
            self.store.record_strategy_transition(
                transition,
                evidence,
                strategy_id=strategy_id,
                validation_run_id=run_id,
            )
        return run_id, transition

    def activate_small_live(
        self,
        approval: LiveActivationApproval,
        *,
        strategy_id: str = TOPDOWN_STRATEGY_ID,
    ) -> StateTransition:
        current = StrategyState(self.store.current_strategy_state(strategy_id))
        if current is not StrategyState.SHADOW:
            raise ValueError("small-live activation requires current SHADOW state")
        report = self.refresh_shadow_report(strategy_id=strategy_id)
        if not report.promotion_eligible:
            raise ValueError("shadow promotion gate is not satisfied")
        evidence = PerformanceEvidence.model_validate(report.performance_evidence)
        transition = self.controller.evaluate(
            current, evidence, live_approval=approval
        )
        runs = self.store.list_validation_runs(
            strategy_version=strategy_id, limit=20
        )
        run = next(
            item
            for item in runs
            if item.get("dataset") == "shadow"
            and item.get("input_hash") == report.input_hash
        )
        self.store.record_strategy_transition(
            transition,
            evidence,
            strategy_id=strategy_id,
            validation_run_id=run["id"],
            live_approval=approval,
        )
        return transition


def _complete_month_keys(
    start: datetime | None, end: datetime | None
) -> list[str]:
    if start is None or end is None or end <= start:
        return []
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result: list[str] = []
    while cursor < end:
        days = monthrange(cursor.year, cursor.month)[1]
        next_month = (cursor + timedelta(days=days)).replace(day=1)
        if start <= cursor and end >= next_month:
            result.append(cursor.strftime("%Y-%m"))
        cursor = next_month
    return result


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
