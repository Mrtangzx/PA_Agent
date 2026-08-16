"""Time-aligned market-data orchestration for the 4:3:2:1 gate.

Only already-closed 15-minute bars are consumed.  Metrics without a trusted,
structured source are left missing so :mod:`pa_agent.trading.topdown` fails
closed with ``DATA_INCOMPLETE`` instead of manufacturing a score.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.data.base import KlineBar
from pa_agent.trading.broker_models import BrokerSnapshot
from pa_agent.trading.hotspots import HOTSPOT_RULE_VERSION
from pa_agent.trading.quant import SignalDecision
from pa_agent.trading.topdown import (
    HotspotSnapshot,
    IndexScoreInput,
    SentimentScoreInput,
    StockScoreInput,
    ThemeScoreInput,
    TopDownScoreSnapshot,
    TopDownScoreStatus,
    TopDownScoring,
    TopDownScoringContext,
)

INDEX_NAMES = {
    "000300": "沪深300",
    "000001": "上证指数",
    "000852": "中证1000",
    "399006": "创业板指",
}


class TopDownContextBuildResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    context: TopDownScoringContext
    data_gaps: list[str] = Field(default_factory=list)
    closed_stock_bar: KlineBar | None = None


class TopDownMarketDataService:
    """Build a reproducible scoring context from frozen public/broker data."""

    def __init__(
        self,
        scoring: TopDownScoring,
        *,
        fetch_index_daily_fn: Callable[..., list[dict[str, Any]]] | None = None,
        fetch_minute_fn: Callable[..., list[dict[str, Any]]] | None = None,
    ) -> None:
        if fetch_index_daily_fn is None or fetch_minute_fn is None:
            from pa_agent.data.eastmoney_client import fetch_index_daily, fetch_stock_minute

            fetch_index_daily_fn = fetch_index_daily_fn or fetch_index_daily
            fetch_minute_fn = fetch_minute_fn or fetch_stock_minute
        self.scoring = scoring
        self.fetch_index_daily = fetch_index_daily_fn
        self.fetch_minute = fetch_minute_fn

    def build_context(
        self,
        *,
        symbol: str,
        daily_signal: SignalDecision,
        pool_snapshot: dict[str, Any],
        broker: BrokerSnapshot,
        hotspot: HotspotSnapshot | None,
        previous_score: TopDownScoreSnapshot | None = None,
        sentiment: SentimentScoreInput | None = None,
        theme_metrics: dict[str, float | int] | None = None,
        captured_at: datetime | None = None,
        authorization_open: bool = False,
    ) -> TopDownContextBuildResult:
        now = (captured_at or datetime.now().astimezone()).astimezone()
        start = (now - timedelta(days=150)).strftime("%Y%m%d")
        end = now.strftime("%Y%m%d")
        minute_start = (now - timedelta(days=5)).strftime("%Y-%m-%d 00:00:00")
        minute_end = now.strftime("%Y-%m-%d %H:%M:%S")
        gaps: list[str] = []
        indexes: list[IndexScoreInput] = []
        expected_close = expected_topdown_bar_close(now)
        component_closes: dict[str, datetime] = {}

        for code, name in INDEX_NAMES.items():
            try:
                daily = _closed_daily_rows(
                    self.fetch_index_daily(code, start_date=start, end_date=end), now
                )
                minute = _closed_rows(
                    self.fetch_minute(
                        code, period="15", start_date=minute_start, end_date=minute_end,
                        adjust="none", is_index=True,
                    ),
                    now,
                )
                item, item_closed_at = _index_input(code, name, daily, minute)
                indexes.append(item)
                component_closes[f"index_{code}"] = item_closed_at
            except (ValueError, TypeError, KeyError) as exc:
                gaps.append(f"index_{code}_incomplete:{exc}")

        stock: StockScoreInput | None = None
        closed_stock_bar: KlineBar | None = None
        try:
            stock_minutes = _closed_rows(
                self.fetch_minute(
                    symbol, period="15", start_date=minute_start, end_date=minute_end,
                    adjust="qfq", is_index=False,
                ),
                now,
            )
            stock, stock_closed_at = _stock_input(
                symbol=symbol,
                rows=stock_minutes,
                signal=daily_signal,
                broker=broker,
                now=now,
            )
            latest_stock = stock_minutes[-1]
            candidate_closed_stock_bar = KlineBar(
                seq=1,
                ts_open=latest_stock["_timestamp"].timestamp() * 1000,
                open=float(latest_stock["open"]),
                high=float(latest_stock["high"]),
                low=float(latest_stock["low"]),
                close=float(latest_stock["close"]),
                volume=float(latest_stock.get("volume") or 0),
                amount=float(latest_stock.get("amount") or 0),
                closed=True,
            )
            component_closes["stock"] = stock_closed_at
            if expected_close is not None and stock_closed_at == expected_close:
                closed_stock_bar = candidate_closed_stock_bar
        except (ValueError, TypeError, KeyError) as exc:
            gaps.append(f"stock_15m_incomplete:{exc}")

        theme: ThemeScoreInput | None = None
        if hotspot is not None and hotspot.rule_version != HOTSPOT_RULE_VERSION:
            gaps.append(
                "hotspot_rule_version_mismatch:"
                f"{hotspot.rule_version or 'missing'}:{HOTSPOT_RULE_VERSION}"
            )
        elif hotspot is not None and theme_metrics is not None:
            required = {
                "relative_strength_percentile", "advancing_pct", "main_net_inflow_pct",
                "turnover_vs_recent", "persistence_days",
            }
            if required.issubset(theme_metrics):
                theme = ThemeScoreInput(
                    **{key: theme_metrics[key] for key in required},
                    hotspot=hotspot,
                    captured_at=hotspot.frozen_at,
                )
            else:
                gaps.append("theme_board_metrics_incomplete")
        else:
            gaps.append("missing_trusted_theme_metrics")
        if sentiment is None:
            gaps.append(
                "missing_trusted_sentiment_snapshot:requires_full_a_share_breadth_limit_"
                "seal_blast_new_high_low_and_hs300_breadth"
            )

        if expected_close is None:
            gaps.append("outside_valid_topdown_capture_window")
            expected_close = _latest_expected_close(now)
        for component, value in component_closes.items():
            if value != expected_close:
                gaps.append(
                    f"bar_time_mismatch:{component}:{value.isoformat()}:"
                    f"expected:{expected_close.isoformat()}"
                )
        bar_closed_at = expected_close.isoformat()
        source_timestamps = {
            "bar": bar_closed_at,
            "broker": broker.captured_at,
            "hotspot": hotspot.frozen_at if hotspot else "",
            "sentiment": sentiment.captured_at if sentiment else "",
        }
        context = TopDownScoringContext(
            symbol=symbol,
            bar_closed_at=bar_closed_at,
            indexes=indexes,
            sentiment=sentiment,
            theme=theme,
            stock=stock,
            pool_version=str(pool_snapshot.get("version") or ""),
            daily_signal_id=daily_signal.signal_time,
            required_source_timestamps=source_timestamps,
            previous_snapshot=previous_score,
            authorization_open=authorization_open,
        )
        return TopDownContextBuildResult(
            context=context,
            data_gaps=list(dict.fromkeys(gaps)),
            closed_stock_bar=closed_stock_bar,
        )

    def evaluate_and_store(self, result: TopDownContextBuildResult, store) -> TopDownScoreSnapshot:
        score = self.evaluate(result)
        store.add_topdown_score(score)
        return score

    def evaluate(self, result: TopDownContextBuildResult) -> TopDownScoreSnapshot:
        """Apply orchestration gaps to the score as a fail-closed result.

        ``TopDownScoring`` only sees the typed inputs.  Time-alignment and
        source-version failures are discovered while building those inputs and
        must invalidate the aggregate score as well; merely appending a gap to
        an otherwise eligible result would expose a contradictory state to the
        per-stock sandbox.
        """
        score = self.scoring.evaluate(result.context)
        gaps = list(dict.fromkeys([*score.data_gaps, *result.data_gaps]))
        if not gaps:
            return score
        return score.model_copy(
            update={
                "total_score": None,
                "consecutive_pass_count": 0,
                "data_gaps": gaps,
                "status": (
                    TopDownScoreStatus.AUTHORIZATION_REVOKED
                    if result.context.authorization_open
                    else TopDownScoreStatus.DATA_INCOMPLETE
                ),
            }
        )


def _closed_rows(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        value = row.get("time")
        timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=now.tzinfo)
        # East Money labels 15-minute K-lines with their bar close time.
        if timestamp <= now:
            result.append({**row, "_timestamp": timestamp})
    return sorted(result, key=lambda item: item["_timestamp"])


def _closed_daily_rows(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    result = _closed_rows(rows, now)
    if now.hour < 15:
        result = [item for item in result if item["_timestamp"].date() < now.date()]
    return result


def _index_input(
    code: str, name: str, daily: list[dict[str, Any]], minute: list[dict[str, Any]]
) -> tuple[IndexScoreInput, datetime]:
    if len(daily) < 65 or len(minute) < 21:
        raise ValueError("requires_65_daily_and_21_closed_15m_bars")
    closes = [float(item["close"]) for item in daily]
    volumes = [float(item.get("volume") or 0) for item in daily]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    ma20_prev5 = sum(closes[-25:-5]) / 20
    minute_closes = [float(item["close"]) for item in minute]
    minute_ma20 = sum(minute_closes[-20:]) / 20
    minute_ma20_prev = sum(minute_closes[-21:-1]) / 20
    session = [item for item in minute if item["_timestamp"].date() == minute[-1]["_timestamp"].date()]
    total_volume = sum(float(item.get("volume") or 0) for item in session)
    vwap = (
        sum(float(item["close"]) * float(item.get("volume") or 0) for item in session)
        / total_volume if total_volume else minute_closes[-1]
    )
    volume_ma20 = sum(volumes[-21:-1]) / 20
    breakdown = closes[-1] < ma60 and volume_ma20 > 0 and volumes[-1] >= volume_ma20 * 1.5
    return IndexScoreInput(
        code=code,
        name=name,
        close_above_ma60=closes[-1] > ma60,
        ma20_above_ma60=ma20 > ma60,
        ma20_slope_positive=ma20 > ma20_prev5,
        intraday_above_vwap_and_ma20_rising=(
            minute_closes[-1] > vwap and minute_ma20 > minute_ma20_prev
        ),
        volume_breakdown=breakdown,
        captured_at=minute[-1]["_timestamp"].isoformat(),
    ), minute[-1]["_timestamp"]


def _stock_input(
    *, symbol: str, rows: list[dict[str, Any]], signal: SignalDecision,
    broker: BrokerSnapshot, now: datetime,
) -> tuple[StockScoreInput, datetime]:
    if len(rows) < 21:
        raise ValueError("requires_21_closed_15m_bars")
    latest = rows[-1]
    quote = broker.quote if broker.quote and broker.quote.symbol == symbol else None
    if quote is None or quote.last_price is None or not quote.captured_at:
        raise ValueError("matching_broker_quote_required")
    if not quote.execution_state_verified:
        raise ValueError("broker_quote_execution_state_unverified")
    quote_at = datetime.fromisoformat(quote.captured_at)
    if quote_at.tzinfo is None:
        quote_at = quote_at.replace(tzinfo=now.tzinfo)
    quote_age = max(0.0, (now - quote_at.astimezone(now.tzinfo)).total_seconds())
    price = float(quote.last_price)
    external_price = float(latest["close"])
    deviation = abs(external_price - price) / price * 100 if price else 100.0
    session = [item for item in rows if item["_timestamp"].date() == latest["_timestamp"].date()]
    total_volume = sum(float(item.get("volume") or 0) for item in session)
    vwap = (
        sum(float(item["close"]) * float(item.get("volume") or 0) for item in session)
        / total_volume if total_volume else external_price
    )
    previous_volumes = [float(item.get("volume") or 0) for item in rows[-21:-1]]
    volume_average = sum(previous_volumes) / len(previous_volumes)
    day_range = float(latest["high"]) - float(latest["low"])
    close_location = (
        (external_price - float(latest["low"])) / day_range if day_range > 0 else 0.5
    )
    trigger = float(signal.trigger_price or 0)
    max_entry = float(signal.max_entry_price or 0)
    stop_atr = signal.condition_snapshot.get("stop_distance_atr")
    return StockScoreInput(
        daily_candidate_passed=signal.status.value == "allow",
        in_trigger_zone=bool(trigger and price >= trigger and max_entry and price <= max_entry),
        below_max_entry_price=bool(max_entry and price <= max_entry),
        breakout_confirmed_on_closed_bar=external_price > float(rows[-2]["high"]),
        above_vwap=external_price > vwap,
        volume_confirmed=(
            volume_average > 0
            and 0.8 <= float(latest.get("volume") or 0) / volume_average <= 1.8
        ),
        no_intraday_reversal=close_location >= 0.35 and external_price >= vwap,
        tradable=not quote.suspended and not quote.limit_locked,
        gap_cancelled=bool(max_entry and price > max_entry),
        stop_distance_atr=float(stop_atr) if stop_atr is not None else None,
        quote_age_seconds=quote_age,
        quote_deviation_pct=deviation,
        existing_position=any(item.symbol == symbol for item in broker.positions),
        captured_at=latest["_timestamp"].isoformat(),
    ), latest["_timestamp"]


def _latest_expected_close(now: datetime) -> datetime:
    minute = now.minute - (now.minute % 15)
    return now.replace(minute=minute, second=0, microsecond=0)


def expected_topdown_bar_close(now: datetime) -> datetime | None:
    """Return the only bar close allowed for a scheduled live evaluation.

    The 09:45 close is the first score snapshot.  It can never authorize an
    order by itself because the strategy still requires two consecutive
    passing snapshots, so the earliest possible authorization remains 10:00.
    """
    local = now.astimezone()
    if local.weekday() >= 5 or local.minute % 15 > 4:
        return None
    minute = local.minute - local.minute % 15
    candidate = local.replace(minute=minute, second=0, microsecond=0)
    clock = (candidate.hour, candidate.minute)
    if (9, 45) <= clock <= (11, 30) or (13, 15) <= clock <= (15, 0):
        return candidate
    return None


def expected_oos_market_close(now: datetime) -> datetime | None:
    """Return the live 15m close or the bounded 15:00 daily recovery close.

    Intraday, sentiment and theme evidence remains limited to five minutes.
    The daily bar has a separately documented fifteen-minute availability
    window, so 15:05-15:14 may recover only the daily warm-up observation.
    """
    live_close = expected_topdown_bar_close(now)
    if live_close is not None:
        return live_close
    local = now.astimezone()
    if local.weekday() >= 5:
        return None
    if local.hour == 15 and 5 <= local.minute <= 14:
        return local.replace(hour=15, minute=0, second=0, microsecond=0)
    return None
