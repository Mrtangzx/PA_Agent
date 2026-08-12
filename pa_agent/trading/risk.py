"""Deterministic position sizing. The LLM is never an input for quantity."""
from __future__ import annotations

import math
from typing import Any

from pa_agent.trading.models import AssetClass, InstrumentProfile, RiskSettings


def estimate_round_trip_cost(
    profile: InstrumentProfile,
    *,
    price: float,
    quantity: float,
) -> float | None:
    if not profile.costs_configured:
        return None
    if profile.asset_class is AssetClass.A_SHARE:
        if profile.commission_rate is None or profile.minimum_commission is None or profile.sell_tax_rate is None:
            return None
        notional = price * quantity
        commission = max(profile.minimum_commission, notional * profile.commission_rate)
        return commission * 2 + notional * profile.sell_tax_rate
    if profile.asset_class is AssetClass.CN_FUTURES:
        if profile.fee_per_lot is None:
            return None
        slippage = 0.0
        if profile.estimated_slippage_ticks and profile.tick_size and profile.contract_multiplier:
            slippage = (
                profile.estimated_slippage_ticks
                * profile.tick_size
                * profile.contract_multiplier
                * quantity
                * 2
            )
        return profile.fee_per_lot * quantity * 2 + slippage
    return None


def calculate_position_size(
    *,
    entry_price: float,
    stop_loss_price: float,
    profile: InstrumentProfile,
    settings: RiskSettings,
    current_open_risk: float = 0.0,
) -> dict[str, Any]:
    """Return a quantity recommendation or explicit missing/limit reasons."""
    missing: list[str] = []
    if settings.account_equity is None:
        missing.append("account_equity")
    if not profile.costs_configured:
        missing.append("cost_configuration")
    if profile.asset_class is AssetClass.UNKNOWN:
        missing.append("recognized_asset_class")
    if profile.asset_class is AssetClass.CN_FUTURES:
        if profile.contract_multiplier is None:
            missing.append("contract_multiplier")
        if not profile.real_contract or is_continuous_contract(profile.real_contract):
            missing.append("real_futures_contract")
        if profile.margin_rate is None:
            missing.append("margin_rate")
    if missing:
        return {"quantity": None, "status": "unavailable", "missing_fields": missing}

    equity = float(settings.account_equity)
    price_risk = abs(float(entry_price) - float(stop_loss_price))
    if price_risk <= 0:
        return {"quantity": None, "status": "unavailable", "missing_fields": ["positive_stop_distance"]}

    per_trade_cap = equity * settings.per_trade_risk_pct / 100.0
    portfolio_remaining = max(0.0, equity * settings.max_open_risk_pct / 100.0 - current_open_risk)
    risk_cap = min(per_trade_cap, portfolio_remaining)
    if risk_cap <= 0:
        return {
            "quantity": 0,
            "status": "blocked",
            "warnings": ["maximum_open_risk_reached"],
            "risk_cap": risk_cap,
        }

    if profile.asset_class is AssetClass.A_SHARE:
        lot = profile.board_lot or 100
        rough_qty = math.floor(risk_cap / price_risk / lot) * lot
        qty = rough_qty
        while qty > 0:
            cost = estimate_round_trip_cost(profile, price=entry_price, quantity=qty)
            if cost is not None and price_risk * qty + cost <= risk_cap:
                break
            qty -= lot
        if settings.available_cash is not None:
            cash_qty = math.floor(settings.available_cash / entry_price / lot) * lot
            qty = min(qty, cash_qty)
        cost = estimate_round_trip_cost(profile, price=entry_price, quantity=qty) or 0.0
        margin = entry_price * qty
        worst = price_risk * qty + cost
    else:
        multiplier = float(profile.contract_multiplier)
        one_lot_cost = estimate_round_trip_cost(profile, price=entry_price, quantity=1) or 0.0
        one_lot_risk = price_risk * multiplier + one_lot_cost
        qty = math.floor(risk_cap / one_lot_risk)
        margin_per_lot = entry_price * multiplier * float(profile.margin_rate)
        if settings.available_cash is not None and margin_per_lot > 0:
            qty = min(qty, math.floor(settings.available_cash / margin_per_lot))
        cost = estimate_round_trip_cost(profile, price=entry_price, quantity=qty) or 0.0
        margin = margin_per_lot * qty
        worst = price_risk * multiplier * qty + cost

    return {
        "quantity": int(qty),
        "status": "ok" if qty > 0 else "blocked",
        "missing_fields": [],
        "risk_cap": risk_cap,
        "planned_risk": worst,
        "estimated_cost": cost,
        "worst_case_amount": worst,
        "capital_required": margin,
        "warnings": [] if qty > 0 else ["insufficient_risk_or_available_funds"],
    }


def is_continuous_contract(symbol: str) -> bool:
    from pa_agent.trading.profiles import is_continuous_futures_symbol

    return is_continuous_futures_symbol(symbol)
