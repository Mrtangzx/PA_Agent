from __future__ import annotations

from pa_agent.trading.models import AssetClass, InstrumentProfile, RiskSettings
from pa_agent.trading.risk import calculate_position_size


def test_a_share_quantity_uses_board_lots_and_costs() -> None:
    profile = InstrumentProfile(
        asset_class=AssetClass.A_SHARE, symbol="600519", costs_configured=True,
        commission_rate=0.0003, minimum_commission=5, sell_tax_rate=0.0005,
    )
    result = calculate_position_size(
        entry_price=100, stop_loss_price=98, profile=profile,
        settings=RiskSettings(account_equity=100_000, available_cash=100_000),
    )
    assert result["quantity"] % 100 == 0
    assert result["planned_risk"] <= 500


def test_missing_cost_prevents_quantity() -> None:
    profile = InstrumentProfile(asset_class=AssetClass.A_SHARE, symbol="600519")
    result = calculate_position_size(
        entry_price=100, stop_loss_price=98, profile=profile,
        settings=RiskSettings(account_equity=100_000),
    )
    assert result["quantity"] is None
    assert "cost_configuration" in result["missing_fields"]


def test_futures_uses_multiplier_and_margin() -> None:
    profile = InstrumentProfile(
        asset_class=AssetClass.CN_FUTURES, symbol="AU0", real_contract="AU2612",
        costs_configured=True, contract_multiplier=1000, margin_rate=0.12,
        fee_per_lot=10, tick_size=0.02, estimated_slippage_ticks=1,
    )
    result = calculate_position_size(
        entry_price=500, stop_loss_price=499.8, profile=profile,
        settings=RiskSettings(account_equity=1_000_000, available_cash=1_000_000),
    )
    assert result["quantity"] > 0
    assert result["planned_risk"] <= 5_000
