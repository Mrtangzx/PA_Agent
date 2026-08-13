"""Cash-flow-adjusted monthly equity metrics."""
from __future__ import annotations

from datetime import datetime

from pa_agent.trading.broker_models import BrokerSnapshot, PortfolioSnapshot


def monthly_return_pct(snapshots: list[dict]) -> float | None:
    """Time-weighted monthly return with deposits/withdrawals removed."""
    if len(snapshots) < 2:
        return None
    ordered = sorted(snapshots, key=lambda item: item["captured_at"])
    first = ordered[0]
    month = datetime.fromisoformat(first["captured_at"]).strftime("%Y-%m")
    values = [
        item for item in ordered
        if datetime.fromisoformat(item["captured_at"]).strftime("%Y-%m") == month
    ]
    if len(values) < 2 or float(values[0]["total_equity"]) <= 0:
        return None
    factor = 1.0
    previous = float(values[0]["total_equity"])
    for current in values[1:]:
        equity = float(current["total_equity"])
        flow = float(current.get("external_cash_flow") or 0)
        if previous <= 0:
            return None
        factor *= (equity - flow) / previous
        previous = equity
    return (factor - 1) * 100


def monthly_equity_peak_drawdown_pct(snapshots: list[dict]) -> float | None:
    """Return the largest chronological peak-to-trough account-equity drawdown."""
    if not snapshots:
        return None
    ordered = sorted(snapshots, key=lambda item: item["captured_at"])
    peak = 0.0
    maximum = 0.0
    for item in ordered:
        equity = float(item["total_equity"])
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak * 100)
    return maximum


def portfolio_snapshot_from_store(store, broker: BrokerSnapshot) -> PortfolioSnapshot:
    """Build the portfolio-risk input from broker facts and audited local links."""
    now = datetime.fromisoformat(broker.captured_at)
    month_key = now.strftime("%Y-%m")
    week_start = now.date().toordinal() - now.weekday()
    equities = [
        item for item in store.list_equity_snapshots(
            account_fingerprint=broker.account_fingerprint
        )
        if str(item.get("captured_at") or "").startswith(month_key)
    ]
    monthly = monthly_return_pct(equities)
    peak_drawdown = monthly_equity_peak_drawdown_pct(equities) or 0.0

    actual_results = store.list_results(dataset="actual")
    daily_loss = 0.0
    weekly_loss = 0.0
    total_equity = float(broker.total_equity or 0)
    for result in actual_results:
        closed = str(result.get("closed_at") or "")
        net = float(result.get("net_pnl") or 0)
        if net >= 0 or total_equity <= 0 or not closed:
            continue
        closed_at = datetime.fromisoformat(closed)
        if closed_at.date() == now.date():
            daily_loss += -net / total_equity * 100
        if closed_at.date().toordinal() - closed_at.weekday() == week_start:
            weekly_loss += -net / total_equity * 100

    actual_plans = [
        plan for plan in store.list_plans()
        if plan.get("status") in {"partially_filled", "executed_open", "exit_detected"}
    ]
    plans_by_symbol = {plan["symbol"]: plan for plan in actual_plans}
    external_symbols = {
        item["symbol"] for item in store.list_external_broker_trades()
        if item.get("account_fingerprint") == broker.account_fingerprint
    }
    unexplained = any(
        item.symbol not in plans_by_symbol and item.symbol not in external_symbols
        for item in broker.positions
    )
    current_open_risk = 0.0
    sector_open_risk: dict[str, float] = {}
    for position in broker.positions:
        plan = plans_by_symbol.get(position.symbol)
        if not plan:
            continue
        risk = max(0.0, position.cost_price - float(plan["stop_loss_price"])) * position.quantity
        current_open_risk += risk
        from pa_agent.trading.universe import risk_theme_for_symbol

        risk_bucket = risk_theme_for_symbol(position.symbol) or position.industry
        if risk_bucket:
            sector_open_risk[risk_bucket] = sector_open_risk.get(risk_bucket, 0.0) + risk

    new_positions_today = sum(
        1 for fill in broker.fills
        if fill.direction.lower() in {"buy", "买入"}
        and str(fill.filled_at).startswith(now.date().isoformat())
    )
    data_gaps = []
    if monthly is None:
        data_gaps.append("monthly_equity_baseline_incomplete")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    if not broker.account_fingerprint or not store.cash_flow_history_complete(
        broker.account_fingerprint,
        range_start=month_start,
        range_end=broker.captured_at,
    ):
        data_gaps.append("monthly_cash_flow_history_incomplete")
    return PortfolioSnapshot(
        data_complete=not data_gaps,
        data_gaps=data_gaps,
        current_open_risk=current_open_risk,
        actual_trade_count=len(actual_results),
        daily_realized_loss_pct=daily_loss,
        weekly_realized_loss_pct=weekly_loss,
        monthly_return_pct=monthly if monthly is not None else 0.0,
        monthly_peak_drawdown_pct=peak_drawdown,
        new_positions_today=new_positions_today,
        sector_open_risk=sector_open_risk,
        unexplained_position_difference=unexplained,
    )
