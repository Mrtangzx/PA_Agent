"""Unified real/shadow lifecycle facade including broker reconciliation."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pa_agent.brokers.ths_adapter import ThsBrokerAdapter
from pa_agent.trading.broker_models import AuthorizedOrder, BrokerSnapshot
from pa_agent.trading.lifecycle import TradeLifecycleTracker


class LifecycleResult(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    reconciliation_status: str = "not_requested"
    broker_order_ids: list[str] = Field(default_factory=list)
    broker_fill_ids: list[str] = Field(default_factory=list)
    requires_user_action: bool = False


class TradeLifecycle:
    def __init__(self, tracker: TradeLifecycleTracker, broker: ThsBrokerAdapter) -> None:
        self.tracker = tracker
        self.broker = broker

    def process(
        self,
        *,
        plan: dict[str, Any],
        broker: BrokerSnapshot,
        closed_bar: Any,
        authorized_order: AuthorizedOrder | None = None,
    ) -> LifecycleResult:
        quote = broker.quote
        events = self.tracker.process_closed_bar(
            symbol=plan["symbol"],
            timeframe=plan["timeframe"],
            bar=closed_bar,
            quote_available=broker.connection.usable and quote is not None,
            suspended=bool(quote.suspended) if quote else False,
            price_limit_locked=bool(quote.limit_locked) if quote else False,
        )
        if authorized_order is None:
            return LifecycleResult(events=events)
        reconciliation = self.broker.reconcile(authorized_order, broker)
        return LifecycleResult(
            events=events,
            reconciliation_status=reconciliation.status,
            broker_order_ids=reconciliation.matched_order_ids,
            broker_fill_ids=reconciliation.matched_fill_ids,
            requires_user_action=reconciliation.status == "reconciliation_required",
        )

    @staticmethod
    def broker_order_status(order_status: str, filled: int, quantity: int) -> tuple[str, str]:
        """Map broker wording to an auditable plan/event state."""
        text = str(order_status or "").strip().casefold()
        if filled >= quantity > 0 or any(word in text for word in ("已成", "filled")):
            return "filled", "broker_filled"
        if filled > 0 or any(word in text for word in ("部成", "partial")):
            return "partially_filled", "broker_partial_fill"
        if any(word in text for word in ("已撤", "撤单", "cancel")):
            return "cancelled", "broker_cancelled"
        if any(word in text for word in ("废单", "拒绝", "reject", "invalid")):
            return "rejected", "broker_rejected"
        return "submitted", "broker_submitted"

    @staticmethod
    def external_manual_fills(
        broker: BrokerSnapshot,
        linked_fill_ids: set[str],
    ) -> list[str]:
        return [
            fill.broker_fill_id for fill in broker.fills
            if fill.broker_fill_id and fill.broker_fill_id not in linked_fill_ids
        ]
