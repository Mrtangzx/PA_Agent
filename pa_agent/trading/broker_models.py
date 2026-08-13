"""Broker-facing domain types shared by risk and concrete broker adapters."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BrokerConnectionStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED_READ_ONLY = "connected_read_only"
    CONNECTED = "connected"
    LOGIN_REQUIRED = "login_required"
    ACCOUNT_MISMATCH = "account_mismatch"
    ADAPTER_INCOMPATIBLE = "adapter_incompatible"
    BLOCKED_BY_MODAL = "blocked_by_modal"
    STALE = "stale"
    ERROR = "error"


class ThsBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    read_only: bool = True
    market_executable: str = "happ.exe"
    trading_executable: str = "xiadan.exe"
    install_path: str = ""
    client_version: str = ""
    broker_name: str = ""
    masked_account: str = ""
    account_fingerprint: str = ""
    confirmed: bool = False
    allow_prefill: bool = False
    idle_sync_seconds: int = Field(default=30, ge=5, le=3600)
    quote_sync_seconds: int = Field(default=2, ge=1, le=60)
    max_quote_age_seconds: int = Field(default=5, ge=1, le=60)
    max_price_deviation_pct: float = Field(default=0.5, ge=0, le=10)


class ConnectionState(BaseModel):
    status: BrokerConnectionStatus
    message: str = ""
    market_pid: int | None = None
    trading_pid: int | None = None
    market_window: int | None = None
    trading_window: int | None = None
    detected_install_path: str = ""
    client_version: str = ""
    account_fingerprint: str = ""
    detected_broker_name: str = ""
    detected_masked_account: str = ""
    checked_at: str

    @property
    def usable(self) -> bool:
        return self.status in {
            BrokerConnectionStatus.CONNECTED_READ_ONLY,
            BrokerConnectionStatus.CONNECTED,
        }


class BrokerPosition(BaseModel):
    symbol: str
    name: str = ""
    quantity: int = Field(ge=0)
    sellable_quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)
    last_price: float = Field(ge=0)
    market_value: float = Field(ge=0)
    industry: str = ""


class BrokerOrder(BaseModel):
    broker_order_id: str = ""
    symbol: str
    direction: str
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    filled_quantity: int = Field(default=0, ge=0)
    status: str
    submitted_at: str


class BrokerFill(BaseModel):
    broker_fill_id: str = ""
    broker_order_id: str = ""
    symbol: str
    direction: str
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    fees: float = Field(default=0, ge=0)
    filled_at: str


class BrokerCashFlow(BaseModel):
    broker_flow_id: str = ""
    direction: str
    amount: float = Field(gt=0)
    occurred_at: str
    status: str = "confirmed"
    description: str = ""
    source: str = "ths_ui"


class BrokerQuote(BaseModel):
    symbol: str = ""
    name: str = ""
    last_price: float | None = Field(default=None, gt=0)
    upper_limit: float | None = Field(default=None, gt=0)
    lower_limit: float | None = Field(default=None, gt=0)
    suspended: bool = False
    limit_locked: bool = False
    captured_at: str = ""


class BrokerSnapshot(BaseModel):
    connection: ConnectionState
    account_fingerprint: str = ""
    total_equity: float | None = Field(default=None, ge=0)
    available_cash: float | None = Field(default=None, ge=0)
    position_value: float | None = Field(default=None, ge=0)
    daily_pnl: float | None = None
    positions: list[BrokerPosition] = Field(default_factory=list)
    orders: list[BrokerOrder] = Field(default_factory=list)
    fills: list[BrokerFill] = Field(default_factory=list)
    cash_flows: list[BrokerCashFlow] = Field(default_factory=list)
    cash_flow_complete: bool = False
    cash_flow_range_start: str = ""
    cash_flow_range_end: str = ""
    quote: BrokerQuote | None = None
    captured_at: str
    source: str = "ths_ui"
    complete: bool = False
    warnings: list[str] = Field(default_factory=list)


class PortfolioSnapshot(BaseModel):
    data_complete: bool = True
    data_gaps: list[str] = Field(default_factory=list)
    current_open_risk: float = Field(default=0, ge=0)
    actual_trade_count: int = Field(default=0, ge=0)
    daily_realized_loss_pct: float = Field(default=0, ge=0)
    weekly_realized_loss_pct: float = Field(default=0, ge=0)
    monthly_return_pct: float = 0
    monthly_peak_drawdown_pct: float = Field(default=0, ge=0)
    total_strategy_drawdown_pct: float = Field(default=0, ge=0)
    new_positions_today: int = Field(default=0, ge=0)
    sector_open_risk: dict[str, float] = Field(default_factory=dict)
    unexplained_position_difference: bool = False


class AuthorizedOrder(BaseModel):
    plan_id: str
    account_fingerprint: str
    symbol: str
    name: str = ""
    direction: str
    order_type: str = "limit"
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    strategy_id: str
    authorized_at: str
    expires_at: str
    expected_cost: float = Field(default=0, ge=0)


class PrefillReceipt(BaseModel):
    status: str
    message: str = ""
    verified_fields: dict[str, str | int | float] = Field(default_factory=dict)
    created_at: str
    final_confirmation_clicked: bool = False


class ReconciliationResult(BaseModel):
    status: str
    plan_id: str
    matched_order_ids: list[str] = Field(default_factory=list)
    matched_fill_ids: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    message: str = ""
