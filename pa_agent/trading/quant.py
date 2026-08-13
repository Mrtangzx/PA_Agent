"""Deterministic A-share strategy used by the private trading workflow.

The module deliberately has no AI dependency.  Given the same closed bars and
configuration it always returns the same decision and audit snapshot.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from pa_agent.data.base import KlineBar


class SignalStatus(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"


class StrategyState(StrEnum):
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    ACTIVE = "active"
    REDUCED = "reduced"
    PAUSED = "paused"
    RETIRED = "retired"


class StrategySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    strategy_id: str = "cloud_ai_daily_pullback_v1"
    parameter_version: str = "1.0.0"
    stock_pool_size: int = Field(default=11, ge=1, le=300)
    market_breadth_min_pct: float = Field(default=55.0, ge=0, le=100)
    pullback_atr_min: float = Field(default=0.8, gt=0)
    pullback_atr_max: float = Field(default=2.5, gt=0)
    ma_touch_atr: float = Field(default=0.35, gt=0)
    volume_ratio_min: float = Field(default=0.8, gt=0)
    volume_ratio_max: float = Field(default=1.8, gt=0)
    close_location_min: float = Field(default=0.65, ge=0, le=1)
    stop_buffer_atr: float = Field(default=0.2, ge=0)
    stop_distance_atr_min: float = Field(default=1.0, gt=0)
    stop_distance_atr_max: float = Field(default=3.0, gt=0)
    max_gap_pct: float = Field(default=2.0, gt=0)
    max_gap_atr: float = Field(default=0.8, gt=0)
    time_stop_bars: int = Field(default=10, ge=1)
    time_stop_min_r: float = Field(default=0.5, ge=0)
    trailing_atr: float = Field(default=2.0, gt=0)


class StrategyContext(BaseModel):
    """Closed daily bars in chronological order (oldest first)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    symbol: str
    bars: tuple[KlineBar, ...]
    index_bars: tuple[KlineBar, ...]
    market_breadth_pct: float
    pool_version: str
    signal_time: str
    next_trading_time: str = ""
    tick_size: float = Field(default=0.01, gt=0)
    eligible: bool = True
    eligibility_reasons: tuple[str, ...] = ()


class SignalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SignalStatus
    strategy_id: str
    parameter_version: str
    pool_version: str
    symbol: str
    signal_time: str
    reasons: list[str] = Field(default_factory=list)
    condition_snapshot: dict[str, Any] = Field(default_factory=dict)
    trigger_price: float | None = None
    max_entry_price: float | None = None
    initial_stop: float | None = None
    valid_until: str = ""
    exit_rules: dict[str, Any] = Field(default_factory=dict)
    invalidation_rules: dict[str, Any] = Field(default_factory=dict)


class Strategy(Protocol):
    def evaluate(self, context: StrategyContext) -> SignalDecision: ...


