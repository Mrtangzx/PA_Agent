"""Closed-bar lifecycle processing for shadow plans and actual positions."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pa_agent.trading.models import TradeResult
from pa_agent.trading.store import TradeStore


class TradeLifecycleTracker:
    def __init__(self, store: TradeStore) -> None:
        self.store = store

    def process_closed_bar(
        self,
        *,
        symbol: str,
        timeframe: str,
        bar: Any,
        quote_available: bool = True,
        suspended: bool = False,
        price_limit_locked: bool = False,
    ) -> list[dict[str, Any]]:
        """Apply one newly closed bar and return user-visible events."""
        if not self.store.available:
            return []
        event_at = _bar_time(bar)
        if not quote_available or suspended:
            events: list[dict[str, Any]] = []
            for plan in self.store.list_plans(symbol=symbol, lifecycle_open=True):
                self.store.append_event(
                    plan["id"], "quote_unavailable", event_at=event_at,
                    details={"suspended": suspended},
                )
                events.append({"plan_id": plan["id"], "event_type": "quote_unavailable"})
            return events

        low, high = float(bar.low), float(bar.high)
        emitted: list[dict[str, Any]] = []
        for plan in self.store.list_plans(symbol=symbol, lifecycle_open=True):
            if plan["timeframe"] != timeframe:
                continue
            if plan.get("created_at") and event_at <= plan["created_at"]:
                continue
            self.store.update_plan(plan["id"], last_price=float(bar.close), last_bar_at=event_at)
            if price_limit_locked:
                self.store.append_event(
                    plan["id"], "price_limit_blocked", event_at=event_at,
                    details={"state_unchanged": True},
                )
                emitted.append({"plan_id": plan["id"], "event_type": "price_limit_blocked"})
                continue
            if _is_after(event_at, plan["valid_until"]) and plan["shadow_status"] == "proposed":
                updates = {"shadow_status": "expired"}
                if plan["status"] == "proposed":
                    updates["status"] = "expired"
                self.store.update_plan(plan["id"], **updates)
                self.store.append_event(plan["id"], "expired", dataset="shadow", event_at=event_at)
                emitted.append({"plan_id": plan["id"], "event_type": "expired", "dataset": "shadow"})
                continue
            emitted.extend(self._update_shadow(plan, low=low, high=high, event_at=event_at))
            emitted.extend(
                self._update_actual(
                    plan, low=low, high=high, event_at=event_at,
                    price_limit_locked=price_limit_locked,
                )
            )
        return emitted

    def _update_shadow(self, plan: dict[str, Any], *, low: float, high: float, event_at: str) -> list[dict[str, Any]]:
        status = plan["shadow_status"]
        if status in {"closed", "expired", "invalidated", "unknown"}:
            return []
        emitted: list[dict[str, Any]] = []
        entered_this_bar = False
        long = _is_long(plan["direction"])
        entry_touched = low <= plan["entry_price"] <= high
        stop_touched = low <= plan["stop_loss_price"] if long else high >= plan["stop_loss_price"]
        if status in {"proposed", "entry_touched"} and stop_touched and not entry_touched:
            updates = {"shadow_status": "invalidated"}
            if plan["status"] == "proposed":
                updates["status"] = "invalidated"
            self.store.update_plan(plan["id"], **updates)
            self.store.append_event(
                plan["id"], "invalidated", dataset="shadow", event_at=event_at,
                price=plan["stop_loss_price"], details={"stop_touched_before_entry": True},
            )
            return [{"plan_id": plan["id"], "event_type": "invalidated", "dataset": "shadow"}]
        if status in {"proposed", "entry_touched"} and entry_touched:
            entered_this_bar = True
            status = "open"
            self.store.update_plan(
                plan["id"], shadow_status="open", shadow_entry_price=plan["entry_price"],
                shadow_opened_at=event_at,
            )
            self.store.append_event(
                plan["id"], "entry_touched", dataset="shadow", event_at=event_at,
                price=plan["entry_price"], details={"actual_fill_assumed": False},
            )
            emitted.append({"plan_id": plan["id"], "event_type": "entry_touched", "dataset": "shadow"})
        if status != "open":
            return emitted

        risk = abs(plan["entry_price"] - plan["stop_loss_price"])
        favorable = high - plan["entry_price"] if long else plan["entry_price"] - low
        adverse = plan["entry_price"] - low if long else high - plan["entry_price"]
        holding = int(plan.get("shadow_holding_bars") or 0) + 1
        self.store.update_plan(
            plan["id"], shadow_mfe=max(float(plan.get("shadow_mfe") or 0), favorable),
            shadow_mae=max(float(plan.get("shadow_mae") or 0), adverse),
            shadow_holding_bars=holding,
        )
        hit_stop = low <= plan["stop_loss_price"] if long else high >= plan["stop_loss_price"]
        hit_target = high >= plan["take_profit_price"] if long else low <= plan["take_profit_price"]
        if not hit_stop and not hit_target:
            return emitted

        ambiguous = bool(hit_stop and hit_target)
        # Conservative rule: stop wins whenever bar ordering is unknowable.
        outcome = "loss" if hit_stop else "win"
        exit_price = plan["stop_loss_price"] if hit_stop else plan["take_profit_price"]
        profile = self.store.get_profile(plan["symbol"])
        quantity = 100 if plan["asset_class"] == "a_share" else 1
        multiplier = 1.0
        cost = None
        if profile is not None:
            quantity = profile.board_lot if plan["asset_class"] == "a_share" else 1
            multiplier = float(profile.contract_multiplier or 1)
            from pa_agent.trading.risk import estimate_round_trip_cost

            cost = estimate_round_trip_cost(profile, price=plan["entry_price"], quantity=quantity)
        gross_pnl = (exit_price - plan["entry_price"]) * (1 if long else -1) * quantity * multiplier
        net_pnl = gross_pnl - cost if cost is not None else None
        risk_amount = risk * quantity * multiplier
        r_multiple = (net_pnl if net_pnl is not None else gross_pnl) / risk_amount
        updated = self.store.get_plan(plan["id"]) or plan
        self.store.add_result(TradeResult(
            id=uuid.uuid4().hex, plan_id=plan["id"], dataset="shadow", outcome=outcome,
            entry_price=plan["entry_price"], exit_price=exit_price, quantity=quantity,
            gross_pnl=gross_pnl, net_pnl=net_pnl,
            r_multiple=r_multiple,
            mfe_r=(float(updated.get("shadow_mfe") or 0) / risk if risk else None),
            mae_r=(float(updated.get("shadow_mae") or 0) / risk if risk else None),
            holding_bars=int(updated.get("shadow_holding_bars") or holding),
            ambiguous_same_bar=ambiguous, opened_at=updated.get("shadow_opened_at") or event_at,
            closed_at=event_at,
        ))
        self.store.update_plan(plan["id"], shadow_status="closed")
        event_type = "stop_detected" if hit_stop else "tp1_detected"
        self.store.append_event(
            plan["id"], event_type, dataset="shadow", event_at=event_at, price=exit_price,
            details={"ambiguous_same_bar": ambiguous, "entered_this_bar": entered_this_bar},
        )
        emitted.append({
            "plan_id": plan["id"], "event_type": event_type, "dataset": "shadow",
            "ambiguous_same_bar": ambiguous,
        })
        return emitted

    def _update_actual(
        self,
        plan: dict[str, Any],
        *,
        low: float,
        high: float,
        event_at: str,
        price_limit_locked: bool,
    ) -> list[dict[str, Any]]:
        if plan["status"] not in {"executed_open", "exit_detected"}:
            return []
        long = _is_long(plan["direction"])
        execution = self.store.get_execution(plan["id"])
        if execution is not None:
            entry = float(execution["price"])
            favorable = high - entry if long else entry - low
            adverse = entry - low if long else high - entry
            self.store.update_plan(
                plan["id"],
                actual_mfe=max(float(plan.get("actual_mfe") or 0), favorable),
                actual_mae=max(float(plan.get("actual_mae") or 0), adverse),
                actual_holding_bars=int(plan.get("actual_holding_bars") or 0) + 1,
            )
        hit_stop = low <= plan["stop_loss_price"] if long else high >= plan["stop_loss_price"]
        hit_tp1 = high >= plan["take_profit_price"] if long else low <= plan["take_profit_price"]
        hit_tp2 = False
        if plan.get("take_profit_price_2") is not None:
            hit_tp2 = high >= plan["take_profit_price_2"] if long else low <= plan["take_profit_price_2"]
        if not (hit_stop or hit_tp1 or hit_tp2):
            return []

        event_type = "stop_detected" if hit_stop else "tp2_detected" if hit_tp2 else "tp1_detected"
        price = (
            plan["stop_loss_price"] if hit_stop else
            plan["take_profit_price_2"] if hit_tp2 else plan["take_profit_price"]
        )
        if plan["asset_class"] == "a_share" and self._a_share_t1_locked(plan["id"], event_at):
            self.store.append_event(
                plan["id"], "t1_locked_breach", dataset="actual", event_at=event_at,
                price=price, details={"detected_event": event_type},
            )
            return [{"plan_id": plan["id"], "event_type": "t1_locked_breach", "dataset": "actual"}]
        if price_limit_locked:
            self.store.append_event(
                plan["id"], "price_limit_blocked", dataset="actual", event_at=event_at,
                price=price, details={"detected_event": event_type},
            )
            return [{"plan_id": plan["id"], "event_type": "price_limit_blocked", "dataset": "actual"}]
        if plan["status"] != "exit_detected":
            self.store.update_plan(plan["id"], status="exit_detected")
            self.store.append_event(
                plan["id"], event_type, dataset="actual", event_at=event_at, price=price,
                details={"requires_user_exit_confirmation": True},
            )
        return [{"plan_id": plan["id"], "event_type": event_type, "dataset": "actual"}]

    def _a_share_t1_locked(self, plan_id: str, event_at: str) -> bool:
        events = self.store.list_events(plan_id)
        executions = [event for event in events if event["event_type"] == "executed"]
        if not executions:
            return False
        return _date_part(executions[-1]["event_at"]) == _date_part(event_at)


def _bar_time(bar: Any) -> str:
    value = getattr(bar, "ts_open", None)
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds).astimezone().isoformat()
    return datetime.now().astimezone().isoformat()


def _date_part(value: str) -> str:
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10]


def _is_after(event_at: str, valid_until: str) -> bool:
    if not valid_until:
        return False
    try:
        return datetime.fromisoformat(event_at) > datetime.fromisoformat(valid_until)
    except ValueError:
        return False


def _is_long(direction: Any) -> bool:
    text = str(direction or "").strip().lower()
    return "多" in text or text in {"long", "buy", "bull"}
