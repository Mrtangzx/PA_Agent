"""Application workflow from deterministic signal to auditable shadow plan."""
from __future__ import annotations

import uuid
from typing import Any

from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.quant import (
    Hs300DailyPullbackStrategy,
    SignalDecision,
    SignalStatus,
    StrategyContext,
)
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID, TopDownScoreSnapshot


class QuantTradingWorkflow:
    def __init__(self, store: TradeStore, strategy: Hs300DailyPullbackStrategy) -> None:
        self.store = store
        self.strategy = strategy

    def evaluate(self, context: StrategyContext) -> dict[str, Any]:
        decision = self.strategy.evaluate(context)
        response: dict[str, Any] = {
            "signal_id": None,
            "decision_id": None,
            "plan_id": None,
            "decision": decision,
        }
        if decision.status is not SignalStatus.ALLOW:
            response["signal_id"] = self.store.add_quant_signal(decision)
            return response
        assert decision.trigger_price and decision.initial_stop
        risk = decision.trigger_price - decision.initial_stop
        target = decision.trigger_price + 2 * risk
        deterministic = decision.model_dump(mode="json")
        decision_id = self.store.add_decision(
            symbol=context.symbol,
            timeframe="1d",
            asset_class=AssetClass.A_SHARE.value,
            original_decision=deterministic,
            final_decision=deterministic,
            meta={
                "strategy_version": decision.strategy_id,
                "feature_version": decision.parameter_version,
                "model_name": "deterministic",
            },
            market_state="trend_pullback",
            confidence=None,
            audit_reason="deterministic_quant_signal; no_ai_execution_input",
        )
        plan = TradePlan(
            id=uuid.uuid4().hex,
            decision_event_id=decision_id,
            symbol=context.symbol,
            timeframe="1d",
            asset_class=AssetClass.A_SHARE,
            direction="buy",
            order_type="limit",
            entry_price=decision.trigger_price,
            stop_loss_price=decision.initial_stop,
            take_profit_price=target,
            valid_until=decision.valid_until,
            status=PlanStatus.PROPOSED,
            shadow_status="proposed",
            strategy_version=decision.strategy_id,
            risk_snapshot={
                "source": "deterministic_quant",
                "parameter_version": decision.parameter_version,
                "pool_version": decision.pool_version,
                "condition_snapshot": decision.condition_snapshot,
                "max_entry_price": decision.max_entry_price,
                "exit_rules": decision.exit_rules,
                "invalidation_rules": decision.invalidation_rules,
                "live_authorized": False,
            },
        )
        self.store.add_plan(plan)
        signal_id = self.store.add_quant_signal(decision, plan_id=plan.id)
        response.update({"signal_id": signal_id, "decision_id": decision_id, "plan_id": plan.id})
        return response

    def evaluate_topdown(
        self,
        context: StrategyContext,
        score: TopDownScoreSnapshot,
    ) -> dict[str, Any]:
        """Persist the two-stage daily candidate and 15m 4:3:2:1 gate.

        A plan is created only when the daily baseline allows the candidate and
        the frozen intraday score is eligible for portfolio risk authorization.
        """
        daily = self.strategy.evaluate(context)
        decision = daily.model_copy(update={
            "strategy_id": TOPDOWN_STRATEGY_ID,
            "parameter_version": f"{daily.parameter_version}+{score.scoring_version}",
            "condition_snapshot": {
                **daily.condition_snapshot,
                "daily_baseline_strategy": daily.strategy_id,
                "topdown_score": score.model_dump(mode="json"),
            },
        })
        response: dict[str, Any] = {
            "signal_id": None,
            "decision_id": None,
            "plan_id": None,
            "daily_decision": daily,
            "score": score,
            "decision": decision,
        }
        if daily.status is not SignalStatus.ALLOW:
            response["signal_id"] = self.store.add_quant_signal(decision)
            return response
        if score.symbol != context.symbol or score.pool_version != context.pool_version:
            decision = decision.model_copy(update={
                "status": SignalStatus.REJECT,
                "reasons": [*decision.reasons, "topdown_score_context_mismatch"],
            })
            response["decision"] = decision
            response["signal_id"] = self.store.add_quant_signal(decision)
            return response
        if not score.eligible_for_risk:
            decision = decision.model_copy(update={
                "status": SignalStatus.REJECT,
                "reasons": [
                    *decision.reasons,
                    f"topdown_{score.status.value}",
                    *score.hard_blocks,
                    *score.data_gaps,
                ],
            })
            response["decision"] = decision
            response["signal_id"] = self.store.add_quant_signal(decision)
            return response

        plan_result = self.create_topdown_plan(daily, score)
        response.update(plan_result)
        response["decision"] = plan_result["decision"]
        return response

    def create_topdown_plan(
        self,
        daily: SignalDecision,
        score: TopDownScoreSnapshot,
    ) -> dict[str, Any]:
        """Idempotently turn an eligible daily candidate into a research plan.

        This method never creates an ``AuthorizedOrder``.  Broker synchronization
        and portfolio authorization remain a separate, user-triggered boundary.
        """
        decision = daily.model_copy(update={
            "strategy_id": TOPDOWN_STRATEGY_ID,
            "parameter_version": f"{daily.parameter_version}+{score.scoring_version}",
            "condition_snapshot": {
                **daily.condition_snapshot,
                "daily_baseline_strategy": daily.strategy_id,
                "topdown_score": score.model_dump(mode="json"),
            },
        })
        response: dict[str, Any] = {
            "signal_id": None,
            "decision_id": None,
            "plan_id": None,
            "decision": decision,
        }
        if daily.status is not SignalStatus.ALLOW:
            decision = decision.model_copy(update={
                "status": SignalStatus.REJECT,
                "reasons": [*decision.reasons, "daily_candidate_not_allowed"],
            })
            response["decision"] = decision
            response["signal_id"] = self.store.add_quant_signal(decision)
            return response
        if score.symbol != daily.symbol or score.pool_version != daily.pool_version:
            decision = decision.model_copy(update={
                "status": SignalStatus.REJECT,
                "reasons": [*decision.reasons, "topdown_score_context_mismatch"],
            })
            response["decision"] = decision
            response["signal_id"] = self.store.add_quant_signal(decision)
            return response
        if not score.eligible_for_risk or score.hard_blocks or score.data_gaps:
            decision = decision.model_copy(update={
                "status": SignalStatus.REJECT,
                "reasons": [
                    *decision.reasons,
                    f"topdown_{score.status.value}",
                    *score.hard_blocks,
                    *score.data_gaps,
                ],
            })
            response["decision"] = decision
            response["signal_id"] = self.store.add_quant_signal(decision)
            return response
        for plan in self.store.list_plans(symbol=daily.symbol):
            stored = plan.get("risk_snapshot") or {}
            if (
                plan.get("strategy_version") == TOPDOWN_STRATEGY_ID
                and (stored.get("topdown_score") or {}).get("input_hash") == score.input_hash
            ):
                response.update({
                    "decision_id": plan.get("decision_event_id"),
                    "plan_id": plan.get("id"),
                })
                return response

        assert decision.trigger_price and decision.initial_stop
        risk = decision.trigger_price - decision.initial_stop
        target = decision.trigger_price + 2 * risk
        deterministic = decision.model_dump(mode="json")
        decision_id = self.store.add_decision(
            symbol=daily.symbol,
            timeframe="15m",
            asset_class=AssetClass.A_SHARE.value,
            original_decision=deterministic,
            final_decision=deterministic,
            meta={
                "strategy_version": TOPDOWN_STRATEGY_ID,
                "feature_version": decision.parameter_version,
                "model_name": "deterministic",
            },
            market_state="topdown_4321_intraday_gate",
            confidence=None,
            audit_reason="daily_candidate_then_closed_15m_topdown_gate; no_ai_execution_input",
        )
        plan = TradePlan(
            id=uuid.uuid4().hex,
            decision_event_id=decision_id,
            symbol=daily.symbol,
            timeframe="15m",
            asset_class=AssetClass.A_SHARE,
            direction="buy",
            order_type="limit",
            entry_price=decision.trigger_price,
            stop_loss_price=decision.initial_stop,
            take_profit_price=target,
            valid_until=decision.valid_until,
            status=PlanStatus.PROPOSED,
            shadow_status="proposed",
            strategy_version=TOPDOWN_STRATEGY_ID,
            risk_snapshot={
                "source": "deterministic_topdown_4321",
                "parameter_version": decision.parameter_version,
                "pool_version": decision.pool_version,
                "daily_condition_snapshot": daily.condition_snapshot,
                "entry_timeframe": "15m",
                "management_timeframe": "1d",
                "topdown_score": score.model_dump(mode="json"),
                "max_entry_price": decision.max_entry_price,
                "exit_rules": decision.exit_rules,
                "invalidation_rules": decision.invalidation_rules,
                "live_authorized": False,
            },
        )
        self.store.add_plan(plan)
        signal_id = self.store.add_quant_signal(decision, plan_id=plan.id)
        response.update({"signal_id": signal_id, "decision_id": decision_id, "plan_id": plan.id})
        return response
