"""Application-facing facade that keeps analysis and trading persistence decoupled."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta
from typing import Any

from pa_agent.trading.models import AssetClass, PlanStatus, RiskSettings, TradePlan
from pa_agent.trading.profiles import default_profile
from pa_agent.trading.risk import calculate_position_size, estimate_round_trip_cost
from pa_agent.trading.store import TradeStore
from pa_agent.util.trade_metrics import compute_expectancy, compute_risk_reward

_ORDER_TYPES = {"限价单", "突破单", "市价单", "limit", "stop", "market"}


class TradingService:
    def __init__(self, store: TradeStore, risk_settings: RiskSettings | None = None) -> None:
        self.store = store
        self.risk_settings = risk_settings or RiskSettings()

    def persist_stage2_decision(
        self,
        *,
        decision_inner: dict[str, Any],
        model_original_decision: dict[str, Any] | None = None,
        stage2_full: dict[str, Any],
        symbol: str,
        timeframe: str,
        data_source: str,
        record_meta: dict[str, Any],
        analysis_record_ref: str = "",
        current_open_risk: float | None = None,
    ) -> dict[str, Any]:
        """Audit every decision, then create a shadow plan when it is structurally executable."""
        original = copy.deepcopy(model_original_decision or decision_inner)
        final = copy.deepcopy(decision_inner)
        profile = self.store.get_profile(symbol) if self.store.available else None
        if profile is None:
            profile = default_profile(
                symbol, data_source, str(record_meta.get("adjustment_mode", "")),
            )
        reasons: list[str] = []
        adjustments = _price_adjustments(original, final)
        if profile.asset_class is AssetClass.A_SHARE and _is_short(final.get("order_direction")):
            final["order_type"] = "不下单"
            final["asset_rule_conflict"] = "A股首期禁止做空"
            reasons.append("A股首期禁止做空；方案已转为不下单")

        prices = _plan_prices(final)
        actionable = str(final.get("order_type", "")).strip() in _ORDER_TYPES and prices is not None
        if actionable and not _has_target_basis(final):
            actionable = False
            final["order_type"] = "不下单"
            final["target_rule_conflict"] = "TP1/TP2 缺少可审计的结构依据"
            reasons.append("TP1/TP2 缺少支撑阻力、摆动点、区间边界或测量移动依据；拒绝建立计划")
        risk_snapshot: dict[str, Any] = {}
        if actionable and prices is not None:
            if current_open_risk is None:
                current_open_risk = sum(
                    float((plan.get("risk_snapshot") or {}).get("planned_risk") or 0)
                    for plan in self.store.list_plans(statuses=["executed_open", "exit_detected"])
                )
            risk_snapshot = calculate_position_size(
                entry_price=prices[0], stop_loss_price=prices[1], profile=profile,
                settings=self.risk_settings, current_open_risk=current_open_risk,
            )
            self._apply_realized_loss_guards(risk_snapshot)
            metrics = _expectancy_metrics(final, profile, prices)
            final["program_trade_metrics"] = metrics
            risk_snapshot["expectancy"] = metrics
            final["risk_suggestion"] = copy.deepcopy(risk_snapshot)

        diagnosis = stage2_full.get("diagnosis_summary") or {}
        confidence = _float_or_none(final.get("trade_confidence"))
        decision_id = self.store.add_decision(
            symbol=symbol, timeframe=timeframe, asset_class=profile.asset_class.value,
            original_decision=original, final_decision=final, meta=record_meta,
            market_state=str(diagnosis.get("cycle_position") or diagnosis.get("market_phase") or ""),
            confidence=confidence, analysis_record_ref=analysis_record_ref,
            price_adjustments=adjustments, audit_reason="; ".join(reasons),
        )
        response: dict[str, Any] = {
            "decision_id": decision_id, "final_decision": final, "plan_id": None,
            "risk_snapshot": risk_snapshot,
        }
        if not actionable or prices is None:
            return response

        plan = TradePlan(
            id=uuid.uuid4().hex, decision_event_id=decision_id,
            analysis_record_ref=analysis_record_ref, symbol=symbol, timeframe=timeframe,
            asset_class=profile.asset_class, direction=str(final.get("order_direction", "")),
            order_type=str(final.get("order_type", "")), entry_price=prices[0],
            stop_loss_price=prices[1], take_profit_price=prices[2],
            take_profit_price_2=_float_or_none(final.get("take_profit_price_2")),
            valid_until=str(final.get("valid_until") or final.get("expiry") or ""),
            status=PlanStatus.PROPOSED, shadow_status="proposed",
            strategy_version=str(record_meta.get("strategy_version", "")), risk_snapshot=risk_snapshot,
        )
        self.store.add_plan(plan)
        response["plan_id"] = plan.id
        response["risk_snapshot"] = risk_snapshot
        return response

    def _apply_realized_loss_guards(self, risk_snapshot: dict[str, Any]) -> None:
        equity = self.risk_settings.account_equity
        if equity is None or risk_snapshot.get("quantity") is None:
            return
        now = datetime.now().astimezone()
        day_loss = 0.0
        week_loss = 0.0
        for result in self.store.list_results(dataset="actual"):
            pnl = float(result.get("net_pnl") or 0)
            if pnl >= 0:
                continue
            try:
                closed = datetime.fromisoformat(result.get("closed_at") or "")
            except ValueError:
                continue
            if closed.date() == now.date():
                day_loss += -pnl
            if closed >= now - timedelta(days=7):
                week_loss += -pnl
        warnings = list(risk_snapshot.get("warnings") or [])
        if day_loss >= equity * self.risk_settings.daily_loss_warning_pct / 100:
            warnings.append("daily_realized_loss_warning")
        if week_loss >= equity * self.risk_settings.weekly_loss_warning_pct / 100:
            warnings.append("weekly_realized_loss_warning")
        if warnings:
            risk_snapshot["warnings"] = warnings
        if any(item.endswith("realized_loss_warning") for item in warnings):
            risk_snapshot["quantity"] = None
            risk_snapshot["status"] = "blocked"


def analysis_record_reference(record: Any) -> str:
    meta = getattr(record, "meta", None)
    if meta is None:
        return ""
    try:
        from pa_agent.config.paths import RECORDS_PENDING_DIR
        from pa_agent.records.pending_writer import _build_basename

        return str((RECORDS_PENDING_DIR / f"{_build_basename(record)}.json").resolve())
    except Exception:  # noqa: BLE001
        timestamp = getattr(meta, "timestamp_local_iso", "")
        symbol = getattr(meta, "symbol", "")
        timeframe = getattr(meta, "timeframe", "")
        return f"{timestamp}|{symbol}|{timeframe}"


def _plan_prices(decision: dict[str, Any]) -> tuple[float, float, float] | None:
    try:
        entry = float(decision["entry_price"])
        stop = float(decision["stop_loss_price"])
        target = float(decision["take_profit_price"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(entry, stop, target) <= 0 or entry == stop:
        return None
    return entry, stop, target


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_short(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return "空" in text or text in {"short", "sell", "bear"}


def _price_adjustments(original: dict[str, Any], final: dict[str, Any]) -> list[dict[str, Any]]:
    adjustments: list[dict[str, Any]] = []
    for field in ("entry_price", "stop_loss_price", "take_profit_price", "take_profit_price_2"):
        before = original.get(field)
        after = final.get(field)
        if before != after:
            adjustments.append({
                "field": field, "model_value": before, "program_value": after,
                "reason": "program_normalization",
            })
    return adjustments


def _expectancy_metrics(
    decision: dict[str, Any],
    profile: Any,
    prices: tuple[float, float, float],
) -> dict[str, Any]:
    rr = compute_risk_reward(prices[0], prices[2], prices[1], decision.get("order_direction"))
    win_rate = _float_or_none(decision.get("estimated_win_rate"))
    if rr is None or win_rate is None:
        return {"gross_expectancy": None, "net_expectancy": None, "win_rate_label": "未校准主观估计"}
    quantity = profile.board_lot if profile.asset_class is AssetClass.A_SHARE else 1
    multiplier = profile.contract_multiplier if profile.asset_class is AssetClass.CN_FUTURES else 1
    multiplier = float(multiplier or 1)
    gross_unit = compute_expectancy(win_rate, float(rr["risk"]), float(rr["reward"]))
    gross = float(gross_unit["gross_expectancy"]) * quantity * multiplier
    cost = estimate_round_trip_cost(profile, price=prices[0], quantity=quantity)
    return {
        "gross_expectancy": gross,
        "net_expectancy": gross - cost if cost is not None else None,
        "estimated_cost": cost,
        "basis_quantity": quantity,
        "win_rate_pct": win_rate,
        "win_rate_label": "未校准主观估计",
        "costs_configured": cost is not None,
    }


def _has_target_basis(decision: dict[str, Any]) -> bool:
    basis_1 = str(decision.get("take_profit_basis") or "").strip()
    basis_2 = str(decision.get("take_profit_basis_2") or "").strip()
    if basis_1 and basis_2:
        return True
    evidence = " ".join([
        str(decision.get("reasoning") or ""),
        " ".join(str(item) for item in decision.get("key_factors") or []),
        " ".join(str(item) for item in decision.get("watch_points") or []),
    ]).lower()
    keywords = ("支撑", "阻力", "摆动", "前高", "前低", "区间", "边界", "通道", "测量", "swing", "measured move", " mm")
    return any(keyword in evidence for keyword in keywords)
