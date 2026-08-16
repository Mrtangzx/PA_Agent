"""Versioned fixed replay suite proving mechanics, never profitability."""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from pa_agent.data.base import KlineBar
from pa_agent.trading.broker_models import AuthorizedOrder
from pa_agent.trading.execution_simulator import AShareCostModel, AShareExecutionSimulator
from pa_agent.trading.lifecycle import TradeLifecycleTracker
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.quant import SignalDecision, SignalStatus
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import (
    TOPDOWN_STRATEGY_ID,
    HotspotItem,
    HotspotSnapshot,
    IndexScoreInput,
    SentimentScoreInput,
    StockScoreInput,
    ThemeScoreInput,
    TopDownScoringContext,
)
from pa_agent.trading.topdown_replay import TopDownReplayEngine, TopDownReplayFrame

FIXTURE_VERSION = "topdown_mechanics_v3_cloud_ai"
SIGNAL_AT = "2026-01-05T15:00:00+08:00"
FIRST = "2026-01-06T09:45:00+08:00"
SECOND = "2026-01-06T10:00:00+08:00"


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    evidence: dict = Field(default_factory=dict)


class FixedMechanismValidationReport(BaseModel):
    strategy_version: str
    scoring_version: str
    fixture_version: str
    status: str
    input_hash: str
    promotion_eligible: bool = False
    checks: list[ValidationCheck] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def run_fixed_mechanism_validation() -> FixedMechanismValidationReport:
    """Run deterministic production logic against a frozen synthetic fixture."""
    engine = TopDownReplayEngine()
    signal = _signal()
    frames = [_frame(FIRST), _frame(SECOND)]
    replay = engine.run(daily_signal=signal, frames=frames)
    future_context = _context(FIRST).model_copy(update={
        "required_source_timestamps": {"theme": SECOND},
    })
    future = engine.run(
        daily_signal=signal,
        frames=[_frame(FIRST, context=future_context)],
    )
    missing = engine.run(
        daily_signal=signal,
        frames=[_frame(FIRST, context=_context(FIRST, missing_theme=True))],
    )
    simulator = AShareExecutionSimulator()
    order = AuthorizedOrder(
        plan_id="fixture",
        account_fingerprint="fixture",
        symbol="600519",
        direction="buy",
        price=100,
        quantity=100,
        stop_loss_price=95,
        strategy_id=engine.scoring.settings.strategy_version,
        authorized_at=FIRST,
        expires_at=SECOND,
    )
    gap_fill = simulator.process_entry(order, _bar(102, 104, 101), max_price=103)
    gap_cancel = simulator.process_entry(order, _bar(104, 105, 103), max_price=103)
    suspended = simulator.process_entry(order, _bar(99, 101, 98), suspended=True)
    limit_locked = simulator.process_entry(order, _bar(99, 101, 98), limit_locked=True)
    t1 = simulator.process_exit(
        entry_price=100,
        stop_price=95,
        target_price=110,
        quantity=100,
        bar=_bar(100, 111, 94),
        bought_same_day=True,
    )
    costs = AShareCostModel().calculate(entry_price=10, exit_price=11, quantity=100)
    lifecycle_checks = _lifecycle_checks()
    checks = [
        ValidationCheck(
            name="two_adjacent_closed_bars_required",
            passed=(
                replay.status == "complete"
                and replay.eligible_count == 1
                and replay.first_eligible_at == SECOND
                and [item.consecutive_pass_count for item in replay.scores] == [1, 2]
            ),
            evidence={"statuses": [item.status for item in replay.scores]},
        ),
        ValidationCheck(
            name="future_timestamp_rejected",
            passed=(
                future.status == "invalid"
                and "frame_1_theme_from_future" in future.hard_failures
            ),
            evidence={"hard_failures": future.hard_failures},
        ),
        ValidationCheck(
            name="missing_theme_not_zero_filled",
            passed=(
                missing.status == "data_incomplete"
                and missing.scores[0].total_score is None
                and "missing_theme" in missing.data_gaps
            ),
            evidence={"data_gaps": missing.data_gaps},
        ),
        ValidationCheck(
            name="gap_fill_and_max_price_cancel",
            passed=(
                gap_fill.status == "filled"
                and gap_fill.price == 102
                and gap_fill.slippage == 2
                and gap_cancel.reason == "gap_above_max_entry"
            ),
            evidence={"fill": gap_fill.model_dump(), "cancel": gap_cancel.model_dump()},
        ),
        ValidationCheck(
            name="suspension_and_limit_lock_blocked",
            passed=(
                suspended.reason == "suspended"
                and limit_locked.reason == "price_limit_locked"
            ),
            evidence={"suspended": suspended.reason, "limit_locked": limit_locked.reason},
        ),
        ValidationCheck(
            name="t_plus_one_locked",
            passed=t1.status == "blocked" and t1.t1_locked,
            evidence=t1.model_dump(),
        ),
        ValidationCheck(
            name="commission_minimum_and_sell_tax",
            passed=(
                costs.buy_commission == 5
                and costs.sell_commission == 5
                and costs.sell_tax == 0.55
            ),
            evidence=costs.model_dump(),
        ),
        *lifecycle_checks,
    ]
    payload = {
        "fixture_version": FIXTURE_VERSION,
        "signal": signal.model_dump(mode="json"),
        "frames": [frame.model_dump(mode="json") for frame in frames],
        "settings": engine.scoring.settings.model_dump(mode="json"),
    }
    return FixedMechanismValidationReport(
        strategy_version=engine.scoring.settings.strategy_version,
        scoring_version=engine.scoring.settings.scoring_version,
        fixture_version=FIXTURE_VERSION,
        status="complete" if all(check.passed for check in checks) else "failed",
        input_hash=_stable_hash(payload),
        checks=checks,
        limitations=[
            "固定回放仅证明时间对齐、评分和成交机制，不证明策略收益。",
            "历史热点、情绪和冻结云算力股票池定义不足时，不得用于当前策略样本外晋级。",
            "仍需至少200笔样本外和12周/80笔影子交易满足晋级门槛。",
        ],
    )


