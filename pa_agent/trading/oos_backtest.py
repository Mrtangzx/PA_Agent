"""Point-in-time portfolio backtest for the 4:3:2:1 A-share strategy.

The public interface deliberately accepts only a previously packaged OOS ZIP.
All archive parsing, historical-universe reconstruction, daily candidate
generation, frozen 15-minute scoring, portfolio constraints and A-share
execution rules stay behind that seam.  Missing point-in-time evidence fails
closed; it is never replaced by today's constituents or a zero score.
"""
from __future__ import annotations

import hashlib
import json
import math
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.data.base import KlineBar
from pa_agent.trading.broker_models import AuthorizedOrder
from pa_agent.trading.execution_simulator import AShareCostModel, AShareExecutionSimulator
from pa_agent.trading.oos_bundle import OosBundleManifest, validate_oos_bundle
from pa_agent.trading.quant import (
    Hs300DailyPullbackStrategy,
    SignalDecision,
    SignalStatus,
    StrategyContext,
    StrategySettings,
    StrategyState,
)
from pa_agent.trading.stability import PerformanceEvidence, StrategyStabilityController
from pa_agent.trading.topdown import (
    TOPDOWN_STRATEGY_ID,
    HotspotItem,
    HotspotSnapshot,
    IndexScoreInput,
    SentimentScoreInput,
    StockScoreInput,
    ThemeScoreInput,
    TopDownScoreSnapshot,
    TopDownScoreStatus,
    TopDownScoring,
    TopDownScoringContext,
    TopDownScoringSettings,
)
from pa_agent.trading.universe import (
    CLOUD_AI_AUTHORIZATION_SYMBOLS,
    CLOUD_AI_SYMBOLS,
    cloud_ai_universe_version,
)

OOS_BACKTEST_VERSION = "topdown_oos_portfolio_v1"
INDEX_CODES = ("000300", "000001", "000852", "399006")
TRUSTED_HOTSPOT_SOURCE_KINDS = {
    "exchange_announcement",
    "company_announcement",
    "eastmoney_news",
    "eastmoney_board",
    "eastmoney_heat",
}


class OosBacktestSettings(BaseModel):
    initial_equity: float = Field(default=1_000_000, gt=0)
    per_trade_risk_pct: float = Field(default=0.25, gt=0, le=100)
    max_open_risk_pct: float = Field(default=1.0, gt=0, le=100)
    max_positions: int = Field(default=3, ge=1)
    max_new_positions_per_day: int = Field(default=1, ge=1)
    max_position_value_pct: float = Field(default=10.0, gt=0, le=100)
    max_sector_open_risk_pct: float = Field(default=0.75, gt=0, le=100)
    monthly_warning_loss_pct: float = Field(default=1.0, gt=0)
    monthly_stop_loss_pct: float = Field(default=1.5, gt=0)
    monthly_profit_protect_pct: float = Field(default=2.0, gt=0)
    monthly_peak_drawdown_reduce_pct: float = Field(default=0.8, gt=0)
    highest_grade_score: float = Field(default=80.0, ge=70, le=100)
    commission_rate: float = Field(default=0.00025, ge=0)
    minimum_commission: float = Field(default=5.0, ge=0)
    sell_tax_rate: float = Field(default=0.0005, ge=0)
    slippage_rate: float = Field(default=0.0005, ge=0, le=0.05)
    board_lot: int = Field(default=100, ge=1)
    pool_size: int = Field(default=len(CLOUD_AI_AUTHORIZATION_SYMBOLS), ge=1, le=300)


class OosBacktestTrade(BaseModel):
    symbol: str
    pool_version: str
    signal_time: str
    score_time: str
    entered_at: str
    exited_at: str
    entry_price: float
    exit_price: float
    quantity: int
    initial_stop: float
    gross_pnl: float
    fees: float
    net_pnl: float
    r_multiple: float
    holding_days: int
    exit_reason: str
    t1_stop_breach: bool = False
    score_input_hash: str


class OosEquityPoint(BaseModel):
    date: str
    equity: float
    cash: float
    position_count: int


class OosBacktestReport(BaseModel):
    strategy_version: str = TOPDOWN_STRATEGY_ID
    backtest_version: str = OOS_BACKTEST_VERSION
    dataset: str = "out_of_sample"
    validation_epoch_id: str = ""
    pool_version: str = ""
    member_hash: str = ""
    status: str
    input_hash: str
    promotion_eligible: bool = False
    period_start: str = ""
    period_end: str = ""
    pool_versions: int = 0
    daily_signal_count: int = 0
    score_frame_count: int = 0
    eligible_opportunity_count: int = 0
    performance_evidence: dict[str, Any]
    gate_failures: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    trades: list[OosBacktestTrade] = Field(default_factory=list)
    equity_curve: list[OosEquityPoint] = Field(default_factory=list)
    monthly_returns: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    generated_at: str


@dataclass(frozen=True)
class _HistoricalBar:
    symbol: str
    instrument_type: str
    effective_at: datetime
    source_published_at: datetime
    observed_at: datetime
    bar: KlineBar
    signal_bar: KlineBar
    adjustment_factor: float
    amount: float | None
    suspended: bool | None
    limit_locked: bool | None
    is_st: bool | None
    delisting: bool | None
    listed_days: int | None
    industry: str


@dataclass(frozen=True)
class _Constituents:
    effective_at: datetime
    source_published_at: datetime
    symbols: frozenset[str]


@dataclass(frozen=True)
class _Opportunity:
    symbol: str
    signal: SignalDecision
    score: TopDownScoreSnapshot
    fill_at: datetime
    fill_bar: _HistoricalBar
    industry: str


@dataclass
class _Position:
    symbol: str
    pool_version: str
    signal_time: str
    score_time: str
    score_input_hash: str
    entry_at: datetime
    entry_price: float
    entry_adjustment_factor: float
    quantity: int
    initial_stop: float
    active_stop_adjusted: float
    initial_risk_amount: float
    buy_commission: float
    highest_close_adjusted: float
    industry: str
    holding_days: int = 0
    time_exit_pending: bool = False
    t1_stop_breach: bool = False


@dataclass
class _BundleData:
    manifest: OosBundleManifest
    records: dict[str, list[dict[str, Any]]]
    daily: dict[tuple[str, str], list[_HistoricalBar]] = field(default_factory=dict)
    intraday: dict[tuple[str, str], list[_HistoricalBar]] = field(default_factory=dict)
    constituents: list[_Constituents] = field(default_factory=list)
    sentiment: list[tuple[datetime, dict[str, Any]]] = field(default_factory=list)
    hotspots: dict[str, list[tuple[datetime, dict[str, Any]]]] = field(default_factory=dict)