class Hs300DailyPullbackStrategy:
    """Unified A-share trend pullback strategy (name kept for API compatibility)."""

    def __init__(self, settings: StrategySettings | None = None) -> None:
        self.settings = settings or StrategySettings()

    def evaluate(self, context: StrategyContext) -> SignalDecision:
        s = self.settings
        reasons: list[str] = []
        snapshot: dict[str, Any] = {}
        if not s.enabled:
            reasons.append("strategy_disabled")
        if not context.eligible:
            reasons.extend(context.eligibility_reasons or ("instrument_ineligible",))
        if len(context.bars) < 65 or len(context.index_bars) < 65:
            reasons.append("insufficient_closed_daily_bars")
            return self._decision(context, reasons, snapshot)

        bars = list(context.bars)
        index = list(context.index_bars)
        closes = [float(bar.close) for bar in bars]
        index_closes = [float(bar.close) for bar in index]
        volumes = [float(bar.volume) for bar in bars]
        ma20 = _mean(closes[-20:])
        ma20_prev5 = _mean(closes[-25:-5])
        ma60 = _mean(closes[-60:])
        index_ma20 = _mean(index_closes[-20:])
        index_ma20_prev5 = _mean(index_closes[-25:-5])
        index_ma60 = _mean(index_closes[-60:])
        atr = _atr(bars, 14)
        recent_high = max(float(bar.high) for bar in bars[-20:])
        current = bars[-1]
        previous = bars[-2]
        pullback_atr = (recent_high - float(current.close)) / atr if atr else math.inf
        volume_ratio = float(current.volume) / _mean(volumes[-21:-1]) if _mean(volumes[-21:-1]) else 0.0
        day_range = float(current.high) - float(current.low)
        close_location = (
            (float(current.close) - float(current.low)) / day_range if day_range > 0 else 0.5
        )
        pullback_low = min(float(bar.low) for bar in bars[-10:])
        touched_ma20 = pullback_low <= ma20 + s.ma_touch_atr * atr
        recovered = float(current.close) > ma20 and float(current.close) > float(previous.high)

        checks = {
            "market_close_above_ma60": index_closes[-1] > index_ma60,
            "market_ma20_above_ma60": index_ma20 > index_ma60,
            "market_ma20_slope_positive": index_ma20 > index_ma20_prev5,
            "market_breadth_ok": context.market_breadth_pct >= s.market_breadth_min_pct,
            "close_above_ma60": closes[-1] > ma60,
            "ma20_above_ma60": ma20 > ma60,
            "ma20_slope_positive": ma20 > ma20_prev5,
            "pullback_depth_ok": s.pullback_atr_min <= pullback_atr <= s.pullback_atr_max,
            "pullback_touched_support": touched_ma20,
            "daily_recovery_confirmed": recovered,
            "close_location_ok": close_location >= s.close_location_min,
            "volume_ratio_ok": s.volume_ratio_min <= volume_ratio <= s.volume_ratio_max,
        }
        snapshot.update({
            "checks": checks,
            "market_index": {
                "symbol": "000300",
                "close": index_closes[-1],
                "ma20": index_ma20,
                "ma60": index_ma60,
                "ma20_previous_5": index_ma20_prev5,
                "ma20_change_5": index_ma20 - index_ma20_prev5,
            },
            "close": closes[-1],
            "ma20": ma20,
            "ma60": ma60,
            "atr14": atr,
            "recent_high_20": recent_high,
            "pullback_atr": pullback_atr,
            "volume_ratio": volume_ratio,
            "close_location": close_location,
            "market_breadth_pct": context.market_breadth_pct,
        })
        reasons.extend(name for name, passed in checks.items() if not passed)
        if reasons:
            return self._decision(context, reasons, snapshot)

        trigger = _round_to_tick(float(current.high) + context.tick_size, context.tick_size)
        max_gap = min(float(current.close) * s.max_gap_pct / 100.0, atr * s.max_gap_atr)
        max_entry = _round_to_tick(float(current.close) + max_gap, context.tick_size)
        stop = _round_to_tick(pullback_low - s.stop_buffer_atr * atr, context.tick_size)
        stop_atr = (trigger - stop) / atr if atr else math.inf
        snapshot["stop_distance_atr"] = stop_atr
        if not s.stop_distance_atr_min <= stop_atr <= s.stop_distance_atr_max:
            reasons.append("stop_distance_outside_allowed_atr_range")
            return self._decision(context, reasons, snapshot)

        valid_until = context.next_trading_time or _next_weekday_iso(context.signal_time)
        return SignalDecision(
            status=SignalStatus.ALLOW,
            strategy_id=s.strategy_id,
            parameter_version=s.parameter_version,
            pool_version=context.pool_version,
            symbol=context.symbol,
            signal_time=context.signal_time,
            condition_snapshot=snapshot,
            trigger_price=trigger,
            max_entry_price=max_entry,
            initial_stop=stop,
            valid_until=valid_until,
            exit_rules={
                "breakeven_after_r": 1.0,
                "trailing_atr": s.trailing_atr,
                "time_stop_bars": s.time_stop_bars,
                "time_stop_min_r": s.time_stop_min_r,
                "t_plus_one": True,
            },
            invalidation_rules={
                "cancel_if_next_open_above": max_entry,
                "cancel_after": valid_until,
                "no_chase": True,
                "no_add_position": True,
            },
        )

    def _decision(
        self,
        context: StrategyContext,
        reasons: list[str],
        snapshot: dict[str, Any],
    ) -> SignalDecision:
        return SignalDecision(
            status=SignalStatus.REJECT,
            strategy_id=self.settings.strategy_id,
            parameter_version=self.settings.parameter_version,
            pool_version=context.pool_version,
            symbol=context.symbol,
            signal_time=context.signal_time,
            reasons=list(dict.fromkeys(reasons)),
            condition_snapshot=snapshot,
        )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _atr(bars: Sequence[KlineBar], period: int) -> float:
    recent = bars[-(period + 1) :]
    ranges: list[float] = []
    for previous, current in pairwise(recent):
        ranges.append(max(
            float(current.high) - float(current.low),
            abs(float(current.high) - float(previous.close)),
            abs(float(current.low) - float(previous.close)),
        ))
    return _mean(ranges[-period:])


def _round_to_tick(value: float, tick: float) -> float:
    precision = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[-1]))
    return round(round(value / tick) * tick, precision)


def _next_weekday_iso(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    result = dt + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result.isoformat()
