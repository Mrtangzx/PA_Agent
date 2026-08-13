"""Deterministic A-share 4:3:2:1 intraday scoring and hotspot snapshots.

The module deliberately performs no network or AI work.  Callers must freeze
all inputs at a closed 15-minute bar before evaluation.  Missing mandatory
inputs fail closed instead of being converted to zero points.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TOPDOWN_STRATEGY_ID = "cloud_ai_topdown_4321_intraday_v1"
LEGACY_TOPDOWN_STRATEGY_ID = "hs300_topdown_4321_intraday_v1"
MANUAL_EXCEPTION_STRATEGY_ID = "manual_exception_4321_v1"
REQUIRED_INDEX_WEIGHTS = {
    "000300": 12.0,  # 沪深300
    "000001": 10.0,  # 上证指数
    "000852": 9.0,   # 中证1000
    "399006": 9.0,   # 创业板指
}


class TopDownScoreStatus(StrEnum):
    DATA_INCOMPLETE = "data_incomplete"
    BLOCKED = "blocked"
    OBSERVE = "observe"
    WAIT_CONFIRMATION = "wait_confirmation"
    ELIGIBLE_FOR_RISK = "eligible_for_risk"
    AUTHORIZATION_REVOKED = "authorization_revoked"


class TopDownScoringSettings(BaseModel):
    """Versioned thresholds for the deterministic 4:3:2:1 gate."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    strategy_version: str = TOPDOWN_STRATEGY_ID
    scoring_version: str = "1.0.0"
    bar_timeframe: str = "15m"
    observe_threshold: float = Field(default=60.0, ge=0, le=100)
    pass_threshold: float = Field(default=70.0, ge=0, le=100)
    revoke_threshold: float = Field(default=65.0, ge=0, le=100)
    consecutive_pass_bars: int = Field(default=2, ge=1, le=10)
    minimum_index_score: float = Field(default=24.0, ge=0, le=40)
    minimum_sentiment_score: float = Field(default=15.0, ge=0, le=30)
    minimum_market_breadth_pct: float = Field(default=40.0, ge=0, le=100)
    hotspot_refresh_seconds: int = Field(default=300, ge=60, le=3600)
    outside_pool_risk_multiplier: float = Field(default=0.5, gt=0, le=1)
    max_outside_pool_positions: int = Field(default=1, ge=0, le=3)


class IndexScoreInput(BaseModel):
    code: str
    name: str = ""
    close_above_ma60: bool
    ma20_above_ma60: bool
    ma20_slope_positive: bool
    intraday_above_vwap_and_ma20_rising: bool
    volume_breakdown: bool = False
    captured_at: str


class SentimentScoreInput(BaseModel):
    advancing_pct: float = Field(ge=0, le=100)
    hs300_above_ma20_pct: float = Field(ge=0, le=100)
    limit_up_count: int = Field(ge=0)
    limit_down_count: int = Field(ge=0)
    seal_success_pct: float = Field(ge=0, le=100)
    blast_board_pct: float = Field(ge=0, le=100)
    new_high_count: int = Field(ge=0)
    new_low_count: int = Field(ge=0)
    turnover_vs_ma20: float = Field(ge=0)
    broad_index_positive: bool
    retreat_or_panic_bars: int = Field(default=0, ge=0)
    limit_down_and_blast_worsening: bool = False
    systemic_volume_selloff: bool = False
    captured_at: str


class HotspotItem(BaseModel):
    item_id: str
    title: str
    source: str
    source_url: str = ""
    published_at: str
    official: bool = False
    verified: bool = False
    positive: bool = False
    major_negative: bool = False
    related_themes: list[str] = Field(default_factory=list)
    risk_code: str = ""


class HotspotSnapshot(BaseModel):
    symbol: str
    captured_at: str
    frozen_at: str
    industries: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    items: list[HotspotItem] = Field(default_factory=list)
    board_strength: dict[str, Any] = Field(default_factory=dict)
    positive_score: float = Field(default=0, ge=0, le=3)
    negative_blocks: list[str] = Field(default_factory=list)
    source_hash: str = ""

    def with_source_hash(self) -> HotspotSnapshot:
        payload = self.model_dump(mode="json", exclude={"source_hash"})
        return self.model_copy(update={"source_hash": _stable_hash(payload)})


