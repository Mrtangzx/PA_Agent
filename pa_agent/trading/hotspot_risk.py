"""Fail-closed handling for major hotspot risk events."""
from __future__ import annotations

from typing import Any

from pa_agent.trading.broker_models import AuthorizedOrder

UNEXECUTED_STATUSES = {"proposed", "triggered", "authorized"}
PREFILLED_STATUSES = {"awaiting_user_confirmation"}
POSSIBLY_EXECUTED_STATUSES = {
    "reconciliation_required",
    "submitted",
    "partially_filled",
    "filled",
    "executed_open",
    "exit_detected",
}
PENDING_RECONCILIATION_STATUSES = {
    "awaiting_user_confirmation",
    "reconciliation_required",
}


def apply_major_hotspot_risk(
    *,
    store: Any,
    snapshot: Any,
    broker_adapter: Any | None = None,
) -> list[dict[str, Any]]:
    """Invalidate only orders proven unexecuted and create auditable actions.

    A prefilled order is cleared only when the adapter can independently read
    back every field and prove it still belongs to this plan. Submitted or
    ambiguous orders are never labelled cancelled by inference.
    """
    if not snapshot.negative_blocks:
        return []
    actions: list[dict[str, Any]] = []
    for plan in store.list_plans(symbol=snapshot.symbol):
        status = str(plan.get("status") or "")
        if status not in UNEXECUTED_STATUSES | PREFILLED_STATUSES | POSSIBLY_EXECUTED_STATUSES:
            continue
        event_type = (
            "major_negative_action_required"
            if status in POSSIBLY_EXECUTED_STATUSES
            else "major_negative_invalidated"
        )
        if _event_already_recorded(store, plan["id"], event_type, snapshot.source_hash):
            continue
        clear_receipt: dict[str, Any] = {}
        if status in PREFILLED_STATUSES:
            order = _authorized_order_from_events(store, plan["id"])
            if order is None:
                clear_receipt = {
                    "status": "not_cleared",
                    "message": "缺少可回溯的授权订单，未触碰同花顺输入框",
                }
            elif broker_adapter is None:
                clear_receipt = {
                    "status": "not_cleared",
                    "message": "同花顺适配器不可用，未触碰输入框",
                }
            else:
                receipt = broker_adapter.clear_prefill_if_matches(order)
                clear_receipt = receipt.model_dump(mode="json")
        details = {
            "previous_status": status,
            "negative_blocks": snapshot.negative_blocks,
            "hotspot_source_hash": snapshot.source_hash,
            "frozen_at": snapshot.frozen_at,
            "prefill_clear": clear_receipt,
        }
        if status in POSSIBLY_EXECUTED_STATUSES:
            details["required_action"] = (
                "核查同花顺真实委托/成交；如已有持仓，按退出规则和T+1约束管理"
            )
        else:
            store.update_plan(plan["id"], status="invalidated")
        store.append_event(plan["id"], event_type, details=details)
        actions.append({"plan_id": plan["id"], "event_type": event_type, **details})
    return actions


def _authorized_order_from_events(store: Any, plan_id: str) -> AuthorizedOrder | None:
    for event in reversed(store.list_events(plan_id)):
        raw = (event.get("details") or {}).get("authorized_order")
        if raw:
            try:
                return AuthorizedOrder.model_validate(raw)
            except Exception:  # noqa: BLE001 - corrupt audit input fails closed
                return None
    return None


def pending_reconciliation_order_ids(store: Any, snapshot: Any) -> set[str]:
    """Protect only orders that can still match one unresolved authorized plan.

    A broad symbol/direction exclusion would hide unrelated manual trades in
    the same stock.  This helper uses the same deterministic bounded key as
    broker reconciliation: account, symbol, direction, price, quantity and
    the 60-second authorization window.
    """
    from pa_agent.brokers.ths_adapter import matching_broker_orders

    protected: set[str] = set()
    for plan in store.list_plans(statuses=sorted(PENDING_RECONCILIATION_STATUSES)):
        order = _authorized_order_from_events(store, str(plan.get("id") or ""))
        if order is None or order.account_fingerprint != snapshot.account_fingerprint:
            continue
        protected.update(
            item.broker_order_id
            for item in matching_broker_orders(snapshot, order)
            if item.broker_order_id
        )
    return protected


def _event_already_recorded(
    store: Any, plan_id: str, event_type: str, source_hash: str
) -> bool:
    return any(
        event.get("event_type") == event_type
        and (event.get("details") or {}).get("hotspot_source_hash") == source_hash
        for event in store.list_events(plan_id)
    )