class OosPortfolioBacktester:
    """Run the complete historical 4:3:2:1 decision and portfolio lifecycle."""

    def __init__(
        self,
        *,
        settings: OosBacktestSettings | None = None,
        strategy_settings: StrategySettings | None = None,
        scoring_settings: TopDownScoringSettings | None = None,
        daily_strategy: Any | None = None,
        scoring: TopDownScoring | None = None,
    ) -> None:
        self.settings = settings or OosBacktestSettings()
        self.daily_strategy = daily_strategy or Hs300DailyPullbackStrategy(strategy_settings)
        self.scoring = scoring or TopDownScoring(scoring_settings)
        self.simulator = AShareExecutionSimulator()
        self.cost_model = AShareCostModel(
            commission_rate=self.settings.commission_rate,
            minimum_commission=self.settings.minimum_commission,
            sell_tax_rate=self.settings.sell_tax_rate,
        )

    def run(self, bundle_path: Path) -> OosBacktestReport:
        """Validate and replay one OOS bundle without mutating application state."""
        generated_at = datetime.now().astimezone().isoformat()
        validation = validate_oos_bundle(Path(bundle_path))
        empty_evidence = PerformanceEvidence(dataset="out_of_sample", trade_count=0)
        if validation.strategy_version != TOPDOWN_STRATEGY_ID:
            return OosBacktestReport(
                validation_epoch_id=validation.validation_epoch_id,
                pool_version=validation.pool_version,
                member_hash=validation.member_hash,
                status="data_incomplete",
                input_hash=self._input_hash(validation.input_hash),
                period_start=validation.period_start,
                period_end=validation.period_end,
                performance_evidence=empty_evidence.model_dump(mode="json"),
                data_gaps=["oos_bundle_strategy_mismatch_current_target"],
                warnings=self._warnings(),
                generated_at=generated_at,
            )
        if validation.status != "complete":
            return OosBacktestReport(
                validation_epoch_id=validation.validation_epoch_id,
                pool_version=validation.pool_version,
                member_hash=validation.member_hash,
                status="data_incomplete",
                input_hash=self._input_hash(validation.input_hash),
                period_start=validation.period_start,
                period_end=validation.period_end,
                performance_evidence=empty_evidence.model_dump(mode="json"),
                data_gaps=list(validation.data_gaps),
                warnings=self._warnings(),
                generated_at=generated_at,
            )

        gaps: list[str] = []
        try:
            data = _load_bundle(Path(bundle_path), gaps)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            gaps.append(f"oos_backtest_load_failed:{type(exc).__name__}:{exc}")
            return OosBacktestReport(
                validation_epoch_id=validation.validation_epoch_id,
                pool_version=validation.pool_version,
                member_hash=validation.member_hash,
                status="data_incomplete",
                input_hash=self._input_hash(validation.input_hash),
                period_start=validation.period_start,
                period_end=validation.period_end,
                performance_evidence=empty_evidence.model_dump(mode="json"),
                data_gaps=_unique(gaps),
                warnings=self._warnings(),
                generated_at=generated_at,
            )

        pools = self._build_monthly_pools(data, gaps)
        opportunities, signal_count, frame_count = self._build_opportunities(
            data, pools, gaps
        )
        trades, equity_curve = self._run_portfolio(data, opportunities, gaps)
        monthly_returns = _monthly_returns(
            equity_curve,
            _time(data.manifest.period_start),
            _time(data.manifest.period_end),
            self.settings.initial_equity,
        )
        evidence = self._performance_evidence(
            trades=trades,
            equity_curve=equity_curve,
            monthly_returns=monthly_returns,
            pool_verified=bool(pools) and not any("pool_" in item for item in gaps),
            sources_verified=not any(
                token in item
                for item in gaps
                for token in ("future", "duplicate", "timestamp", "source_")
            ),
            hotspot_sentiment_verified=(
                frame_count > 0
                and not any(
                    token in item
                    for item in gaps
                    for token in ("sentiment_", "hotspot_", "theme_")
                )
            ),
            execution_verified=bool(trades) and not gaps,
        )
        transition = StrategyStabilityController().evaluate(
            StrategyState.CANDIDATE, evidence
        )
        promotion_eligible = (
            not gaps
            and transition.current is StrategyState.SHADOW
            and transition.reasons == ["oos_gate_passed"]
        )
        gate_failures = _oos_gate_failures(evidence)
        return OosBacktestReport(
            validation_epoch_id=data.manifest.validation_epoch_id,
            pool_version=data.manifest.pool_version,
            member_hash=data.manifest.member_hash,
            status="complete" if not gaps else "data_incomplete",
            input_hash=self._input_hash(validation.input_hash),
            promotion_eligible=promotion_eligible,
            period_start=data.manifest.period_start,
            period_end=data.manifest.period_end,
            pool_versions=len(pools),
            daily_signal_count=signal_count,
            score_frame_count=frame_count,
            eligible_opportunity_count=len(opportunities),
            performance_evidence=evidence.model_dump(mode="json"),
            gate_failures=gate_failures,
            data_gaps=_unique(gaps),
            trades=trades,
            equity_curve=equity_curve,
            monthly_returns=monthly_returns,
            warnings=self._warnings(),
            generated_at=generated_at,
        )

    def _build_monthly_pools(
        self, data: _BundleData, gaps: list[str]
    ) -> dict[str, set[str]]:
        if data.manifest.strategy_version == TOPDOWN_STRATEGY_ID:
            return self._build_fixed_cloud_ai_pools(data, gaps)
        return self._build_legacy_hs300_pools(data, gaps)

    def _build_fixed_cloud_ai_pools(
        self, data: _BundleData, gaps: list[str]
    ) -> dict[str, set[str]]:
        index_days = sorted({
            item.effective_at.date()
            for item in data.daily.get(("index", "000300"), [])
        })
        first_days: dict[str, Any] = {}
        for day in index_days:
            first_days.setdefault(day.strftime("%Y-%m"), day)
        defined = set(data.manifest.symbols or CLOUD_AI_SYMBOLS)
        required = set(
            data.manifest.authorization_symbols or CLOUD_AI_AUTHORIZATION_SYMBOLS
        )
        pools: dict[str, set[str]] = {}
        for month, first_day in first_days.items():
            warmup = [
                item for item in data.daily.get(("index", "000300"), [])
                if item.effective_at.date() < first_day
            ]
            if len(warmup) < 65:
                continue
            point = datetime.combine(
                first_day, datetime.min.time(), tzinfo=index_by_day_timezone(data)
            )
            membership = _latest_before(
                data.constituents, point, key=lambda value: value.effective_at
            )
            if membership is None:
                gaps.append(f"pool_cloud_ai_definition_missing:{month}")
                continue
            if membership.source_published_at > point:
                gaps.append(f"pool_cloud_ai_source_from_future:{month}")
                continue
            if set(membership.symbols) != defined:
                gaps.append(f"pool_cloud_ai_definition_mismatch:{month}")
                continue
            incomplete = []
            eligible: set[str] = set()
            for symbol in sorted(required):
                history = [
                    item for item in data.daily.get(("stock", symbol), [])
                    if item.effective_at.date() < first_day
                ][-20:]
                if len(history) < 20 or any(item.amount is None for item in history):
                    incomplete.append(symbol)
                    continue
                latest = history[-1]
                if None in (
                    latest.suspended, latest.limit_locked, latest.is_st,
                    latest.delisting, latest.listed_days,
                ):
                    incomplete.append(symbol)
                    continue
                if (
                    latest.is_st or latest.delisting or latest.suspended
                    or latest.limit_locked or int(latest.listed_days or 0) < 120
                ):
                    continue
                if not latest.industry:
                    incomplete.append(symbol)
                    continue
                eligible.add(symbol)
            if incomplete:
                gaps.append(
                    f"pool_cloud_ai_eligibility_history_incomplete:{month}:"
                    + ",".join(incomplete)
                )
                continue
            # Fixed means no liquidity ranking or substitution. Members that
            # fail a contemporaneous tradability rule are excluded only for
            # that monthly snapshot; another symbol is never substituted.
            pools[month] = eligible
        return pools

    def _build_legacy_hs300_pools(
        self, data: _BundleData, gaps: list[str]
    ) -> dict[str, set[str]]:
        hs300_days = sorted({
            item.effective_at.date()
            for item in data.daily.get(("index", "000300"), [])
        })
        first_days: dict[str, Any] = {}
        for day in hs300_days:
            first_days.setdefault(day.strftime("%Y-%m"), day)
        pools: dict[str, set[str]] = {}
        for month, first_day in first_days.items():
            tzinfo = index_by_day_timezone(data)
            point = datetime.combine(first_day, datetime.min.time(), tzinfo=tzinfo)
            warmup = [
                item for item in data.daily.get(("index", "000300"), [])
                if item.effective_at.date() < first_day
            ]
            # The opening part of a bundle is indicator warm-up, not a missing
            # historical pool.  Validation begins only after 65 closed days.
            if len(warmup) < 65:
                continue
            membership = _latest_before(data.constituents, point, key=lambda value: value.effective_at)
            if membership is None:
                gaps.append(f"pool_historical_constituents_missing:{month}")
                continue
            if membership.source_published_at > point:
                gaps.append(f"pool_constituents_source_from_future:{month}")
                continue
            ranked: list[tuple[float, str]] = []
            incomplete = 0
            for symbol in sorted(membership.symbols):
                history = [
                    item for item in data.daily.get(("stock", symbol), [])
                    if item.effective_at.date() < first_day
                ][-20:]
                if len(history) < 20 or any(item.amount is None for item in history):
                    incomplete += 1
                    continue
                latest = history[-1]
                if None in (
                    latest.suspended, latest.limit_locked, latest.is_st,
                    latest.delisting, latest.listed_days,
                ):
                    incomplete += 1
                    continue
                if (
                    latest.is_st or latest.delisting or latest.suspended
                    or latest.limit_locked or int(latest.listed_days or 0) < 120
                ):
                    continue
                if not latest.industry:
                    incomplete += 1
                    continue
                average = sum(float(item.amount or 0) for item in history) / 20
                ranked.append((average, symbol))
            if incomplete:
                gaps.append(f"pool_liquidity_or_eligibility_history_incomplete:{month}:{incomplete}")
            if len(ranked) < self.settings.pool_size:
                gaps.append(
                    f"pool_fewer_than_{self.settings.pool_size}_eligible_members:{month}:{len(ranked)}"
                )
                continue
            ranked.sort(key=lambda item: (-item[0], item[1]))
            pools[month] = {symbol for _, symbol in ranked[: self.settings.pool_size]}
        return pools

    def _build_opportunities(
        self,
        data: _BundleData,
        pools: dict[str, set[str]],
        gaps: list[str],
    ) -> tuple[list[_Opportunity], int, int]:
        index_daily = data.daily.get(("index", "000300"), [])
        index_by_day = {item.effective_at.date(): item for item in index_daily}
        signal_count = 0
        frame_count = 0
        opportunities: list[_Opportunity] = []
        seen_opportunity: set[tuple[str, Any]] = set()
        for day in sorted(index_by_day):
            month = day.strftime("%Y-%m")
            members = pools.get(month)
            if not members:
                continue
            sentiment_record = _latest_at_or_before(
                data.sentiment, index_by_day[day].effective_at
            )
            if sentiment_record is None:
                gaps.append(f"sentiment_daily_missing:{day.isoformat()}")
                continue
            breadth = _number(sentiment_record[1].get("hs300_above_ma20_pct"))
            if breadth is None:
                gaps.append(f"sentiment_hs300_breadth_missing:{day.isoformat()}")
                continue
            for symbol in sorted(members):
                stock_history = [
                    item for item in data.daily.get(("stock", symbol), [])
                    if item.effective_at.date() <= day
                ]
                market_history = [
                    item for item in index_daily if item.effective_at.date() <= day
                ]
                if len(stock_history) < 65 or len(market_history) < 65:
                    continue
                next_daily = next(
                    (
                        item for item in data.daily.get(("stock", symbol), [])
                        if item.effective_at.date() > day
                    ),
                    None,
                )
                if next_daily is None:
                    continue
                context = StrategyContext(
                    symbol=symbol,
                    bars=tuple(item.signal_bar for item in stock_history),
                    index_bars=tuple(item.signal_bar for item in market_history),
                    market_breadth_pct=breadth,
                    pool_version=(
                        data.manifest.pool_version
                        or cloud_ai_universe_version(day)
                        if data.manifest.strategy_version == TOPDOWN_STRATEGY_ID
                        else f"hs300-{month}"
                    ),
                    signal_time=stock_history[-1].effective_at.isoformat(),
                    next_trading_time=next_daily.effective_at.isoformat(),
                    eligible=True,
                )
                signal = self.daily_strategy.evaluate(context)
                if signal.status is not SignalStatus.ALLOW:
                    continue
                signal = signal.model_copy(update={"strategy_id": TOPDOWN_STRATEGY_ID})
                signal_count += 1
                frames = self._score_next_session(data, signal, members, gaps)
                frame_count += len(frames)
                eligible = next(
                    (
                        score
                        for score in frames
                        if score.status is TopDownScoreStatus.ELIGIBLE_FOR_RISK
                    ),
                    None,
                )
                if eligible is None:
                    continue
                score = eligible
                stock_rows = [
                    item for item in data.intraday.get(("stock", symbol), [])
                    if item.effective_at.date() == datetime.fromisoformat(score.bar_closed_at).date()
                ]
                next_bar = next(
                    (item for item in stock_rows if item.effective_at > datetime.fromisoformat(score.bar_closed_at)),
                    None,
                )
                if next_bar is None:
                    continue
                available_at = [
                    datetime.fromisoformat(value)
                    for key, value in score.source_timestamps.items()
                    if key != "bar" and value
                ]
                if any(value > next_bar.effective_at for value in available_at):
                    gaps.append(
                        "score_source_not_available_before_next_execution_bar:"
                        f"{symbol}:{score.bar_closed_at}"
                    )
                    continue
                identity = (symbol, next_bar.effective_at)
                if identity in seen_opportunity:
                    continue
                seen_opportunity.add(identity)
                opportunities.append(_Opportunity(
                    symbol=symbol,
                    signal=signal,
                    score=score,
                    fill_at=next_bar.effective_at,
                    fill_bar=next_bar,
                    industry=stock_history[-1].industry,
                ))
        opportunities.sort(
            key=lambda item: (
                item.fill_at,
                -(item.score.total_score or 0),
                -(item.score.theme_score or 0),
                item.symbol,
            )
        )
        return opportunities, signal_count, frame_count

    def _score_next_session(
        self,
        data: _BundleData,
        signal: SignalDecision,
        pool_members: set[str],
        gaps: list[str],
    ) -> list[TopDownScoreSnapshot]:
        assert signal.valid_until
        next_day = datetime.fromisoformat(signal.valid_until).date()
        stock_rows = [
            item for item in data.intraday.get(("stock", signal.symbol), [])
            if item.effective_at.date() == next_day
        ]
        previous: TopDownScoreSnapshot | None = None
        scores: list[TopDownScoreSnapshot] = []
        for row in stock_rows:
            if row.effective_at.hour == 9 and row.effective_at.minute < 45:
                continue
            try:
                context = self._scoring_context(
                    data=data,
                    signal=signal,
                    stock_row=row,
                    pool_members=pool_members,
                    previous=previous,
                )
            except ValueError as exc:
                gaps.append(f"score_frame_incomplete:{signal.symbol}:{row.effective_at.isoformat()}:{exc}")
                continue
            score = self.scoring.evaluate(context)
            scores.append(score)
            previous = score
        return scores

    def _scoring_context(
        self,
        *,
        data: _BundleData,
        signal: SignalDecision,
        stock_row: _HistoricalBar,
        pool_members: set[str],
        previous: TopDownScoreSnapshot | None,
    ) -> TopDownScoringContext:
        at = stock_row.effective_at
        indexes = [self._index_input(data, code, at) for code in INDEX_CODES]
        sentiment_record = _latest_at(data.sentiment, at, max_age=timedelta(minutes=15))
        if sentiment_record is None:
            raise ValueError("sentiment_snapshot_missing")
        sentiment = _sentiment_input(sentiment_record[1], sentiment_record[0])
        hotspot_record = _latest_at(
            data.hotspots.get(signal.symbol, []), at, max_age=timedelta(minutes=15)
        )
        if hotspot_record is None:
            raise ValueError("hotspot_snapshot_missing")
        theme = _theme_input(signal.symbol, hotspot_record[1], hotspot_record[0])
        stock = self._stock_input(data, signal, stock_row)
        source_timestamps = {
            # The score becomes available only when the last of the four
            # required index observations has arrived.
            "index": max(datetime.fromisoformat(item.captured_at) for item in indexes).isoformat(),
            "sentiment": sentiment.captured_at,
            "theme": theme.captured_at,
            "quote": stock.captured_at,
        }
        if signal.symbol not in pool_members:
            raise ValueError("not_in_point_in_time_pool")
        return TopDownScoringContext(
            symbol=signal.symbol,
            bar_closed_at=at.isoformat(),
            indexes=indexes,
            sentiment=sentiment,
            theme=theme,
            stock=stock,
            pool_version=signal.pool_version,
            daily_signal_id=signal.signal_time,
            required_source_timestamps=source_timestamps,
            previous_snapshot=previous,
        )

    def _index_input(
        self, data: _BundleData, code: str, at: datetime
    ) -> IndexScoreInput:
        daily = [
            item for item in data.daily.get(("index", code), [])
            if item.effective_at.date() < at.date()
        ]
        minute = [
            item for item in data.intraday.get(("index", code), [])
            if item.effective_at <= at
        ]
        if len(daily) < 65 or len(minute) < 21:
            raise ValueError(f"index_{code}_requires_65_daily_and_21_intraday")
        closes = [item.signal_bar.close for item in daily]
        volumes = [item.signal_bar.volume for item in daily]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        ma20_prev5 = sum(closes[-25:-5]) / 20
        minute_closes = [item.signal_bar.close for item in minute]
        minute_ma20 = sum(minute_closes[-20:]) / 20
        minute_ma20_prev = sum(minute_closes[-21:-1]) / 20
        session = [item for item in minute if item.effective_at.date() == at.date()]
        volume = sum(item.signal_bar.volume for item in session)
        vwap = (
            sum(item.signal_bar.close * item.signal_bar.volume for item in session) / volume
            if volume else minute_closes[-1]
        )
        volume_ma20 = sum(volumes[-21:-1]) / 20
        return IndexScoreInput(
            code=code,
            close_above_ma60=closes[-1] > ma60,
            ma20_above_ma60=ma20 > ma60,
            ma20_slope_positive=ma20 > ma20_prev5,
            intraday_above_vwap_and_ma20_rising=(
                minute_closes[-1] > vwap and minute_ma20 > minute_ma20_prev
            ),
            volume_breakdown=(
                closes[-1] < ma60
                and volume_ma20 > 0
                and volumes[-1] >= volume_ma20 * 1.5
            ),
            captured_at=max(item.observed_at for item in minute[-21:]).isoformat(),
        )

    def _stock_input(
        self, data: _BundleData, signal: SignalDecision, current: _HistoricalBar
    ) -> StockScoreInput:
        rows = [
            item for item in data.intraday.get(("stock", signal.symbol), [])
            if item.effective_at <= current.effective_at
        ]
        if len(rows) < 21:
            raise ValueError("stock_requires_21_intraday_bars")
        if current.suspended is None or current.limit_locked is None:
            raise ValueError("stock_tradability_fields_missing")
        session = [item for item in rows if item.effective_at.date() == current.effective_at.date()]
        volume = sum(item.signal_bar.volume for item in session)
        vwap = (
            sum(item.signal_bar.close * item.signal_bar.volume for item in session) / volume
            if volume else current.signal_bar.close
        )
        prior_volume = sum(item.signal_bar.volume for item in rows[-21:-1]) / 20
        bar_range = current.signal_bar.high - current.signal_bar.low
        close_location = (
            (current.signal_bar.close - current.signal_bar.low) / bar_range
            if bar_range > 0 else 0.5
        )
        trigger = float(signal.trigger_price or 0)
        max_entry = float(signal.max_entry_price or 0)
        first_open = session[0].signal_bar.open
        return StockScoreInput(
            daily_candidate_passed=True,
            in_trigger_zone=trigger <= current.signal_bar.close <= max_entry,
            below_max_entry_price=current.signal_bar.close <= max_entry,
            breakout_confirmed_on_closed_bar=(
                current.signal_bar.close > rows[-2].signal_bar.high
            ),
            above_vwap=current.signal_bar.close > vwap,
            volume_confirmed=(
                prior_volume > 0
                and 0.8 <= current.signal_bar.volume / prior_volume <= 1.8
            ),
            no_intraday_reversal=(
                close_location >= 0.35 and current.signal_bar.close >= vwap
            ),
            tradable=not current.suspended and not current.limit_locked,
            gap_cancelled=first_open > max_entry,
            stop_distance_atr=_number(signal.condition_snapshot.get("stop_distance_atr")),
            quote_age_seconds=0,
            quote_deviation_pct=0,
            existing_position=False,
            captured_at=current.observed_at.isoformat(),
        )

    def _run_portfolio(
        self,
        data: _BundleData,
        opportunities: list[_Opportunity],
        gaps: list[str],
    ) -> tuple[list[OosBacktestTrade], list[OosEquityPoint]]:
        by_day: dict[Any, list[_Opportunity]] = defaultdict(list)
        for item in opportunities:
            by_day[item.fill_at.date()].append(item)
        stock_days = sorted({
            item.effective_at.date()
            for (kind, _), rows in data.daily.items() if kind == "stock"
            for item in rows
        })
        cash = self.settings.initial_equity
        positions: dict[str, _Position] = {}
        trades: list[OosBacktestTrade] = []
        curve: list[OosEquityPoint] = []
        month_key = ""
        month_start_equity = self.settings.initial_equity
        month_peak_equity = self.settings.initial_equity
        for day in stock_days:
            current_month = day.strftime("%Y-%m")
            start_of_day_equity = _mark_to_market(
                cash, positions, data, day, self.cost_model
            )
            if current_month != month_key:
                month_key = current_month
                month_start_equity = start_of_day_equity
                month_peak_equity = start_of_day_equity
            # Only time-stop and gap-stop exits are known at the open.  A later
            # low-of-day stop never frees a morning slot using future data.
            for symbol in list(positions):
                position = positions[symbol]
                daily = _daily_on(data, symbol, day)
                if daily is None:
                    gaps.append(f"execution_daily_bar_missing:{symbol}:{day.isoformat()}")
                    continue
                if not math.isclose(
                    daily.adjustment_factor,
                    position.entry_adjustment_factor,
                    rel_tol=0,
                    abs_tol=1e-12,
                ):
                    gaps.append(
                        f"corporate_action_during_open_position_unsupported:"
                        f"{symbol}:{day.isoformat()}"
                    )
                    continue
                if daily.suspended is None or daily.limit_locked is None:
                    gaps.append(f"execution_tradability_missing:{symbol}:{day.isoformat()}")
                    continue
                if daily.suspended or daily.limit_locked:
                    continue
                if position.time_exit_pending:
                    cash += self._close_position(
                        position, daily.bar.open * (1 - self.settings.slippage_rate),
                        daily.effective_at, "time_stop_next_open", trades
                    )
                    positions.pop(symbol)
                elif daily.signal_bar.open < position.active_stop_adjusted:
                    cash += self._close_position(
                        position,
                        daily.bar.open * (1 - self.settings.slippage_rate),
                        daily.effective_at, "gap_below_protective_stop", trades
                    )
                    positions.pop(symbol)

            candidates = sorted(
                by_day.get(day, []),
                key=lambda item: (
                    item.fill_at,
                    -(item.score.total_score or 0),
                    -(item.score.theme_score or 0),
                    item.symbol,
                ),
            )
            opened_today = 0
            equity_before_entries = _mark_to_market(
                cash, positions, data, day, self.cost_model
            )
            month_return_pct = (
                (equity_before_entries / month_start_equity - 1) * 100
                if month_start_equity else 0.0
            )
            month_peak_equity = max(month_peak_equity, equity_before_entries)
            month_peak_drawdown_pct = (
                (month_peak_equity - equity_before_entries) / month_peak_equity * 100
                if month_peak_equity else 0.0
            )
            for opportunity in candidates:
                if opened_today >= self.settings.max_new_positions_per_day:
                    break
                if len(positions) >= self.settings.max_positions:
                    break
                if opportunity.symbol in positions:
                    continue
                if month_return_pct <= -self.settings.monthly_stop_loss_pct:
                    break
                if (
                    month_return_pct <= -self.settings.monthly_warning_loss_pct
                    and (opportunity.score.total_score or 0) < self.settings.highest_grade_score
                ):
                    continue
                signal = opportunity.signal
                assert signal.trigger_price and signal.initial_stop
                equity = _mark_to_market(cash, positions, data, day, self.cost_model)
                factor = opportunity.fill_bar.adjustment_factor
                entry_trigger = float(signal.trigger_price) / factor
                max_entry = float(signal.max_entry_price or signal.trigger_price) / factor
                initial_stop = float(signal.initial_stop) / factor
                risk_per_share = entry_trigger - initial_stop
                if risk_per_share <= 0:
                    continue
                open_risk = sum(
                    max(
                        0.0,
                        (
                            item.entry_price
                            - item.active_stop_adjusted / item.entry_adjustment_factor
                        ) * item.quantity,
                    )
                    for item in positions.values()
                )
                sector_risk = sum(
                    max(
                        0.0,
                        (
                            item.entry_price
                            - item.active_stop_adjusted / item.entry_adjustment_factor
                        ) * item.quantity,
                    )
                    for item in positions.values()
                    if item.industry == opportunity.industry
                )
                risk_budget = min(
                    equity * self.settings.per_trade_risk_pct / 100,
                    max(0.0, equity * self.settings.max_open_risk_pct / 100 - open_risk),
                    max(
                        0.0,
                        equity * self.settings.max_sector_open_risk_pct / 100
                        - sector_risk,
                    ),
                )
                if (
                    month_return_pct >= self.settings.monthly_profit_protect_pct
                    or month_peak_drawdown_pct
                    >= self.settings.monthly_peak_drawdown_reduce_pct
                ):
                    risk_budget /= 2
                risk_qty = int(risk_budget // risk_per_share // self.settings.board_lot) * self.settings.board_lot
                value_qty = int(
                    (equity * self.settings.max_position_value_pct / 100)
                    // entry_trigger // self.settings.board_lot
                ) * self.settings.board_lot
                cash_qty = int(cash // entry_trigger // self.settings.board_lot) * self.settings.board_lot
                quantity = min(risk_qty, value_qty, cash_qty)
                if quantity <= 0:
                    continue
                order = AuthorizedOrder(
                    plan_id=f"oos-{opportunity.symbol}-{opportunity.fill_at.isoformat()}",
                    account_fingerprint="oos-backtest",
                    symbol=opportunity.symbol,
                    direction="buy",
                    price=entry_trigger,
                    quantity=quantity,
                    stop_loss_price=initial_stop,
                    strategy_id=TOPDOWN_STRATEGY_ID,
                    authorized_at=opportunity.score.bar_closed_at,
                    expires_at=signal.valid_until,
                )
                simulated = self.simulator.process_entry(
                    order,
                    opportunity.fill_bar.bar,
                    suspended=bool(opportunity.fill_bar.suspended),
                    limit_locked=bool(opportunity.fill_bar.limit_locked),
                    max_price=max_entry,
                )
                if simulated.status != "filled" or simulated.price is None:
                    continue
                entry_price = simulated.price * (1 + self.settings.slippage_rate)
                if entry_price > max_entry:
                    continue
                buy_commission = max(
                    self.settings.minimum_commission,
                    entry_price * quantity * self.settings.commission_rate,
                )
                required_cash = entry_price * quantity + buy_commission
                if required_cash > cash:
                    continue
                cash -= required_cash
                positions[opportunity.symbol] = _Position(
                    symbol=opportunity.symbol,
                    pool_version=signal.pool_version,
                    signal_time=signal.signal_time,
                    score_time=opportunity.score.bar_closed_at,
                    score_input_hash=opportunity.score.input_hash,
                    entry_at=opportunity.fill_at,
                    entry_price=entry_price,
                    entry_adjustment_factor=factor,
                    quantity=quantity,
                    initial_stop=initial_stop,
                    active_stop_adjusted=float(signal.initial_stop),
                    initial_risk_amount=(entry_price - initial_stop) * quantity,
                    buy_commission=buy_commission,
                    highest_close_adjusted=entry_price * factor,
                    industry=opportunity.industry,
                )
                opened_today += 1

            for symbol in list(positions):
                position = positions[symbol]
                daily = _daily_on(data, symbol, day)
                if daily is None or daily.effective_at.date() < position.entry_at.date():
                    continue
                if daily.suspended is None or daily.limit_locked is None:
                    gaps.append(
                        f"execution_tradability_missing:{symbol}:{day.isoformat()}"
                    )
                    continue
                position.holding_days += 1
                same_day = day == position.entry_at.date()
                tradable = not bool(daily.suspended) and not bool(daily.limit_locked)
                same_day_low = _post_entry_low(data, position, day)
                effective_low = (
                    same_day_low if same_day else daily.signal_bar.low
                )
                if effective_low is not None and effective_low <= position.active_stop_adjusted:
                    if same_day:
                        position.t1_stop_breach = True
                    elif tradable:
                        exit_price = (
                            position.active_stop_adjusted / daily.adjustment_factor
                        ) * (1 - self.settings.slippage_rate)
                        cash += self._close_position(
                            position, exit_price, daily.effective_at,
                            "protective_stop", trades,
                        )
                        positions.pop(symbol)
                        continue
                risk_per_share = position.entry_price - position.initial_stop
                current_r = (
                    (
                        daily.signal_bar.close
                        - position.entry_price * position.entry_adjustment_factor
                    ) / (risk_per_share * position.entry_adjustment_factor)
                    if risk_per_share > 0 else -math.inf
                )
                if position.holding_days >= 10 and current_r < 0.5:
                    position.time_exit_pending = True
                position.highest_close_adjusted = max(
                    position.highest_close_adjusted, daily.signal_bar.close
                )
                new_stop = position.active_stop_adjusted
                adjusted_entry = position.entry_price * position.entry_adjustment_factor
                adjusted_risk = risk_per_share * position.entry_adjustment_factor
                if position.highest_close_adjusted >= adjusted_entry + adjusted_risk:
                    new_stop = max(new_stop, adjusted_entry)
                atr = _number(signal_atr_from_position(position, opportunities))
                if atr and atr > 0:
                    new_stop = max(new_stop, position.highest_close_adjusted - 2 * atr)
                position.active_stop_adjusted = new_stop

            equity = _mark_to_market(cash, positions, data, day, self.cost_model)
            curve.append(OosEquityPoint(
                date=day.isoformat(),
                equity=round(equity, 6),
                cash=round(cash, 6),
                position_count=len(positions),
            ))
            month_peak_equity = max(month_peak_equity, equity)
        if positions and stock_days:
            final_day = stock_days[-1]
            for symbol in list(positions):
                position = positions[symbol]
                daily = _daily_on(data, symbol, final_day)
                if (
                    daily is None
                    or daily.suspended is None
                    or daily.limit_locked is None
                    or daily.suspended
                    or daily.limit_locked
                ):
                    gaps.append(f"dataset_end_position_not_liquidatable:{symbol}")
                    continue
                cash += self._close_position(
                    position,
                    daily.bar.close * (1 - self.settings.slippage_rate),
                    daily.effective_at,
                    "dataset_end_liquidation",
                    trades,
                )
                positions.pop(symbol)
            if curve:
                curve[-1] = OosEquityPoint(
                    date=final_day.isoformat(),
                    equity=round(
                        _mark_to_market(cash, positions, data, final_day, self.cost_model),
                        6,
                    ),
                    cash=round(cash, 6),
                    position_count=len(positions),
                )
        return trades, curve

    def _close_position(
        self,
        position: _Position,
        exit_price: float,
        exited_at: datetime,
        reason: str,
        trades: list[OosBacktestTrade],
    ) -> float:
        costs = self.cost_model.calculate(
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
        )
        gross = (exit_price - position.entry_price) * position.quantity
        net = gross - costs.total
        trades.append(OosBacktestTrade(
            symbol=position.symbol,
            pool_version=position.pool_version,
            signal_time=position.signal_time,
            score_time=position.score_time,
            entered_at=position.entry_at.isoformat(),
            exited_at=exited_at.isoformat(),
            entry_price=round(position.entry_price, 6),
            exit_price=round(exit_price, 6),
            quantity=position.quantity,
            initial_stop=position.initial_stop,
            gross_pnl=round(gross, 6),
            fees=costs.total,
            net_pnl=round(net, 6),
            r_multiple=(
                round(net / position.initial_risk_amount, 8)
                if position.initial_risk_amount > 0 else 0
            ),
            holding_days=position.holding_days,
            exit_reason=reason,
            t1_stop_breach=position.t1_stop_breach,
            score_input_hash=position.score_input_hash,
        ))
        # Entry cash was deducted at fill.  Return gross sale proceeds less the
        # sell-side commission and tax; the buy commission is already gone.
        return exit_price * position.quantity - costs.sell_commission - costs.sell_tax

    def _performance_evidence(
        self,
        *,
        trades: list[OosBacktestTrade],
        equity_curve: list[OosEquityPoint],
        monthly_returns: dict[str, float],
        pool_verified: bool,
        sources_verified: bool,
        hotspot_sentiment_verified: bool,
        execution_verified: bool,
    ) -> PerformanceEvidence:
        r_values = [item.r_multiple for item in trades]
        profits = sum(max(0.0, item.net_pnl) for item in trades)
        losses = abs(sum(min(0.0, item.net_pnl) for item in trades))
        peak = self.settings.initial_equity
        drawdown = 0.0
        for point in equity_curve:
            peak = max(peak, point.equity)
            if peak:
                drawdown = max(drawdown, (peak - point.equity) / peak * 100)
        return PerformanceEvidence(
            dataset="out_of_sample",
            trade_count=len(trades),
            expectancy_r=(sum(r_values) / len(r_values) if r_values else None),
            profit_factor=(
                profits / losses if losses > 0
                else float("inf") if profits > 0 else None
            ),
            max_drawdown_pct=drawdown,
            profitable_month_ratio=(
                sum(value > 0 for value in monthly_returns.values()) / len(monthly_returns)
                if monthly_returns else None
            ),
            complete_months=len(monthly_returns),
            all_complete_months_profitable=(
                bool(monthly_returns) and all(value > 0 for value in monthly_returns.values())
            ),
            point_in_time_universe_verified=pool_verified,
            source_time_alignment_verified=sources_verified,
            execution_rules_verified=execution_verified,
            hotspot_sentiment_history_verified=hotspot_sentiment_verified,
        )

    def _input_hash(self, bundle_hash: str) -> str:
        return _stable_hash({
            "bundle_hash": bundle_hash,
            "backtest_version": OOS_BACKTEST_VERSION,
            "settings": self.settings.model_dump(mode="json"),
            "strategy": self.daily_strategy.settings.model_dump(mode="json"),
            "scoring": self.scoring.settings.model_dump(mode="json"),
        })

    @staticmethod
    def _warnings() -> list[str]:
        return [
            "样本外回测是策略验证证据; 不构成收益承诺。",
            "热点/情绪/冻结股票池定义和行情缺一项即失败关闭, 不使用当前数据回填历史。",
            "成交采用保守滑点、A股100股整手、T+1、停牌与涨跌停约束。",
        ]


def validate_oos_report_consistency(report: Any) -> PerformanceEvidence:
    """Recompute promotion metrics from a report's auditable detail rows."""
    payload = (
        report.model_dump(mode="json")
        if hasattr(report, "model_dump")
        else dict(report)
    )
    if payload.get("strategy_version") != TOPDOWN_STRATEGY_ID:
        raise ValueError("OOS report strategy mismatch")
    if payload.get("backtest_version") != OOS_BACKTEST_VERSION:
        raise ValueError("OOS report backtest version mismatch")
    input_hash = str(payload.get("input_hash") or "")
    if len(input_hash) != 64 or any(value not in "0123456789abcdef" for value in input_hash):
        raise ValueError("OOS report input hash invalid")
    evidence = PerformanceEvidence.model_validate(
        payload.get("performance_evidence") or {}
    )
    if evidence.dataset != "out_of_sample":
        raise ValueError("OOS report evidence dataset mismatch")
    trades = [OosBacktestTrade.model_validate(item) for item in payload.get("trades") or []]
    curve = [OosEquityPoint.model_validate(item) for item in payload.get("equity_curve") or []]
    monthly_returns = {
        str(key): float(value)
        for key, value in (payload.get("monthly_returns") or {}).items()
    }
    if evidence.trade_count != len(trades):
        raise ValueError("OOS report trade count does not match trade audit rows")
    if payload.get("status") == "complete" and payload.get("data_gaps"):
        raise ValueError("complete OOS report cannot contain data gaps")
    if payload.get("promotion_eligible") and (
        payload.get("status") != "complete" or not trades or not curve
    ):
        raise ValueError("promotion-eligible OOS report requires complete trade and equity audit")

    r_values = [item.r_multiple for item in trades]
    expectancy = sum(r_values) / len(r_values) if r_values else None
    profit = sum(max(0.0, item.net_pnl) for item in trades)
    loss = abs(sum(min(0.0, item.net_pnl) for item in trades))
    profit_factor = (
        profit / loss if loss > 0 else float("inf") if profit > 0 else None
    )
    peak = curve[0].equity if curve else 0.0
    drawdown = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        drawdown = max(
            drawdown,
            (peak - point.equity) / peak * 100 if peak else 0.0,
        )
    profitable_ratio = (
        sum(value > 0 for value in monthly_returns.values()) / len(monthly_returns)
        if monthly_returns else None
    )
    for label, measured, stored in (
        ("expectancy_r", expectancy, evidence.expectancy_r),
        ("profit_factor", profit_factor, evidence.profit_factor),
        ("max_drawdown_pct", drawdown, evidence.max_drawdown_pct),
        ("profitable_month_ratio", profitable_ratio, evidence.profitable_month_ratio),
    ):
        if not _same_optional_number(measured, stored):
            raise ValueError(f"OOS report {label} does not match audit rows")
    if evidence.complete_months != len(monthly_returns):
        raise ValueError("OOS report complete-month count does not match monthly returns")
    if evidence.all_complete_months_profitable != (
        bool(monthly_returns) and all(value > 0 for value in monthly_returns.values())
    ):
        raise ValueError("OOS report profitable-month flag does not match monthly returns")
    return evidence


def _load_bundle(path: Path, gaps: list[str]) -> _BundleData:
    with zipfile.ZipFile(path) as archive:
        manifest = OosBundleManifest.model_validate_json(archive.read("manifest.json"))
        records: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for artifact in manifest.artifacts:
            for line_number, line in enumerate(
                archive.read(artifact.path).decode("utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"record_not_object:{artifact.path}:{line_number}")
                records[artifact.kind].append(value)
    data = _BundleData(manifest=manifest, records=dict(records))
    for kind in ("daily_bars", "intraday_15m"):
        target = data.daily if kind == "daily_bars" else data.intraday
        seen: set[tuple[str, str, datetime]] = set()
        for index, record in enumerate(records.get(kind, []), 1):
            prefix = f"{kind}:{index}"
            instrument_type = str(record.get("instrument_type") or "")
            if instrument_type not in {"stock", "index"}:
                gaps.append(f"instrument_type_missing_or_invalid:{prefix}")
                continue
            symbol = str(record.get("symbol") or "")
            if not (symbol.isdigit() and len(symbol) == 6):
                gaps.append(f"symbol_invalid:{prefix}")
                continue
            effective = _time(str(record.get("effective_at") or ""))
            identity = (instrument_type, symbol, effective)
            if identity in seen:
                gaps.append(f"duplicate_bar:{kind}:{instrument_type}:{symbol}:{effective.isoformat()}")
                continue
            seen.add(identity)
            try:
                open_price = float(record["open"])
                high = float(record["high"])
                low = float(record["low"])
                close = float(record["close"])
                volume = float(record.get("volume") or 0)
                adjustment_factor = float(record.get("adjustment_factor") or 1)
            except (KeyError, TypeError, ValueError):
                gaps.append(f"ohlcv_invalid:{prefix}")
                continue
            if (
                min(open_price, high, low, close, adjustment_factor) <= 0
                or high < max(open_price, close, low)
            ):
                gaps.append(f"ohlc_relationship_invalid:{prefix}")
                continue
            signal_open = float(record.get("adjusted_open") or open_price * adjustment_factor)
            signal_high = float(record.get("adjusted_high") or high * adjustment_factor)
            signal_low = float(record.get("adjusted_low") or low * adjustment_factor)
            signal_close = float(record.get("adjusted_close") or close * adjustment_factor)
            if not all(
                math.isclose(
                    adjusted,
                    raw * adjustment_factor,
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                )
                for raw, adjusted in (
                    (open_price, signal_open), (high, signal_high),
                    (low, signal_low), (close, signal_close),
                )
            ):
                gaps.append(f"adjustment_relationship_invalid:{prefix}")
                continue
            target.setdefault((instrument_type, symbol), []).append(_HistoricalBar(
                symbol=symbol,
                instrument_type=instrument_type,
                effective_at=effective,
                source_published_at=_time(str(record.get("source_published_at") or "")),
                observed_at=_time(str(record.get("observed_at") or "")),
                bar=KlineBar(
                    seq=1,
                    ts_open=effective.timestamp() * 1000,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=float(record["amount"]) if record.get("amount") is not None else None,
                    closed=True,
                ),
                signal_bar=KlineBar(
                    seq=1,
                    ts_open=effective.timestamp() * 1000,
                    open=signal_open,
                    high=signal_high,
                    low=signal_low,
                    close=signal_close,
                    volume=volume,
                    amount=float(record["amount"]) if record.get("amount") is not None else 0,
                    closed=True,
                ),
                adjustment_factor=adjustment_factor,
                amount=float(record["amount"]) if record.get("amount") is not None else None,
                suspended=_optional_bool(record, "suspended"),
                limit_locked=_optional_bool(record, "limit_locked"),
                is_st=_optional_bool(record, "is_st"),
                delisting=_optional_bool(record, "delisting"),
                listed_days=int(record["listed_days"]) if record.get("listed_days") is not None else None,
                industry=str(record.get("industry") or ""),
            ))
        for values in target.values():
            values.sort(key=lambda item: item.effective_at)
    for record in records.get("historical_constituents", []):
        symbols = frozenset(str(item) for item in record.get("symbols") or [])
        data.constituents.append(_Constituents(
            effective_at=_time(str(record.get("effective_at") or "")),
            source_published_at=_time(str(record.get("source_published_at") or "")),
            symbols=symbols,
        ))
    data.constituents.sort(key=lambda item: item.effective_at)
    for record in records.get("market_sentiment", []):
        data.sentiment.append((_time(str(record.get("effective_at") or "")), record))
    data.sentiment.sort(key=lambda item: item[0])
    for record in records.get("hotspots", []):
        symbol = str(record.get("symbol") or "")
        if not symbol:
            gaps.append("hotspot_symbol_missing")
            continue
        for item in record.get("items") or []:
            source_kind = str(item.get("source_kind") or "")
            if source_kind not in TRUSTED_HOTSPOT_SOURCE_KINDS:
                gaps.append(
                    f"hotspot_source_kind_untrusted:{symbol}:"
                    f"{item.get('item_id') or item.get('title') or 'unknown'}"
                )
            if not str(item.get("source_url") or "").startswith("https://"):
                gaps.append(f"hotspot_source_url_invalid:{symbol}")
        data.hotspots.setdefault(symbol, []).append(
            (_time(str(record.get("effective_at") or "")), record)
        )
    for values in data.hotspots.values():
        values.sort(key=lambda item: item[0])
    return data


def _sentiment_input(record: dict[str, Any], at: datetime) -> SentimentScoreInput:
    required = (
        "advancing_pct", "hs300_above_ma20_pct", "limit_up_count",
        "limit_down_count", "seal_success_pct", "blast_board_pct",
        "new_high_count", "new_low_count", "turnover_vs_ma20",
        "broad_index_positive",
    )
    missing = [key for key in required if record.get(key) is None]
    if missing:
        raise ValueError("sentiment_fields_missing:" + ",".join(missing))
    return SentimentScoreInput(
        **{key: record[key] for key in required},
        retreat_or_panic_bars=int(record.get("retreat_or_panic_bars") or 0),
        limit_down_and_blast_worsening=bool(record.get("limit_down_and_blast_worsening", False)),
        systemic_volume_selloff=bool(record.get("systemic_volume_selloff", False)),
        captured_at=str(record.get("observed_at") or at.isoformat()),
    )


def _theme_input(symbol: str, record: dict[str, Any], at: datetime) -> ThemeScoreInput:
    metrics = record.get("theme_metrics") or record
    required = (
        "relative_strength_percentile", "advancing_pct", "main_net_inflow_pct",
        "turnover_vs_recent", "persistence_days",
    )
    missing = [key for key in required if metrics.get(key) is None]
    if missing:
        raise ValueError("theme_fields_missing:" + ",".join(missing))
    items = []
    for index, item in enumerate(record.get("items") or [], 1):
        items.append(HotspotItem(
            item_id=str(item.get("item_id") or f"{symbol}-{at.isoformat()}-{index}"),
            title=str(item.get("title") or ""),
            source=str(item.get("source") or ""),
            source_url=str(item.get("source_url") or ""),
            published_at=str(item.get("published_at") or ""),
            official=bool(item.get("official", False)),
            verified=bool(item.get("verified", False)),
            positive=bool(item.get("positive", False)),
            major_negative=bool(item.get("major_negative", False)),
            related_themes=[str(value) for value in item.get("related_themes") or []],
            risk_code=str(item.get("risk_code") or ""),
            time_valid=bool(item.get("time_valid", False)),
            time_validation_reason=str(item.get("time_validation_reason") or ""),
        ))
    hotspot = HotspotSnapshot(
        symbol=symbol,
        captured_at=str(record.get("observed_at") or record.get("captured_at") or at.isoformat()),
        frozen_at=at.isoformat(),
        industries=[str(value) for value in record.get("industries") or []],
        concepts=[str(value) for value in record.get("concepts") or []],
        items=items,
        board_strength=dict(record.get("board_strength") or {}),
        positive_score=float(record.get("positive_score") or 0),
        negative_blocks=[str(value) for value in record.get("negative_blocks") or []],
        data_gaps=[str(value) for value in record.get("data_gaps") or []],
        rule_version=str(record.get("rule_version") or ""),
        effective_windows_days={
            str(key): int(value)
            for key, value in (record.get("effective_windows_days") or {}).items()
        },
    ).with_source_hash()
    return ThemeScoreInput(
        **{key: metrics[key] for key in required},
        hotspot=hotspot,
        captured_at=at.isoformat(),
    )


def _daily_on(data: _BundleData, symbol: str, day: Any) -> _HistoricalBar | None:
    return next(
        (
            item for item in data.daily.get(("stock", symbol), [])
            if item.effective_at.date() == day
        ),
        None,
    )


def _post_entry_low(
    data: _BundleData, position: _Position, day: Any
) -> float | None:
    rows = [
        item
        for item in data.intraday.get(("stock", position.symbol), [])
        if item.effective_at.date() == day
        and item.effective_at >= position.entry_at
    ]
    return min((item.signal_bar.low for item in rows), default=None)


def index_by_day_timezone(data: _BundleData):
    rows = data.daily.get(("index", "000300"), [])
    if not rows:
        raise ValueError("hs300_daily_bars_missing")
    return rows[0].effective_at.tzinfo


def _mark_to_market(
    cash: float,
    positions: dict[str, _Position],
    data: _BundleData,
    day: Any,
    costs: AShareCostModel,
) -> float:
    value = cash
    for position in positions.values():
        rows = [
            item for item in data.daily.get(("stock", position.symbol), [])
            if item.effective_at.date() <= day
        ]
        price = rows[-1].bar.close if rows else position.entry_price
        sell_commission = max(
            costs.minimum_commission, price * position.quantity * costs.commission_rate
        )
        sell_tax = price * position.quantity * costs.sell_tax_rate
        value += price * position.quantity - sell_commission - sell_tax
    return value


def _monthly_returns(
    curve: list[OosEquityPoint],
    start: datetime,
    end: datetime,
    initial_equity: float,
) -> dict[str, float]:
    if not curve:
        return {}
    by_month: dict[str, OosEquityPoint] = {}
    for point in curve:
        by_month[point.date[:7]] = point
    result: dict[str, float] = {}
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_equity = initial_equity
    while cursor < end:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        key = cursor.strftime("%Y-%m")
        complete_month_end = next_month - timedelta(microseconds=1)
        if start <= cursor and end >= complete_month_end and key in by_month:
            equity = by_month[key].equity
            result[key] = round((equity / previous_equity - 1) * 100, 8)
            previous_equity = equity
        elif key in by_month:
            previous_equity = by_month[key].equity
        cursor = next_month
    return result


def _oos_gate_failures(evidence: PerformanceEvidence) -> list[str]:
    failures: list[str] = []
    if evidence.trade_count < 200:
        failures.append(f"trade_count_below_200:{evidence.trade_count}")
    if evidence.expectancy_r is None or evidence.expectancy_r < 0.15:
        failures.append("expectancy_r_below_0.15")
    if evidence.profit_factor is None or evidence.profit_factor < 1.20:
        failures.append("profit_factor_below_1.20")
    if evidence.max_drawdown_pct is None or evidence.max_drawdown_pct > 10:
        failures.append("max_drawdown_above_10pct_or_missing")
    if evidence.profitable_month_ratio is None or evidence.profitable_month_ratio < 0.75:
        failures.append("profitable_month_ratio_below_75pct")
    for field_name in (
        "point_in_time_universe_verified", "source_time_alignment_verified",
        "execution_rules_verified", "hotspot_sentiment_history_verified",
    ):
        if not getattr(evidence, field_name):
            failures.append(field_name + "_false")
    return failures


def signal_atr_from_position(
    position: _Position, opportunities: list[_Opportunity]
) -> float | None:
    match = next(
        (
            item for item in opportunities
            if item.symbol == position.symbol
            and item.score.input_hash == position.score_input_hash
        ),
        None,
    )
    return _number(match.signal.condition_snapshot.get("atr14")) if match else None


def _latest_at_or_before(
    values: list[tuple[datetime, dict[str, Any]]], at: datetime
) -> tuple[datetime, dict[str, Any]] | None:
    candidates = [item for item in values if item[0] <= at]
    return candidates[-1] if candidates else None


def _latest_at(
    values: list[tuple[datetime, dict[str, Any]]],
    at: datetime,
    *,
    max_age: timedelta,
) -> tuple[datetime, dict[str, Any]] | None:
    candidates = [item for item in values if item[0] <= at]
    if not candidates:
        return None
    latest = candidates[-1]
    return latest if at - latest[0] <= max_age else None


def _latest_before(values: list[Any], at: datetime, *, key) -> Any | None:
    candidates = [item for item in values if key(item) <= at]
    return candidates[-1] if candidates else None


def _optional_bool(record: dict[str, Any], key: str) -> bool | None:
    return bool(record[key]) if key in record and record[key] is not None else None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same_optional_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    if math.isinf(left) or math.isinf(right):
        return left == right
    return math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-7)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