class ThemeScoreInput(BaseModel):
    relative_strength_percentile: float = Field(ge=0, le=100)
    advancing_pct: float = Field(ge=0, le=100)
    main_net_inflow_pct: float
    turnover_vs_recent: float = Field(ge=0)
    persistence_days: int = Field(ge=0)
    hotspot: HotspotSnapshot
    captured_at: str


class StockScoreInput(BaseModel):
    daily_candidate_passed: bool
    in_trigger_zone: bool
    below_max_entry_price: bool
    breakout_confirmed_on_closed_bar: bool
    above_vwap: bool
    volume_confirmed: bool
    no_intraday_reversal: bool
    tradable: bool
    gap_cancelled: bool = False
    stop_distance_atr: float | None = None
    quote_age_seconds: float | None = None
    quote_deviation_pct: float | None = None
    existing_position: bool = False
    captured_at: str


class TopDownScoringContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    bar_closed_at: str
    indexes: list[IndexScoreInput]
    sentiment: SentimentScoreInput | None
    theme: ThemeScoreInput | None
    stock: StockScoreInput | None
    pool_version: str
    daily_signal_id: str = ""
    required_source_timestamps: dict[str, str] = Field(default_factory=dict)
    previous_snapshot: TopDownScoreSnapshot | None = None
    authorization_open: bool = False


class TopDownScoreSnapshot(BaseModel):
    strategy_version: str
    scoring_version: str
    symbol: str
    pool_version: str
    bar_closed_at: str
    index_score: float | None = None
    sentiment_score: float | None = None
    theme_score: float | None = None
    stock_score: float | None = None
    total_score: float | None = None
    consecutive_pass_count: int = 0
    hard_blocks: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    component_details: dict[str, Any] = Field(default_factory=dict)
    source_timestamps: dict[str, str] = Field(default_factory=dict)
    input_hash: str
    status: TopDownScoreStatus

    @property
    def eligible_for_risk(self) -> bool:
        return self.status is TopDownScoreStatus.ELIGIBLE_FOR_RISK


