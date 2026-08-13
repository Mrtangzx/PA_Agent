"""Public domain types for the local, human-confirmed trading workflow."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AssetClass(StrEnum):
    A_SHARE = "a_share"
    CN_FUTURES = "cn_futures"
    UNKNOWN = "unknown"


class PlanStatus(StrEnum):
    PROPOSED = "proposed"
    TRIGGERED = "triggered"
    AWAITING_USER_CONFIRMATION = "awaiting_user_confirmation"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    IGNORED = "ignored"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    EXECUTED_OPEN = "executed_open"
    EXIT_DETECTED = "exit_detected"
    CLOSED = "closed"


class TradeEventType(StrEnum):
    CREATED = "created"
    ENTRY_TOUCHED = "entry_touched"
    EXECUTED = "executed"
    IGNORED = "ignored"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    T1_LOCKED_BREACH = "t1_locked_breach"
    TP1_DETECTED = "tp1_detected"
    TP2_DETECTED = "tp2_detected"
    STOP_DETECTED = "stop_detected"
    EXIT_CONFIRMED = "exit_confirmed"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    QUOTE_UNAVAILABLE = "quote_unavailable"
    PRICE_LIMIT_BLOCKED = "price_limit_blocked"
    BROKER_SUBMITTED = "broker_submitted"
    BROKER_PARTIAL_FILL = "broker_partial_fill"
    BROKER_FILLED = "broker_filled"
    BROKER_CANCELLED = "broker_cancelled"
    BROKER_REJECTED = "broker_rejected"
    AWAITING_USER_CONFIRMATION = "awaiting_user_confirmation"
    EXTERNAL_MANUAL_TRADE = "external_manual_trade"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class InstrumentProfile(BaseModel):
    """Confirmed market mechanics and costs; strategy text never lives here."""

    model_config = ConfigDict(extra="ignore")

    asset_class: AssetClass = AssetClass.UNKNOWN
    symbol: str
    instrument_code: str = ""
    tick_size: float | None = Field(default=None, gt=0)
    trading_hours: str = ""
    price_precision: int | None = Field(default=None, ge=0, le=12)
    allow_short: bool = False
    costs_configured: bool = False
    confirmed: bool = False

    # A shares
    board_lot: int = Field(default=100, ge=1)
    t_plus_one: bool = True
    commission_rate: float | None = Field(default=None, ge=0)
    minimum_commission: float | None = Field(default=None, ge=0)
    sell_tax_rate: float | None = Field(default=None, ge=0)
    price_limit_type: str = ""
    adjustment_mode: Literal["qfq", "hfq", "none", ""] = ""

    # Chinese futures
    real_contract: str = ""
    product_code: str = ""
    contract_multiplier: float | None = Field(default=None, gt=0)
    margin_rate: float | None = Field(default=None, ge=0, le=1)
    fee_per_lot: float | None = Field(default=None, ge=0)
    estimated_slippage_ticks: float | None = Field(default=None, ge=0)
    has_night_session: bool = False
    last_trading_day: str = ""


class RiskSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_equity: float | None = Field(default=None, gt=0)
    available_cash: float | None = Field(default=None, ge=0)
    per_trade_risk_pct: float = Field(default=0.5, gt=0, le=100)
    max_open_risk_pct: float = Field(default=1.5, gt=0, le=100)
    daily_loss_warning_pct: float = Field(default=1.5, gt=0, le=100)
    weekly_loss_warning_pct: float = Field(default=3.0, gt=0, le=100)


class TradePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    decision_event_id: str
    analysis_record_ref: str = ""
    symbol: str
    timeframe: str
    asset_class: AssetClass = AssetClass.UNKNOWN
    direction: str
    order_type: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    take_profit_price_2: float | None = None
    valid_until: str = ""
    status: PlanStatus = PlanStatus.PROPOSED
    shadow_status: str = "proposed"
    strategy_version: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().astimezone().isoformat())
    risk_snapshot: dict[str, Any] = Field(default_factory=dict)


class Execution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    plan_id: str
    executed_at: str
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    real_contract: str = ""
    fees: float = Field(default=0, ge=0)
    note: str = ""


class TradeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    plan_id: str
    dataset: Literal["shadow", "actual"]
    outcome: str
    entry_price: float | None = None
    exit_price: float | None = None
    quantity: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    r_multiple: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    holding_bars: int | None = None
    ambiguous_same_bar: bool = False
    opened_at: str = ""
    closed_at: str = ""
