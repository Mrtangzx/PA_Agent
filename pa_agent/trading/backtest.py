"""Event-driven, conservative daily-bar validation for deterministic strategies."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field

from pa_agent.data.base import KlineBar
from pa_agent.trading.broker_models import AuthorizedOrder
from pa_agent.trading.execution_simulator import AShareCostModel, AShareExecutionSimulator
from pa_agent.trading.quant import Hs300DailyPullbackStrategy, SignalStatus, StrategyContext


class BacktestTrade(BaseModel):
    symbol: str
    signal_time: str
    entered_at: str
    exited_at: str
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    net_pnl: float
    r_multiple: float
    holding_bars: int
    exit_reason: str


class BacktestReport(BaseModel):
    strategy_id: str
    dataset: str
    signal_count: int
    trade_count: int
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_pct: float | None
    profitable_month_ratio: float | None
    monthly_returns: dict[str, float] = Field(default_factory=dict)
    trades: list[BacktestTrade] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EventDrivenBacktester:
    """Walk-forward engine: signal uses bar N, entry starts at bar N+1."""

    def __init__(self, strategy: Hs300DailyPullbackStrategy) -> None:
        self.strategy = strategy
        self.simulator = AShareExecutionSimulator()

    def run_symbol(
        self,
        *,
        symbol: str,
        bars: Sequence[KlineBar],
        index_bars: Sequence[KlineBar],
        breadth_by_time: dict[str, float],
        pool_membership_by_time: dict[str, set[str]],
        pool_version_by_time: dict[str, str],
        initial_equity: float = 1_000_000,
        risk_pct: float = 0.25,
        commission_rate: float = 0.00025,
        minimum_commission: float = 5.0,
        sell_tax_rate: float = 0.0005,
        slippage_rate: float = 0.0005,
        dataset: str = "out_of_sample",
    ) -> BacktestReport:
        if len(bars) != len(index_bars):
            raise ValueError("stock and index bars must be date-aligned")
        trades: list[BacktestTrade] = []
        cost_model = AShareCostModel(
            commission_rate=commission_rate,
            minimum_commission=minimum_commission,
            sell_tax_rate=sell_tax_rate,
        )
        signal_count = 0
        equity = initial_equity
        equity_curve = [equity]
        index = 65
        while index < len(bars) - 1:
            signal_bar = bars[index]
            key = _date_key(signal_bar)
            members = pool_membership_by_time.get(key)
            if members is None:
                index += 1
                continue
            context = StrategyContext(
                symbol=symbol,
                bars=tuple(bars[: index + 1]),
                index_bars=tuple(index_bars[: index + 1]),
                market_breadth_pct=float(breadth_by_time.get(key, -1)),
                pool_version=pool_version_by_time.get(key, key[:7]),
                signal_time=_bar_iso(signal_bar),
                next_trading_time=_bar_iso(bars[index + 1]),
                eligible=symbol in members,
                eligibility_reasons=() if symbol in members else ("not_in_historical_pool",),
            )
            decision = self.strategy.evaluate(context)
            if decision.status is not SignalStatus.ALLOW:
                index += 1
                continue
            signal_count += 1
            assert decision.trigger_price and decision.initial_stop
            risk_per_share = decision.trigger_price - decision.initial_stop
            quantity = int((equity * risk_pct / 100) // risk_per_share // 100) * 100
            if quantity <= 0:
                index += 1
                continue
            order = AuthorizedOrder(
                plan_id=f"backtest-{symbol}-{key}", account_fingerprint="backtest",
                symbol=symbol, direction="buy", price=decision.trigger_price,
                quantity=quantity, stop_loss_price=decision.initial_stop,
                strategy_id=decision.strategy_id, authorized_at=decision.signal_time,
                expires_at=decision.valid_until,
            )
            next_bar = bars[index + 1]
            max_entry = decision.max_entry_price or decision.trigger_price
            if float(next_bar.open) > max_entry:
                index += 1
                continue
            entry = self.simulator.process_entry(order, next_bar, max_price=max_entry)
            if entry.status != "filled" or entry.price is None:
                index += 1
                continue
            entry_price = entry.price * (1 + slippage_rate)
            stop = decision.initial_stop
            exit_price = None
            exit_reason = ""
            holding = 0
            maximum_close = entry_price
            for exit_index in range(index + 1, min(len(bars), index + 12)):
                holding += 1
                current = bars[exit_index]
                maximum_close = max(maximum_close, float(current.close))
                if maximum_close >= entry_price + (entry_price - stop):
                    stop = max(stop, entry_price)
                simulated = self.simulator.process_exit(
                    entry_price=entry_price,
                    stop_price=stop,
                    # The production strategy has no fixed 2R take-profit.
                    # It protects at 1R, then exits through the 2ATR trail or
                    # the time stop.  Infinity keeps this simulator focused on
                    # the currently effective protective stop.
                    target_price=float("inf"),
                    quantity=quantity,
                    bar=current,
                    bought_same_day=exit_index == index + 1,
                )
                if simulated.status == "filled" and simulated.price is not None:
                    exit_price = simulated.price * (1 - slippage_rate)
                    exit_reason = simulated.reason
                    break
                if holding >= self.strategy.settings.time_stop_bars:
                    current_r = (float(current.close) - entry_price) / (entry_price - decision.initial_stop)
                    if current_r < self.strategy.settings.time_stop_min_r:
                        exit_price = float(current.close) * (1 - slippage_rate)
                        exit_reason = "time_stop"
                        break
                trailing = maximum_close - self.strategy.settings.trailing_atr * float(
                    decision.condition_snapshot["atr14"]
                )
                stop = max(stop, trailing)
            if exit_price is None:
                exit_index = min(len(bars) - 1, index + 11)
                exit_price = float(bars[exit_index].close) * (1 - slippage_rate)
                exit_reason = "backtest_window_end"
            gross = (exit_price - entry_price) * quantity
            costs = cost_model.calculate(
                entry_price=entry_price, exit_price=exit_price, quantity=quantity
            )
            net = gross - costs.total
            risk_amount = (entry_price - decision.initial_stop) * quantity
            trade = BacktestTrade(
                symbol=symbol,
                signal_time=decision.signal_time,
                entered_at=_bar_iso(next_bar),
                exited_at=_bar_iso(bars[exit_index]),
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                gross_pnl=gross,
                net_pnl=net,
                r_multiple=net / risk_amount,
                holding_bars=holding,
                exit_reason=exit_reason,
            )
            trades.append(trade)
            equity += net
            equity_curve.append(equity)
            index = exit_index + 1

        monthly_pnl: dict[str, float] = {}
        for trade in trades:
            month = trade.exited_at[:7]
            monthly_pnl[month] = monthly_pnl.get(month, 0.0) + trade.net_pnl
        monthly_returns = {
            month: pnl / initial_equity * 100 for month, pnl in sorted(monthly_pnl.items())
        }
        r_values = [trade.r_multiple for trade in trades]
        profits = sum(max(0.0, trade.net_pnl) for trade in trades)
        losses = abs(sum(min(0.0, trade.net_pnl) for trade in trades))
        peak = equity_curve[0]
        max_drawdown = 0.0
        for value in equity_curve:
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100 if peak else 0)
        profitable = sum(value > 0 for value in monthly_returns.values())
        return BacktestReport(
            strategy_id=self.strategy.settings.strategy_id,
            dataset=dataset,
            signal_count=signal_count,
            trade_count=len(trades),
            expectancy_r=sum(r_values) / len(r_values) if r_values else None,
            profit_factor=profits / losses if losses else None,
            max_drawdown_pct=max_drawdown,
            profitable_month_ratio=(profitable / len(monthly_returns) if monthly_returns else None),
            monthly_returns=monthly_returns,
            trades=trades,
            warnings=[
                "Backtest is validation evidence, not a promise of future profit.",
                "Historical constituent membership must be supplied by date.",
            ],
        )


def _date_key(bar: KlineBar) -> str:
    return datetime.fromtimestamp(float(bar.ts_open) / 1000).date().isoformat()


def _bar_iso(bar: KlineBar) -> str:
    return datetime.fromtimestamp(float(bar.ts_open) / 1000).astimezone().isoformat()
