from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pa_agent.trading.broker_models import (
    BrokerConnectionStatus,
    BrokerQuote,
    BrokerSnapshot,
    ConnectionState,
    PortfolioSnapshot,
)
from pa_agent.trading.models import AssetClass, InstrumentProfile, RiskSettings
from pa_agent.trading.portfolio import (
    PortfolioRisk,
    PortfolioRiskSettings,
    RiskStatus,
)
from pa_agent.trading.quant import SignalDecision, SignalStatus, StrategyState
from pa_agent.trading.topdown import (
    TOPDOWN_STRATEGY_ID,
    TopDownScoreSnapshot,
    TopDownScoreStatus,
)


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _signal() -> SignalDecision:
    now = _now()
    return SignalDecision(
        status=SignalStatus.ALLOW,
        strategy_id="hs300_daily_pullback_v1",
        parameter_version="1.0.0",
        pool_version="hs300-2026-08",
        symbol="600519",
        signal_time=now,
        trigger_price=100,
        max_entry_price=101,
        initial_stop=95,
        valid_until=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )


def _broker(*, complete: bool = True) -> BrokerSnapshot:
    now = _now()
    state = ConnectionState(
        status=BrokerConnectionStatus.CONNECTED,
        checked_at=now,
        account_fingerprint="abc",
    )
    return BrokerSnapshot(
        connection=state,
        account_fingerprint="abc",
        total_equity=1_000_000,
        available_cash=1_000_000,
        position_value=0,
        quote=BrokerQuote(symbol="600519", name="贵州茅台", last_price=100, captured_at=now),
        captured_at=now,
        complete=complete,
    )


def _profile() -> InstrumentProfile:
    return InstrumentProfile(
        symbol="600519",
        asset_class=AssetClass.A_SHARE,
        tick_size=0.01,
        board_lot=100,
        costs_configured=True,
        confirmed=True,
        commission_rate=0.0003,
        minimum_commission=5,
        sell_tax_rate=0.0005,
    )


def test_live_trading_is_off_by_default() -> None:
    result = PortfolioRisk(RiskSettings()).authorize(
        plan_id="p1", signal=_signal(), broker=_broker(), portfolio=PortfolioSnapshot(),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
    )
    assert result.status is RiskStatus.BLOCKED
    assert "live_trading_not_explicitly_enabled" in result.reasons


def test_complete_broker_snapshot_authorizes_board_lot_after_explicit_enable() -> None:
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.25, max_open_risk_pct=1.0),
        PortfolioRiskSettings(live_trading_enabled=True),
    )
    result = module.authorize(
        plan_id="p1", signal=_signal(), broker=_broker(), portfolio=PortfolioSnapshot(),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
    )
    assert result.status is RiskStatus.AUTHORIZED
    assert result.order is not None
    assert result.order.quantity % 100 == 0
    assert result.order.quantity * result.order.price <= 100_000


def test_reduced_state_halves_risk() -> None:
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.5),
        PortfolioRiskSettings(live_trading_enabled=True),
    )
    result = module.authorize(
        plan_id="p1", signal=_signal(), broker=_broker(), portfolio=PortfolioSnapshot(),
        strategy_state=StrategyState.REDUCED, profile=_profile(), external_quote_price=100,
    )
    assert result.effective_per_trade_risk_pct == 0.125


def test_first_30_actual_trades_are_capped_at_quarter_percent() -> None:
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.5),
        PortfolioRiskSettings(live_trading_enabled=True),
    )
    result = module.authorize(
        plan_id="p1", signal=_signal(), broker=_broker(),
        portfolio=PortfolioSnapshot(actual_trade_count=29),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
    )
    assert result.effective_per_trade_risk_pct == 0.25
    assert result.risk_snapshot["effective_max_open_risk_pct"] == 1.0
    assert result.risk_snapshot["risk_stage"] == "initial_live"


def test_initial_stage_blocks_at_one_percent_total_open_risk() -> None:
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.5, max_open_risk_pct=1.5),
        PortfolioRiskSettings(live_trading_enabled=True),
    )
    result = module.authorize(
        plan_id="p1", signal=_signal(), broker=_broker(),
        portfolio=PortfolioSnapshot(
            actual_trade_count=29,
            current_open_risk=10_000,
        ),
        strategy_state=StrategyState.ACTIVE, profile=_profile(),
        external_quote_price=100,
    )
    assert result.status is RiskStatus.BLOCKED
    assert "maximum_open_risk_reached" in result.reasons
    assert result.risk_snapshot["effective_max_open_risk_pct"] == 1.0


def test_upgraded_stage_uses_half_percent_and_one_point_five_open_risk() -> None:
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.5, max_open_risk_pct=1.5),
        PortfolioRiskSettings(live_trading_enabled=True),
    )
    result = module.authorize(
        plan_id="p1", signal=_signal(), broker=_broker(),
        portfolio=PortfolioSnapshot(
            actual_trade_count=30,
            current_open_risk=10_000,
        ),
        strategy_state=StrategyState.ACTIVE, profile=_profile(),
        external_quote_price=100,
    )
    assert result.status is RiskStatus.AUTHORIZED
    assert result.effective_per_trade_risk_pct == 0.5
    assert result.risk_snapshot["effective_max_open_risk_pct"] == 1.5
    assert result.risk_snapshot["risk_stage"] == "upgraded"