def _signal() -> SignalDecision:
    return SignalDecision(
        status=SignalStatus.ALLOW,
        strategy_id="cloud_ai_daily_pullback_v1",
        parameter_version="1.0.0",
        pool_version="hs300-2026-01",
        symbol="600519",
        signal_time=SIGNAL_AT,
        trigger_price=100,
        max_entry_price=105,
        initial_stop=95,
        condition_snapshot={"stop_distance_atr": 1.5},
    )


def _context(at: str, *, missing_theme: bool = False) -> TopDownScoringContext:
    indexes = [
        IndexScoreInput(
            code=code,
            close_above_ma60=True,
            ma20_above_ma60=True,
            ma20_slope_positive=True,
            intraday_above_vwap_and_ma20_rising=True,
            captured_at=at,
        )
        for code in ("000300", "000001", "000852", "399006")
    ]
    sentiment = SentimentScoreInput(
        advancing_pct=65,
        hs300_above_ma20_pct=70,
        limit_up_count=40,
        limit_down_count=2,
        seal_success_pct=85,
        blast_board_pct=12,
        new_high_count=120,
        new_low_count=20,
        turnover_vs_ma20=1.1,
        broad_index_positive=True,
        captured_at=at,
    )
    hotspot = HotspotSnapshot(
        symbol="600519",
        captured_at=at,
        frozen_at=at,
        industries=["白酒"],
        items=[HotspotItem(
            item_id="fixture-official-1",
            title="冻结的官方行业信息",
            source="固定回放数据",
            published_at=at,
            official=True,
            verified=True,
            positive=True,
            related_themes=["白酒"],
        )],
        positive_score=3,
    ).with_source_hash()
    theme = ThemeScoreInput(
        relative_strength_percentile=90,
        advancing_pct=80,
        main_net_inflow_pct=2.5,
        turnover_vs_recent=1.4,
        persistence_days=4,
        hotspot=hotspot,
        captured_at=at,
    )
    return TopDownScoringContext(
        symbol="600519",
        bar_closed_at=at,
        indexes=indexes,
        sentiment=sentiment,
        theme=None if missing_theme else theme,
        stock=StockScoreInput(
            daily_candidate_passed=True,
            in_trigger_zone=True,
            below_max_entry_price=True,
            breakout_confirmed_on_closed_bar=True,
            above_vwap=True,
            volume_confirmed=True,
            no_intraday_reversal=True,
            tradable=True,
            stop_distance_atr=1.5,
            quote_age_seconds=2,
            quote_deviation_pct=0.1,
            captured_at=at,
        ),
        pool_version="hs300-2026-01",
        daily_signal_id=SIGNAL_AT,
        required_source_timestamps={
            "index": at,
            "sentiment": at,
            "theme": at,
            "quote": at,
        },
    )


def _frame(at: str, **updates) -> TopDownReplayFrame:
    values = {
        "context": _context(at),
        "pool_members": {"600519"},
        "pool_effective_at": "2026-01-01T00:00:00+08:00",
        "pool_source_published_at": "2025-12-31T18:00:00+08:00",
    }
    values.update(updates)
    return TopDownReplayFrame(**values)


