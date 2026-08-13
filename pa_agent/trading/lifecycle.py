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
            risk_snapshot = plan.get("risk_snapshot") or {}
            entry_timeframe = str(
                risk_snapshot.get("entry_timeframe") or plan["timeframe"]
            )
            management_timeframe = str(
                risk_snapshot.get("management_timeframe") or plan["timeframe"]
            )
            shadow_is_open = plan.get("shadow_status") == "open"
            actual_is_open = plan.get("status") in {
                "partially_filled", "executed_open", "exit_detected",
            }
            expected_timeframe = (
                management_timeframe
                if shadow_is_open or actual_is_open
                else entry_timeframe
            )
            if expected_timeframe != timeframe:
                if (
                    shadow_is_open
                    and timeframe == entry_timeframe
                    and bool((risk_snapshot.get("exit_rules") or {}).get("t_plus_one", True))
                    and _date_part(str(plan.get("shadow_opened_at") or ""))
                    == _date_part(event_at)
                    and self.store.claim_lifecycle_bar(
                        plan_id=plan["id"],
                        timeframe=timeframe,
                        bar_closed_at=event_at,
                    )
                ):
                    active_stop = float(
                        plan.get("shadow_active_stop") or plan["stop_loss_price"]
                    )
                    long = _is_long(plan["direction"])
                    stop_touched = low <= active_stop if long else high >= active_stop
                    if stop_touched:
                        self.store.append_event(
                            plan["id"],
                            "t1_locked_breach",
                            dataset="shadow",
                            event_at=event_at,
                            price=active_stop,
                            details={
                                "state_unchanged": True,
                                "management_timeframe": management_timeframe,
                            },
                        )
                        emitted.append({
                            "plan_id": plan["id"],
                            "event_type": "t1_locked_breach",
                            "dataset": "shadow",
                        })
                continue
            if plan.get("created_at") and event_at <= plan["created_at"]:
                continue
            if not self.store.claim_lifecycle_bar(
                plan_id=plan["id"],
                timeframe=timeframe,
                bar_closed_at=event_at,
            ):
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
            emitted.extend(self._update_shadow(
                plan,
                low=low,
                high=high,
                open_price=float(getattr(bar, "open", None) or bar.close),
                close=float(bar.close),
                event_at=event_at,
                manage_on_this_bar=(timeframe == management_timeframe),
            ))
            emitted.extend(
                self._update_actual(
                    plan,
                    low=low,
                    high=high,
                    close=float(bar.close),
                    event_at=event_at,
                    price_limit_locked=price_limit_locked,
                )
            )
        return emitted

    def _update_shadow(
        self,
        plan: dict[str, Any],
        *,
        low: float,
        high: float,
        open_price: float,
        close: float,
        event_at: str,
        manage_on_this_bar: bool,
    ) -> list[dict[str, Any]]:
        status = plan["shadow_status"]
        if status in {"closed", "expired", "invalidated", "unknown"}:
            return []
        emitted: list[dict[str, Any]] = []
        entered_this_bar = False
        long = _is_long(plan["direction"])
        rules = dict((plan.get("risk_snapshot") or {}).get("exit_rules") or {})
        if status == "open" and plan.get("shadow_time_exit_pending"):
            return [self._close_shadow(
                plan,
                exit_price=open_price,
                event_at=event_at,
                event_type="time_exit_filled",
                holding_bars=int(plan.get("shadow_holding_bars") or 0),
            )]
        planned_entry = float(plan["entry_price"])
        max_entry_value = (plan.get("risk_snapshot") or {}).get("max_entry_price")
        max_entry = float(max_entry_value) if max_entry_value is not None else None
        gap_fill = bool(
            max_entry is not None and planned_entry <= open_price <= max_entry
        )
        if (
            status in {"proposed", "entry_touched"}
            and max_entry is not None
            and open_price > max_entry
        ):
            updates = {"shadow_status": "invalidated"}
            if plan["status"] == "proposed":
                updates["status"] = "invalidated"
            self.store.update_plan(plan["id"], **updates)
            self.store.append_event(
                plan["id"],
                "gap_above_max_entry",
                dataset="shadow",
                event_at=event_at,
                price=open_price,
                details={"max_entry_price": max_entry},
            )
            return [{
                "plan_id": plan["id"],
                "event_type": "gap_above_max_entry",
                "dataset": "shadow",
            }]
        entry_touched = gap_fill or low <= planned_entry <= high
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
            shadow_entry = open_price if gap_fill else planned_entry
            self.store.update_plan(
                plan["id"], shadow_status="open", shadow_entry_price=shadow_entry,
                shadow_opened_at=event_at,
                shadow_active_stop=plan["stop_loss_price"],
                shadow_highest_close=shadow_entry,
            )
            self.store.append_event(
                plan["id"], "entry_touched", dataset="shadow", event_at=event_at,
                price=shadow_entry,
                details={
                    "actual_fill_assumed": False,
                    "gap_open_fill": gap_fill,
                    "planned_trigger": planned_entry,
                },
            )
            plan = {
                **plan,
                "shadow_entry_price": shadow_entry,
                "shadow_active_stop": plan["stop_loss_price"],
                "shadow_highest_close": shadow_entry,
                "shadow_opened_at": event_at,
            }
            emitted.append({"plan_id": plan["id"], "event_type": "entry_touched", "dataset": "shadow"})
        if status != "open":
            return emitted
        if entered_this_bar and not manage_on_this_bar:
            return emitted

        if rules:
            emitted.extend(self._update_managed_shadow(
                plan,
                low=low,
                high=high,
                open_price=open_price,
                close=close,
                event_at=event_at,
                entered_this_bar=entered_this_bar,
                rules=rules,
            ))
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

    def _update_managed_shadow(
        self,
        plan: dict[str, Any],
        *,
        low: float,
        high: float,
        open_price: float,
        close: float,
        event_at: str,
        entered_this_bar: bool,
        rules: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply closed-bar exits without assuming an unknowable intrabar path."""
        long = _is_long(plan["direction"])
        entry = float(plan.get("shadow_entry_price") or plan["entry_price"])
        initial_stop = float(plan["stop_loss_price"])
        risk = abs(entry - initial_stop)
        active_stop = float(plan.get("shadow_active_stop") or initial_stop)
        hit_stop = low <= active_stop if long else high >= active_stop
        if hit_stop:
            if (
                entered_this_bar
                and plan["asset_class"] == "a_share"
                and bool(rules.get("t_plus_one", True))
            ):
                self.store.append_event(
                    plan["id"],
                    "t1_locked_breach",
                    dataset="shadow",
                    event_at=event_at,
                    price=active_stop,
                    details={"state_unchanged": True},
                )
            else:
                exit_price = (
                    min(open_price, active_stop)
                    if long
                    else max(open_price, active_stop)
                )
                event_type = (
                    "stop_detected"
                    if active_stop == initial_stop
                    else "trailing_stop_detected"
                )
                return [self._close_shadow(
                    plan,
                    exit_price=exit_price,
                    event_at=event_at,
                    event_type=event_type,
                    holding_bars=int(plan.get("shadow_holding_bars") or 0) + 1,
                )]

        favorable = high - entry if long else entry - low
        adverse = entry - low if long else high - entry
        holding = int(plan.get("shadow_holding_bars") or 0) + 1
        previous_mfe = float(plan.get("shadow_mfe") or 0)
        previous_mae = float(plan.get("shadow_mae") or 0)
        highest_close = (
            max(float(plan.get("shadow_highest_close") or entry), close)
            if long
            else min(float(plan.get("shadow_highest_close") or entry), close)
        )
        updated_stop = active_stop
        breakeven_after_r = float(rules.get("breakeven_after_r", 1.0))
        if risk > 0 and max(previous_mfe, favorable) >= breakeven_after_r * risk:
            updated_stop = max(updated_stop, entry) if long else min(updated_stop, entry)
            atr = _plan_atr(plan)
            trailing_atr = float(rules.get("trailing_atr", 2.0))
            if atr is not None and atr > 0:
                trailing_stop = (
                    highest_close - trailing_atr * atr
                    if long
                    else highest_close + trailing_atr * atr
                )
                updated_stop = (
                    max(updated_stop, trailing_stop)
                    if long
                    else min(updated_stop, trailing_stop)
                )
        self.store.update_plan(
            plan["id"],
            shadow_mfe=max(previous_mfe, favorable),
            shadow_mae=max(previous_mae, adverse),
            shadow_holding_bars=holding,
            shadow_active_stop=updated_stop,
            shadow_highest_close=highest_close,
        )
        if updated_stop != active_stop:
            self.store.append_event(
                plan["id"],
                "protective_stop_updated",
                dataset="shadow",
                event_at=event_at,
                price=updated_stop,
                details={
                    "previous_stop": active_stop,
                    "effective_from_next_bar": True,
                },
            )

        time_stop_bars = int(rules.get("time_stop_bars", 10))
        time_stop_min_r = float(rules.get("time_stop_min_r", 0.5))
        close_r = ((close - entry) if long else (entry - close)) / risk if risk else 0
        if holding >= time_stop_bars and close_r < time_stop_min_r:
            self.store.update_plan(plan["id"], shadow_time_exit_pending=1)
            self.store.append_event(
                plan["id"],
                "time_exit_scheduled",
                dataset="shadow",
                event_at=event_at,
                price=close,
                details={"execute_at_next_open": True, "close_r": close_r},
            )
            return [{
                "plan_id": plan["id"],
                "event_type": "time_exit_scheduled",
                "dataset": "shadow",
            }]
        return []

    def _close_shadow(
        self,
        plan: dict[str, Any],
        *,
        exit_price: float,
        event_at: str,
        event_type: str,
        holding_bars: int,
    ) -> dict[str, Any]:
        long = _is_long(plan["direction"])
        entry = float(plan.get("shadow_entry_price") or plan["entry_price"])
        risk = abs(entry - float(plan["stop_loss_price"]))
        profile = self.store.get_profile(plan["symbol"])
        quantity = 100 if plan["asset_class"] == "a_share" else 1
        multiplier = 1.0
        cost = None
        if plan["asset_class"] == "a_share":
            from pa_agent.trading.execution_simulator import AShareCostModel

            if profile is not None:
                quantity = profile.board_lot
            cost = AShareCostModel().calculate(
                entry_price=entry,
                exit_price=exit_price,
                quantity=quantity,
            ).total
        elif profile is not None:
            multiplier = float(profile.contract_multiplier or 1)
            from pa_agent.trading.risk import estimate_round_trip_cost

            cost = estimate_round_trip_cost(profile, price=entry, quantity=quantity)
        gross_pnl = (exit_price - entry) * (1 if long else -1) * quantity * multiplier
        net_pnl = gross_pnl - cost if cost is not None else None
        risk_amount = risk * quantity * multiplier
        updated = self.store.get_plan(plan["id"]) or plan
        result_value = net_pnl if net_pnl is not None else gross_pnl
        self.store.add_result(TradeResult(
            id=uuid.uuid4().hex,
            plan_id=plan["id"],
            dataset="shadow",
            outcome="win" if result_value > 0 else "loss" if result_value < 0 else "flat",
            entry_price=entry,
            exit_price=exit_price,
            quantity=quantity,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            r_multiple=(result_value / risk_amount if risk_amount else None),
            mfe_r=(float(updated.get("shadow_mfe") or 0) / risk if risk else None),
            mae_r=(float(updated.get("shadow_mae") or 0) / risk if risk else None),
            holding_bars=holding_bars,
            opened_at=updated.get("shadow_opened_at") or event_at,
            closed_at=event_at,
        ))
        self.store.update_plan(
            plan["id"],
            shadow_status="closed",
            shadow_time_exit_pending=0,
        )
        self.store.append_event(
            plan["id"],
            event_type,
            dataset="shadow",
            event_at=event_at,
            price=exit_price,
            details={"managed_exit_rules": True},
        )
        return {
            "plan_id": plan["id"],
            "event_type": event_type,
            "dataset": "shadow",
        }

    def _update_actual(
        self,
        plan: dict[str, Any],
        *,
        low: float,
        high: float,
        close: float,
        event_at: str,
        price_limit_locked: bool,
    ) -> list[dict[str, Any]]:
        if plan["status"] not in {"partially_filled", "executed_open", "exit_detected"}:
            return []
        long = _is_long(plan["direction"])
        execution = self.store.get_execution(plan["id"])
        rules = dict((plan.get("risk_snapshot") or {}).get("exit_rules") or {})
        active_stop = float(plan.get("actual_active_stop") or plan["stop_loss_price"])
        holding = int(plan.get("actual_holding_bars") or 0)
        if execution is not None:
            entry = float(execution["price"])
            favorable = high - entry if long else entry - low
            adverse = entry - low if long else high - entry
            holding += 1
            self.store.update_plan(
                plan["id"],
                actual_mfe=max(float(plan.get("actual_mfe") or 0), favorable),
                actual_mae=max(float(plan.get("actual_mae") or 0), adverse),
                actual_holding_bars=holding,
            )
        else:
            entry = float(plan["entry_price"])
        hit_stop = low <= active_stop if long else high >= active_stop
        hit_tp1 = (
            False if rules
            else high >= plan["take_profit_price"] if long
            else low <= plan["take_profit_price"]
        )
        hit_tp2 = False
        if plan.get("take_profit_price_2") is not None:
            hit_tp2 = high >= plan["take_profit_price_2"] if long else low <= plan["take_profit_price_2"]
        if not (hit_stop or hit_tp1 or hit_tp2):
            if rules and execution is not None:
                highest_close = (
                    max(float(plan.get("actual_highest_close") or entry), close)
                    if long
                    else min(float(plan.get("actual_highest_close") or entry), close)
                )
                risk = abs(entry - float(plan["stop_loss_price"]))
                updated_stop = active_stop
                mfe = max(
                    float(plan.get("actual_mfe") or 0),
                    high - entry if long else entry - low,
                )
                if risk > 0 and mfe >= float(rules.get("breakeven_after_r", 1.0)) * risk:
                    updated_stop = (
                        max(updated_stop, entry) if long else min(updated_stop, entry)
                    )
                    atr = _plan_atr(plan)
                    if atr is not None and atr > 0:
                        trailing = (
                            highest_close - float(rules.get("trailing_atr", 2.0)) * atr
                            if long
                            else highest_close + float(rules.get("trailing_atr", 2.0)) * atr
                        )
                        updated_stop = (
                            max(updated_stop, trailing)
                            if long
                            else min(updated_stop, trailing)
                        )
                self.store.update_plan(
                    plan["id"],
                    actual_active_stop=updated_stop,
                    actual_highest_close=highest_close,
                )
                if updated_stop != active_stop:
                    self.store.append_event(
                        plan["id"],
                        "actual_protective_stop_updated",
                        dataset="actual",
                        event_at=event_at,
                        price=updated_stop,
                        details={
                            "previous_stop": active_stop,
                            "effective_from_next_bar": True,
                        },
                    )
                close_r = (
                    ((close - entry) if long else (entry - close)) / risk
                    if risk else 0
                )
                if (
                    holding >= int(rules.get("time_stop_bars", 10))
                    and close_r < float(rules.get("time_stop_min_r", 0.5))
                ):
                    self.store.update_plan(
                        plan["id"],
                        status="exit_detected",
                        actual_time_exit_pending=1,
                    )
                    self.store.append_event(
                        plan["id"],
                        "time_exit_detected",
                        dataset="actual",
                        event_at=event_at,
                        price=close,
                        details={"requires_user_exit_confirmation": True},
                    )
                    return [{
                        "plan_id": plan["id"],
                        "event_type": "time_exit_detected",
                        "dataset": "actual",
                    }]
            return []

        event_type = (
            "stop_detected"
            if hit_stop and active_stop == float(plan["stop_loss_price"])
            else "trailing_stop_detected"
            if hit_stop
            else "tp2_detected"
            if hit_tp2
            else "tp1_detected"
        )
        price = (
            active_stop if hit_stop else
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


def _plan_atr(plan: dict[str, Any]) -> float | None:
    risk = plan.get("risk_snapshot") or {}
    snapshots = [
        risk.get("daily_condition_snapshot") or {},
        risk.get("condition_snapshot") or {},
    ]
    for snapshot in snapshots:
        value = snapshot.get("atr14")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _is_long(direction: Any) -> bool:
    text = str(direction or "").strip().lower()
    return "多" in text or text in {"long", "buy", "bull"}