def test_monthly_profit_or_peak_drawdown_halves_effective_risk() -> None:
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.5),
        PortfolioRiskSettings(live_trading_enabled=True, initial_live_trade_count=0),
    )
    profit = module.authorize(
        plan_id="p1", signal=_signal(), broker=_broker(),
        portfolio=PortfolioSnapshot(actual_trade_count=30, monthly_return_pct=2.0),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
    )
    drawdown = module.authorize(
        plan_id="p2", signal=_signal(), broker=_broker(),
        portfolio=PortfolioSnapshot(actual_trade_count=30, monthly_peak_drawdown_pct=0.8),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
    )
    assert profit.effective_per_trade_risk_pct == 0.25
    assert drawdown.effective_per_trade_risk_pct == 0.25


def test_stale_broker_quote_and_daily_loss_fail_closed() -> None:
    broker = _broker()
    broker.quote.captured_at = "2026-01-01T00:00:00+08:00"
    module = PortfolioRisk(
        RiskSettings(daily_loss_warning_pct=1.0),
        PortfolioRiskSettings(live_trading_enabled=True),
    )
    result = module.authorize(
        plan_id="p1", signal=_signal(), broker=broker,
        portfolio=PortfolioSnapshot(daily_realized_loss_pct=1.0),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
    )
    assert "broker_quote_stale" in result.reasons
    assert "daily_realized_loss_stop_reached" in result.reasons


def _eligible_topdown() -> TopDownScoreSnapshot:
    return TopDownScoreSnapshot(
        strategy_version=TOPDOWN_STRATEGY_ID,
        scoring_version="1.0.0",
        symbol="600519",
        pool_version="hs300-2026-08",
        bar_closed_at=_now(),
        index_score=31,
        sentiment_score=22,
        theme_score=14,
        stock_score=8,
        total_score=75,
        consecutive_pass_count=2,
        input_hash="a" * 64,
        status=TopDownScoreStatus.ELIGIBLE_FOR_RISK,
    )


def test_topdown_strategy_requires_eligible_score_snapshot() -> None:
    signal = _signal().model_copy(update={"strategy_id": TOPDOWN_STRATEGY_ID})
    module = PortfolioRisk(RiskSettings(), PortfolioRiskSettings(live_trading_enabled=True))
    blocked = module.authorize(
        plan_id="p1", signal=signal, broker=_broker(), portfolio=PortfolioSnapshot(),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
    )
    assert "topdown_score_required" in blocked.reasons
    allowed = module.authorize(
        plan_id="p1", signal=signal, broker=_broker(), portfolio=PortfolioSnapshot(),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
        topdown_score=_eligible_topdown(),
    )
    assert allowed.status is RiskStatus.AUTHORIZED


def test_outside_pool_exception_halves_risk_and_requires_approval() -> None:
    signal = _signal().model_copy(update={"strategy_id": "manual_exception_4321_v1"})
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.5),
        PortfolioRiskSettings(live_trading_enabled=True, initial_live_trade_count=0),
    )
    blocked = module.authorize(
        plan_id="p1", signal=signal, broker=_broker(), portfolio=PortfolioSnapshot(),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
        topdown_score=_eligible_topdown(), trading_channel="outside_pool_exception",
    )
    assert "outside_pool_approval_required" in blocked.reasons
    allowed = module.authorize(
        plan_id="p1", signal=signal, broker=_broker(), portfolio=PortfolioSnapshot(),
        strategy_state=StrategyState.ACTIVE, profile=_profile(), external_quote_price=100,
        topdown_score=_eligible_topdown(), trading_channel="outside_pool_exception",
        outside_pool_approval_valid=True,
    )
    assert allowed.status is RiskStatus.AUTHORIZED
    assert allowed.effective_per_trade_risk_pct == 0.25


def test_cloud_ai_members_share_one_theme_risk_cap() -> None:
    signal = _signal().model_copy(update={
        "strategy_id": TOPDOWN_STRATEGY_ID,
        "pool_version": "cloud_ai_11_v1-2026-08",
        "symbol": "300846",
    })
    broker = _broker()
    broker.quote.symbol = "300846"
    profile = _profile().model_copy(update={"symbol": "300846"})
    score = _eligible_topdown().model_copy(update={
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "symbol": "300846",
        "pool_version": "cloud_ai_11_v1-2026-08",
    })
    module = PortfolioRisk(
        RiskSettings(per_trade_risk_pct=0.25),
        PortfolioRiskSettings(live_trading_enabled=True),
    )

    result = module.authorize(
        plan_id="p1", signal=signal, broker=broker,
        portfolio=PortfolioSnapshot(sector_open_risk={"云算力主题": 7_400}),
        strategy_state=StrategyState.ACTIVE, profile=profile,
        external_quote_price=100, topdown_score=score,
    )

    assert result.status is RiskStatus.BLOCKED
    assert "sector_risk_limit_exceeded" in result.reasons