class TopDownScoring:
    """Pure evaluator for the versioned 4:3:2:1 gate."""

    def __init__(self, settings: TopDownScoringSettings | None = None) -> None:
        self.settings = settings or TopDownScoringSettings()

    def evaluate(self, context: TopDownScoringContext) -> TopDownScoreSnapshot:
        s = self.settings
        input_payload = context.model_dump(mode="json")
        input_hash = _stable_hash({"settings": s.model_dump(mode="json"), "input": input_payload})
        data_gaps = self._data_gaps(context)
        source_timestamps = dict(context.required_source_timestamps)
        source_timestamps.setdefault("bar", context.bar_closed_at)
        if data_gaps:
            return TopDownScoreSnapshot(
                strategy_version=s.strategy_version,
                scoring_version=s.scoring_version,
                symbol=context.symbol,
                pool_version=context.pool_version,
                bar_closed_at=context.bar_closed_at,
                data_gaps=data_gaps,
                source_timestamps=source_timestamps,
                input_hash=input_hash,
                status=TopDownScoreStatus.DATA_INCOMPLETE,
            )

        assert context.sentiment and context.theme and context.stock
        index_score, index_details, index_blocks = self._index_score(context.indexes)
        sentiment_score, sentiment_details, sentiment_blocks = self._sentiment_score(
            context.sentiment
        )
        theme_score, theme_details, theme_blocks = self._theme_score(context.theme)
        stock_score, stock_details, stock_blocks = self._stock_score(context.stock)
        total = round(index_score + sentiment_score + theme_score + stock_score, 4)
        hard_blocks = list(
            dict.fromkeys(index_blocks + sentiment_blocks + theme_blocks + stock_blocks)
        )
        passed_now = total >= s.pass_threshold and not hard_blocks
        previous = context.previous_snapshot
        consecutive = 1 if passed_now else 0
        if (
            passed_now
            and previous is not None
            and previous.symbol == context.symbol
            and previous.total_score is not None
            and previous.total_score >= s.pass_threshold
            and not previous.hard_blocks
            and _is_adjacent_a_share_bar(previous.bar_closed_at, context.bar_closed_at)
        ):
            consecutive = previous.consecutive_pass_count + 1

        if context.authorization_open and (total < s.revoke_threshold or hard_blocks):
            status = TopDownScoreStatus.AUTHORIZATION_REVOKED
        elif hard_blocks or total < s.observe_threshold:
            status = TopDownScoreStatus.BLOCKED
        elif total < s.pass_threshold:
            status = TopDownScoreStatus.OBSERVE
        elif consecutive < s.consecutive_pass_bars:
            status = TopDownScoreStatus.WAIT_CONFIRMATION
        else:
            status = TopDownScoreStatus.ELIGIBLE_FOR_RISK

        return TopDownScoreSnapshot(
            strategy_version=s.strategy_version,
            scoring_version=s.scoring_version,
            symbol=context.symbol,
            pool_version=context.pool_version,
            bar_closed_at=context.bar_closed_at,
            index_score=index_score,
            sentiment_score=sentiment_score,
            theme_score=theme_score,
            stock_score=stock_score,
            total_score=total,
            consecutive_pass_count=consecutive,
            hard_blocks=hard_blocks,
            component_details={
                "index": index_details,
                "sentiment": sentiment_details,
                "theme": theme_details,
                "stock": stock_details,
            },
            source_timestamps=source_timestamps,
            input_hash=input_hash,
            status=status,
        )

    @staticmethod
    def _data_gaps(context: TopDownScoringContext) -> list[str]:
        gaps: list[str] = []
        codes = {item.code for item in context.indexes}
        for code in REQUIRED_INDEX_WEIGHTS:
            if code not in codes:
                gaps.append(f"missing_index_{code}")
        if context.sentiment is None:
            gaps.append("missing_sentiment")
        if context.theme is None:
            gaps.append("missing_theme")
        if context.stock is None:
            gaps.append("missing_stock")
        if not context.pool_version:
            gaps.append("missing_pool_version")
        if not context.daily_signal_id:
            gaps.append("missing_daily_signal")
        for name, timestamp in context.required_source_timestamps.items():
            if not timestamp:
                gaps.append(f"missing_timestamp_{name}")
        return gaps

    def _index_score(self, inputs: list[IndexScoreInput]) -> tuple[float, dict, list[str]]:
        by_code = {item.code: item for item in inputs}
        score = 0.0
        details: dict[str, Any] = {}
        bearish = 0
        for code, weight in REQUIRED_INDEX_WEIGHTS.items():
            item = by_code[code]
            points = weight * (
                0.30 * item.close_above_ma60
                + 0.25 * item.ma20_above_ma60
                + 0.20 * item.ma20_slope_positive
                + 0.25 * item.intraday_above_vwap_and_ma20_rising
            )
            score += points
            if not item.close_above_ma60 and not item.ma20_slope_positive:
                bearish += 1
            details[code] = {"name": item.name, "score": round(points, 4), "weight": weight}
        blocks: list[str] = []
        if score < self.settings.minimum_index_score:
            blocks.append("index_score_below_24")
        if bearish >= 3:
            blocks.append("three_indexes_bearish")
        if by_code["000300"].volume_breakdown:
            blocks.append("hs300_volume_breakdown")
        return round(score, 4), details, blocks

    def _sentiment_score(self, item: SentimentScoreInput) -> tuple[float, dict, list[str]]:
        advancing = _bucket(item.advancing_pct, [(60, 8), (55, 6), (50, 4), (45, 2)])
        breadth = _bucket(item.hs300_above_ma20_pct, [(65, 6), (55, 4.5), (45, 3), (40, 1.5)])
        if item.limit_down_count == 0:
            limit_relation = 5.0 if item.limit_up_count >= 10 else 3.0
        else:
            ratio = item.limit_up_count / item.limit_down_count
            limit_relation = _bucket(ratio, [(5, 5), (3, 4), (1.5, 2.5), (1, 1)])
        board_quality = min(4.0, 4.0 * (item.seal_success_pct / 100) * (1 - item.blast_board_pct / 100))
        high_low_total = item.new_high_count + item.new_low_count
        high_low = 1.5 if high_low_total == 0 else 3 * item.new_high_count / high_low_total
        turnover = min(4.0, 4.0 * min(item.turnover_vs_ma20, 1.2) / 1.2)
        if not item.broad_index_positive:
            turnover *= 0.5
        score = round(advancing + breadth + limit_relation + board_quality + high_low + turnover, 4)
        mood = (
            "强修复" if score >= 25 else "活跃" if score >= 21 else "中性"
            if score >= 15 else "退潮" if score >= 9 else "恐慌"
        )
        blocks: list[str] = []
        if score < self.settings.minimum_sentiment_score:
            blocks.append("sentiment_score_below_15")
        if item.hs300_above_ma20_pct < self.settings.minimum_market_breadth_pct:
            blocks.append("market_breadth_below_40")
        if item.retreat_or_panic_bars >= 2:
            blocks.append("sentiment_retreat_or_panic_two_bars")
        if item.limit_down_and_blast_worsening:
            blocks.append("limit_down_and_blast_worsening")
        if item.systemic_volume_selloff:
            blocks.append("systemic_volume_selloff")
        details = {
            "advancing": advancing,
            "breadth": breadth,
            "limit_relation": limit_relation,
            "board_quality": round(board_quality, 4),
            "new_high_low": round(high_low, 4),
            "turnover": round(turnover, 4),
            "mood": mood,
        }
        return score, details, blocks

    @staticmethod
    def _theme_score(item: ThemeScoreInput) -> tuple[float, dict, list[str]]:
        relative = min(6.0, 6 * item.relative_strength_percentile / 100)
        advancing = min(4.0, 4 * item.advancing_pct / 100)
        flow = min(4.0, max(0.0, 2 * max(item.main_net_inflow_pct, 0) / 2 + 2 * min(item.turnover_vs_recent, 1.5) / 1.5))
        persistence = min(3.0, item.persistence_days * 0.75)
        verified_hotspot = any(
            value.positive and value.verified and value.source for value in item.hotspot.items
        )
        hotspot = item.hotspot.positive_score if verified_hotspot else 0.0
        score = round(relative + advancing + flow + persistence + hotspot, 4)
        blocks = list(item.hotspot.negative_blocks)
        blocks.extend(
            f"major_negative_{value.risk_code or value.item_id}"
            for value in item.hotspot.items
            if value.official and value.verified and value.major_negative
        )
        details = {
            "relative_strength": round(relative, 4),
            "advancing": round(advancing, 4),
            "flow_and_turnover": round(flow, 4),
            "persistence": round(persistence, 4),
            "verified_hotspot": round(hotspot, 4),
            "hotspot_titles": [value.title for value in item.hotspot.items[:5]],
        }
        return min(20.0, score), details, list(dict.fromkeys(blocks))

    @staticmethod
    def _stock_score(item: StockScoreInput) -> tuple[float, dict, list[str]]:
        trigger = 3.0 if item.in_trigger_zone and item.below_max_entry_price else 0.0
        breakout = 3.0 if item.breakout_confirmed_on_closed_bar else 0.0
        vwap_volume = 2.0 if item.above_vwap and item.volume_confirmed else 0.0
        tradability = 2.0 if item.no_intraday_reversal and item.tradable else 0.0
        blocks: list[str] = []
        if not item.daily_candidate_passed:
            blocks.append("daily_candidate_not_passed")
        if not item.below_max_entry_price:
            blocks.append("above_max_entry_price")
        if item.gap_cancelled:
            blocks.append("gap_cancelled")
        if item.stop_distance_atr is None or not 1 <= item.stop_distance_atr <= 3:
            blocks.append("stop_distance_outside_1_to_3_atr")
        if item.quote_age_seconds is None or item.quote_age_seconds > 5:
            blocks.append("broker_quote_stale")
        if item.quote_deviation_pct is None or item.quote_deviation_pct > 0.5:
            blocks.append("external_and_broker_quote_deviation")
        if item.existing_position:
            blocks.append("position_already_exists_no_adding")
        if not item.tradable:
            blocks.append("stock_not_tradable")
        score = trigger + breakout + vwap_volume + tradability
        return score, {
            "trigger_zone": trigger,
            "closed_bar_breakout": breakout,
            "vwap_and_volume": vwap_volume,
            "tradability": tradability,
        }, blocks


def _bucket(value: float, levels: list[tuple[float, float]]) -> float:
    for threshold, score in levels:
        if value >= threshold:
            return float(score)
    return 0.0


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_adjacent_a_share_bar(previous: str, current: str) -> bool:
    """True only for adjacent closed 15-minute A-share trading bars."""
    try:
        before = datetime.fromisoformat(previous)
        after = datetime.fromisoformat(current)
    except ValueError:
        return False
    if before.tzinfo is None or after.tzinfo is None or before.date() != after.date():
        return False
    if after <= before:
        return False
    if before.hour == 11 and before.minute == 30:
        return after.hour == 13 and after.minute == 15
    return after - before == timedelta(minutes=15)
