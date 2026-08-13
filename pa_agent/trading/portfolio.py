"""Portfolio authorization.  This is the only path to an executable order."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from pa_agent.trading.broker_models import (
    AuthorizedOrder,
    BrokerSnapshot,
    PortfolioSnapshot,
)
from pa_agent.trading.models import InstrumentProfile, RiskSettings
from pa_agent.trading.quant import SignalDecision, SignalStatus, StrategyState
from pa_agent.trading.risk import calculate_position_size
from pa_agent.trading.topdown import (
    MANUAL_EXCEPTION_STRATEGY_ID,
    TOPDOWN_STRATEGY_ID,
    TopDownScoreSnapshot,
)
from pa_agent.trading.universe import risk_theme_for_symbol

TradingChannel = Literal["normal_pool", "outside_pool_exception"]


class RiskStatus(StrEnum):
    AUTHORIZED = "authorized"
    BLOCKED = "blocked"


class PortfolioRiskSettings(BaseModel):
    max_positions: int = Field(default=3, ge=1)
    max_new_positions_per_day: int = Field(default=1, ge=1)
    max_position_value_pct: float = Field(default=10.0, gt=0, le=100)
    max_sector_open_risk_pct: float = Field(default=0.75, gt=0, le=100)
    monthly_warning_loss_pct: float = Field(default=1.0, gt=0)
    monthly_stop_loss_pct: float = Field(default=1.5, gt=0)
    monthly_profit_protect_pct: float = Field(default=2.0, gt=0)
    monthly_peak_drawdown_reduce_pct: float = Field(default=0.8, gt=0)
    drawdown_reduce_pct: float = Field(default=5.0, gt=0)
    drawdown_pause_pct: float = Field(default=8.0, gt=0)
    drawdown_retire_pct: float = Field(default=10.0, gt=0)
    require_complete_broker_snapshot: bool = True
    max_quote_age_seconds: int = Field(default=5, ge=1, le=60)
    max_quote_deviation_pct: float = Field(default=0.5, ge=0, le=10)
    initial_live_trade_count: int = Field(default=30, ge=0)
    initial_per_trade_risk_pct: float = Field(default=0.25, gt=0, le=100)
    initial_max_open_risk_pct: float = Field(default=1.0, gt=0, le=100)
    live_trading_enabled: bool = False


class RiskDecision(BaseModel):
    status: RiskStatus
    reasons: list[str] = Field(default_factory=list)
    quantity: int | None = None
    effective_per_trade_risk_pct: float | None = None
    risk_snapshot: dict = Field(default_factory=dict)
    order: AuthorizedOrder | None = None


class PortfolioRisk:
    def __init__(
        self,
        risk_settings: RiskSettings,
        portfolio_settings: PortfolioRiskSettings | None = None,
    ) -> None:
        self.risk_settings = risk_settings
        self.settings = portfolio_settings or PortfolioRiskSettings()

    def authorize(
        self,
        *,
        plan_id: str,
        signal: SignalDecision,
        broker: BrokerSnapshot,
        portfolio: PortfolioSnapshot,
        strategy_state: StrategyState,
        profile: InstrumentProfile,
        external_quote_price: float | None = None,
        industry: str = "",
        topdown_score: TopDownScoreSnapshot | None = None,
        trading_channel: TradingChannel = "normal_pool",
        outside_pool_approval_valid: bool = False,
        outside_pool_position_count: int = 0,
    ) -> RiskDecision:
        reasons: list[str] = []
        s = self.settings
        if signal.status is not SignalStatus.ALLOW:
            reasons.append("quant_signal_not_allowed")
        if not portfolio.data_complete:
            reasons.extend(portfolio.data_gaps or ["portfolio_snapshot_incomplete"])
        requires_topdown = signal.strategy_id in {
            TOPDOWN_STRATEGY_ID,
            MANUAL_EXCEPTION_STRATEGY_ID,
        }
        if requires_topdown:
            if topdown_score is None:
                reasons.append("topdown_score_required")
            elif topdown_score.symbol != signal.symbol:
                reasons.append("topdown_score_symbol_mismatch")
            elif not topdown_score.eligible_for_risk:
                reasons.append(f"topdown_score_{topdown_score.status.value}")
            elif topdown_score.hard_blocks or topdown_score.data_gaps:
                reasons.append("topdown_score_not_clean")
        if trading_channel == "outside_pool_exception":
            if not outside_pool_approval_valid:
                reasons.append("outside_pool_approval_required")
            if outside_pool_position_count >= 1:
                reasons.append("outside_pool_position_limit_reached")
        if not s.live_trading_enabled:
            reasons.append("live_trading_not_explicitly_enabled")
        if strategy_state not in {StrategyState.ACTIVE, StrategyState.REDUCED}:
            reasons.append(f"strategy_state_{strategy_state.value}_cannot_trade")
        if not broker.connection.usable:
            reasons.append(f"broker_{broker.connection.status.value}")
        if s.require_complete_broker_snapshot and not broker.complete:
            reasons.append("broker_snapshot_incomplete")
        if broker.account_fingerprint != broker.connection.account_fingerprint:
            reasons.append("broker_account_fingerprint_mismatch")
        if broker.available_cash is None or broker.total_equity is None:
            reasons.append("broker_funds_unavailable")
        if portfolio.unexplained_position_difference:
            reasons.append("unexplained_position_difference")
        if len(broker.positions) >= s.max_positions:
            reasons.append("maximum_positions_reached")
        if portfolio.new_positions_today >= s.max_new_positions_per_day:
            reasons.append("maximum_new_positions_today_reached")
        if portfolio.monthly_return_pct <= -s.monthly_stop_loss_pct:
            reasons.append("monthly_loss_stop_reached")
        if portfolio.daily_realized_loss_pct >= self.risk_settings.daily_loss_warning_pct:
            reasons.append("daily_realized_loss_stop_reached")
        if portfolio.weekly_realized_loss_pct >= self.risk_settings.weekly_loss_warning_pct:
            reasons.append("weekly_realized_loss_stop_reached")
        if portfolio.total_strategy_drawdown_pct >= s.drawdown_pause_pct:
            reasons.append("strategy_drawdown_pause_reached")
        if any(position.symbol == signal.symbol for position in broker.positions):
            reasons.append("position_already_exists_no_adding")
        risk_bucket = risk_theme_for_symbol(signal.symbol) or industry
        sector_risk = 0.0
        if risk_bucket and broker.total_equity:
            sector_risk = portfolio.sector_open_risk.get(risk_bucket, 0.0)
            if sector_risk >= broker.total_equity * s.max_sector_open_risk_pct / 100:
                reasons.append("sector_risk_limit_reached")
        quote = broker.quote
        if quote is None or quote.symbol != signal.symbol or quote.last_price is None:
            reasons.append("fresh_matching_broker_quote_required")
        else:
            try:
                captured = datetime.fromisoformat(quote.captured_at)
                age = (datetime.now(UTC).astimezone() - captured).total_seconds()
            except (TypeError, ValueError):
                age = float("inf")
            if age < 0 or age > s.max_quote_age_seconds:
                reasons.append("broker_quote_stale")
            if external_quote_price:
                deviation = abs(quote.last_price - external_quote_price) / external_quote_price * 100
                if deviation > s.max_quote_deviation_pct:
                    reasons.append("external_and_broker_quote_deviation")
        if reasons:
            return RiskDecision(status=RiskStatus.BLOCKED, reasons=reasons)

        assert signal.trigger_price and signal.initial_stop and broker.total_equity is not None
        effective_pct = self.risk_settings.per_trade_risk_pct
        effective_max_open_risk_pct = self.risk_settings.max_open_risk_pct
        if portfolio.actual_trade_count < s.initial_live_trade_count:
            effective_pct = min(effective_pct, s.initial_per_trade_risk_pct)
            effective_max_open_risk_pct = min(
                effective_max_open_risk_pct,
                s.initial_max_open_risk_pct,
            )
        if (
            strategy_state is StrategyState.REDUCED
            or portfolio.monthly_return_pct >= s.monthly_profit_protect_pct
            or portfolio.monthly_peak_drawdown_pct >= s.monthly_peak_drawdown_reduce_pct
            or portfolio.total_strategy_drawdown_pct >= s.drawdown_reduce_pct
        ):
            effective_pct /= 2
        if trading_channel == "outside_pool_exception":
            effective_pct *= 0.5
        effective = self.risk_settings.model_copy(update={
            "account_equity": broker.total_equity,
            "available_cash": broker.available_cash,
            "per_trade_risk_pct": effective_pct,
            "max_open_risk_pct": effective_max_open_risk_pct,
        })
        sizing = calculate_position_size(
            entry_price=signal.trigger_price,
            stop_loss_price=signal.initial_stop,
            profile=profile,
            settings=effective,
            current_open_risk=portfolio.current_open_risk,
        )
        sizing["effective_per_trade_risk_pct"] = effective_pct
        sizing["effective_max_open_risk_pct"] = effective_max_open_risk_pct
        sizing["risk_stage"] = (
            "initial_live"
            if portfolio.actual_trade_count < s.initial_live_trade_count
            else "upgraded"
        )
        quantity = sizing.get("quantity")
        if not quantity:
            return RiskDecision(
                status=RiskStatus.BLOCKED,
                reasons=list(sizing.get("missing_fields") or sizing.get("warnings") or ["position_size_zero"]),
                effective_per_trade_risk_pct=effective_pct,
                risk_snapshot=sizing,
            )
        if risk_bucket and broker.total_equity:
            sector_cap = broker.total_equity * s.max_sector_open_risk_pct / 100
            if sector_risk + float(sizing.get("planned_risk") or 0) > sector_cap:
                return RiskDecision(
                    status=RiskStatus.BLOCKED,
                    reasons=["sector_risk_limit_exceeded"],
                    effective_per_trade_risk_pct=effective_pct,
                    risk_snapshot={
                        **sizing,
                        "risk_bucket": risk_bucket,
                        "sector_open_risk": sector_risk,
                        "sector_risk_cap": sector_cap,
                    },
                )
        maximum_value = broker.total_equity * s.max_position_value_pct / 100
        lot = profile.board_lot or 100
        value_limited = int(maximum_value // signal.trigger_price // lot) * lot
        quantity = min(int(quantity), value_limited)
        required = signal.trigger_price * quantity + float(sizing.get("estimated_cost") or 0)
        if quantity <= 0 or required > float(broker.available_cash or 0):
            return RiskDecision(
                status=RiskStatus.BLOCKED,
                reasons=["position_value_or_cash_limit"],
                effective_per_trade_risk_pct=effective_pct,
                risk_snapshot=sizing,
            )
        now = datetime.now(UTC).astimezone().isoformat()
        order = AuthorizedOrder(
            plan_id=plan_id,
            account_fingerprint=broker.account_fingerprint,
            symbol=signal.symbol,
            direction="buy",
            price=signal.trigger_price,
            quantity=quantity,
            stop_loss_price=signal.initial_stop,
            strategy_id=signal.strategy_id,
            authorized_at=now,
            expires_at=signal.valid_until,
            expected_cost=float(sizing.get("estimated_cost") or 0),
        )
        return RiskDecision(
            status=RiskStatus.AUTHORIZED,
            quantity=quantity,
            effective_per_trade_risk_pct=effective_pct,
            risk_snapshot=sizing,
            order=order,
        )
