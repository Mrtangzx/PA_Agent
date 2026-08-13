"""Conservative daily-bar A-share fill simulation."""
from __future__ import annotations

from pydantic import BaseModel, Field

from pa_agent.data.base import KlineBar
from pa_agent.trading.broker_models import AuthorizedOrder


class SimulatedFill(BaseModel):
    status: str
    price: float | None = None
    quantity: int = 0
    reason: str = ""
    stop_triggered: bool = False
    target_triggered: bool = False
    ambiguous_same_bar: bool = False
    t1_locked: bool = False
    slippage: float = Field(default=0, ge=0)


class AShareCosts(BaseModel):
    buy_commission: float = Field(ge=0)
    sell_commission: float = Field(ge=0)
    sell_tax: float = Field(ge=0)
    total: float = Field(ge=0)


class AShareCostModel(BaseModel):
    commission_rate: float = Field(default=0.00025, ge=0)
    minimum_commission: float = Field(default=5.0, ge=0)
    sell_tax_rate: float = Field(default=0.0005, ge=0)

    def calculate(
        self, *, entry_price: float, exit_price: float, quantity: int
    ) -> AShareCosts:
        buy_commission = max(
            self.minimum_commission, entry_price * quantity * self.commission_rate
        )
        sell_commission = max(
            self.minimum_commission, exit_price * quantity * self.commission_rate
        )
        sell_tax = exit_price * quantity * self.sell_tax_rate
        total = buy_commission + sell_commission + sell_tax
        return AShareCosts(
            buy_commission=round(buy_commission, 6),
            sell_commission=round(sell_commission, 6),
            sell_tax=round(sell_tax, 6),
            total=round(total, 6),
        )


class AShareExecutionSimulator:
    def process_entry(
        self,
        order: AuthorizedOrder,
        bar: KlineBar,
        *,
        suspended: bool = False,
        limit_locked: bool = False,
        max_price: float | None = None,
    ) -> SimulatedFill:
        if suspended:
            return SimulatedFill(status="blocked", reason="suspended")
        if limit_locked:
            return SimulatedFill(status="blocked", reason="price_limit_locked")
        if int(order.quantity) % 100:
            return SimulatedFill(status="blocked", reason="board_lot_violation")
        maximum = float(max_price) if max_price is not None else float(order.price)
        if float(bar.open) > maximum:
            return SimulatedFill(status="blocked", reason="gap_above_max_entry")
        if float(bar.open) >= order.price:
            return SimulatedFill(
                status="filled",
                price=float(bar.open),
                quantity=order.quantity,
                reason="gap_open_fill" if float(bar.open) > order.price else "trigger_fill",
                slippage=max(0.0, float(bar.open) - order.price),
            )
        if float(bar.high) < order.price:
            return SimulatedFill(status="not_filled", reason="trigger_not_reached")
        return SimulatedFill(
            status="filled", price=order.price, quantity=order.quantity,
            reason="trigger_fill",
        )

    def process_exit(
        self,
        *,
        entry_price: float,
        stop_price: float,
        target_price: float,
        quantity: int,
        bar: KlineBar,
        bought_same_day: bool = False,
        suspended: bool = False,
        limit_locked: bool = False,
    ) -> SimulatedFill:
        stop = float(bar.low) <= stop_price
        target = float(bar.high) >= target_price
        if bought_same_day and (stop or target):
            return SimulatedFill(
                status="blocked",
                reason="t_plus_one_locked",
                stop_triggered=stop,
                target_triggered=target,
                t1_locked=True,
            )
        if suspended or limit_locked:
            return SimulatedFill(
                status="blocked",
                reason="suspended" if suspended else "price_limit_locked",
                stop_triggered=stop,
                target_triggered=target,
            )
        if not stop and not target:
            return SimulatedFill(status="open")
        ambiguous = stop and target
        if stop:
            price = min(float(bar.open), stop_price) if float(bar.open) < stop_price else stop_price
            return SimulatedFill(
                status="filled",
                price=price,
                quantity=quantity,
                reason="stop_first_conservative" if ambiguous else "stop",
                stop_triggered=True,
                target_triggered=target,
                ambiguous_same_bar=ambiguous,
                slippage=max(0.0, stop_price - price),
            )
        return SimulatedFill(
            status="filled", price=target_price, quantity=quantity,
            reason="target", target_triggered=True,
        )