def _bar(open_: float, high: float, low: float) -> KlineBar:
    return KlineBar(
        seq=1,
        ts_open=datetime(2026, 1, 6, tzinfo=UTC).timestamp() * 1000,
        open=open_,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=100,
    )


def _lifecycle_checks() -> list[ValidationCheck]:
    with tempfile.TemporaryDirectory(prefix="pa-validation-") as directory:
        store = TradeStore(Path(directory) / "trades.db")
        decision_id = store.add_decision(
            decision_id="lifecycle-fixture-decision",
            symbol="600519",
            timeframe="15m",
            asset_class=AssetClass.A_SHARE.value,
            original_decision={},
            final_decision={},
            meta={"strategy_version": TOPDOWN_STRATEGY_ID},
        )
        plan = TradePlan(
            id="lifecycle-fixture-plan",
            decision_event_id=decision_id,
            symbol="600519",
            timeframe="15m",
            asset_class=AssetClass.A_SHARE,
            direction="buy",
            order_type="limit",
            entry_price=100,
            stop_loss_price=95,
            take_profit_price=110,
            status=PlanStatus.PROPOSED,
            shadow_status="proposed",
            strategy_version=TOPDOWN_STRATEGY_ID,
            created_at="2026-01-05T15:00:00+08:00",
            risk_snapshot={
                "max_entry_price": 103,
                "entry_timeframe": "15m",
                "management_timeframe": "1d",
                "daily_condition_snapshot": {"atr14": 2},
                "exit_rules": {
                    "breakeven_after_r": 1,
                    "trailing_atr": 2,
                    "time_stop_bars": 10,
                    "time_stop_min_r": 0.5,
                    "t_plus_one": True,
                },
            },
        )
        store.add_plan(plan)
        tracker = TradeLifecycleTracker(store)
        tracker.process_closed_bar(
            symbol="600519",
            timeframe="15m",
            bar=_fixture_bar("2026-01-06T10:00:00+08:00", 100, 102, 99, 101),
        )
        entered = store.get_plan(plan.id) or {}
        tracker.process_closed_bar(
            symbol="600519",
            timeframe="15m",
            bar=_fixture_bar("2026-01-06T10:15:00+08:00", 101, 106, 94, 100),
        )
        t1_events = [
            item for item in store.list_events(plan.id)
            if item["event_type"] == "t1_locked_breach" and item["dataset"] == "shadow"
        ]
        daily = _fixture_bar("2026-01-06T15:00:00+08:00", 101, 106, 96, 105)
        tracker.process_closed_bar(symbol="600519", timeframe="1d", bar=daily)
        protected = store.get_plan(plan.id) or {}
        tracker.process_closed_bar(symbol="600519", timeframe="1d", bar=daily)
        duplicate = store.get_plan(plan.id) or {}
        tracker.process_closed_bar(
            symbol="600519",
            timeframe="1d",
            bar=_fixture_bar("2026-01-07T15:00:00+08:00", 102, 103, 100.5, 101),
        )
        results = store.list_results(dataset="shadow")
        return [
            ValidationCheck(
                name="intraday_entry_daily_management",
                passed=(
                    entered.get("shadow_status") == "open"
                    and entered.get("shadow_holding_bars") == 0
                    and protected.get("shadow_holding_bars") == 1
                ),
                evidence={
                    "entry_holding_bars": entered.get("shadow_holding_bars"),
                    "daily_holding_bars": protected.get("shadow_holding_bars"),
                },
            ),
            ValidationCheck(
                name="t1_risk_and_next_bar_trailing_stop",
                passed=(
                    bool(t1_events)
                    and protected.get("shadow_active_stop") == 101
                    and len(results) == 1
                    and results[0].get("exit_price") == 101
                ),
                evidence={
                    "t1_event_count": len(t1_events),
                    "protective_stop": protected.get("shadow_active_stop"),
                    "exit_price": results[0].get("exit_price") if results else None,
                },
            ),
            ValidationCheck(
                name="closed_bar_restart_idempotency",
                passed=(
                    protected.get("shadow_holding_bars") == 1
                    and duplicate.get("shadow_holding_bars") == 1
                ),
                evidence={
                    "first_count": protected.get("shadow_holding_bars"),
                    "duplicate_count": duplicate.get("shadow_holding_bars"),
                },
            ),
        ]


def _fixture_bar(
    at: str, open_: float, high: float, low: float, close: float
) -> KlineBar:
    timestamp = datetime.fromisoformat(at).timestamp() * 1000
    return KlineBar(
        seq=1,
        ts_open=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
        closed=True,
    )


def _stable_hash(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
