"""Safe lifecycle handling when a top-down authorization is revoked."""
from __future__ import annotations

from typing import Any

from pa_agent.trading.hotspot_risk import (
    POSSIBLY_EXECUTED_STATUSES,
    PREFILLED_STATUSES,
    _authorized_order_from_events,
)


def apply_topdown_authorization_revocation(
    *,
    store: Any,
    score: Any,
    broker_adapter: Any | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for plan in store.list_plans(symbol=score.symbol):
        status = str(plan.get("status") or "")
        if status not in PREFILLED_STATUSES | POSSIBLY_EXECUTED_STATUSES:
            continue
        event_type = (
            "topdown_revocation_action_required"
            if status in POSSIBLY_EXECUTED_STATUSES
            else "topdown_authorization_revoked"
        )
        if any(
            event.get("event_type") == event_type
            and (event.get("details") or {}).get("score_input_hash")
            == score.input_hash
            for event in store.list_events(plan["id"])
        ):
            continue
        clear_receipt: dict[str, Any] = {}
        if status in PREFILLED_STATUSES:
            order = _authorized_order_from_events(store, plan["id"])
            if order is None:
                clear_receipt = {
                    "status": "not_cleared",
                    "message": "缺少可回溯授权订单，未触碰同花顺输入框",
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
            "score": score.total_score,
            "hard_blocks": score.hard_blocks,
            "data_gaps": score.data_gaps,
            "bar_closed_at": score.bar_closed_at,
            "score_input_hash": score.input_hash,
            "prefill_clear": clear_receipt,
        }
        if status in POSSIBLY_EXECUTED_STATUSES:
            details["required_action"] = (
                "核查同花顺真实委托/成交；不得把评分撤销等同于券商撤单"
            )
        else:
            store.update_plan(plan["id"], status="invalidated")
        store.append_event(plan["id"], event_type, details=details)
        actions.append({"plan_id": plan["id"], "event_type": event_type, **details})
    return actions
