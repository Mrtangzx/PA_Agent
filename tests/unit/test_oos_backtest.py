from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone

from pa_agent.trading.oos_backtest import OosBacktestSettings, OosPortfolioBacktester
from pa_agent.trading.quant import SignalDecision, SignalStatus, StrategySettings

TZ8 = timezone(timedelta(hours=8))
PUBLISHED = "2025-12-31T18:00:00+08:00"
TARGET = "000100"


class _OneSignalStrategy:
    settings = StrategySettings()

    @staticmethod
    def evaluate(context) -> SignalDecision:
        allowed = context.signal_time.startswith("2026-04-01") and context.symbol == TARGET
        return SignalDecision(
            status=SignalStatus.ALLOW if allowed else SignalStatus.REJECT,
            strategy_id="hs300_daily_pullback_v1",
            parameter_version="fixture-v1",
            pool_version=context.pool_version,
            symbol=context.symbol,
            signal_time=context.signal_time,
            reasons=[] if allowed else ["fixture_not_signal_day"],
            condition_snapshot={"atr14": 5.0, "stop_distance_atr": 1.0},
            trigger_price=100 if allowed else None,
            max_entry_price=110 if allowed else None,
            initial_stop=95 if allowed else None,
            valid_until="2026-04-02T15:00:00+08:00" if allowed else "",
        )


def _record(at: datetime, **values):
    return {
        "effective_at": at.isoformat(),
        "source_published_at": PUBLISHED,
        **values,
    }


def _build_bundle(
    tmp_path,
    *,
    future_hotspot: bool = False,
    untrusted_hotspot: bool = False,
    corporate_action: bool = False,
):
    start = datetime(2026, 1, 1, 15, tzinfo=TZ8)
    days = [start + timedelta(days=index) for index in range(120)]
    symbols = [f"{index:06d}" for index in range(1, 301)]
    constituents = [_record(
        datetime(2026, 1, 1, 0, tzinfo=TZ8),
        symbols=symbols,
    )]
    daily = []
    for day_index, at in enumerate(days):
        base = 70 + day_index * 0.25
        for code in ("000300", "000001", "000852", "399006"):
            daily.append(_record(
                at,
                instrument_type="index",
                symbol=code,
                open=base,
                high=base + 1.2,
                low=base - 0.6,
                close=base + 0.8,
                volume=1_000_000 + day_index * 1000,
                amount=100_000_000,
                adjustment_factor=1,
            ))
        for symbol in symbols:
            adjustment_factor = (
                0.5
                if corporate_action
                and symbol == TARGET
                and at.date() >= datetime(2026, 4, 3, tzinfo=TZ8).date()
                else 1
            )
            stock_base = 90 + day_index * 0.05
            if at.date() == datetime(2026, 4, 2, tzinfo=TZ8).date() and symbol == TARGET:
                raw = (103, 108, 98, 105)
            elif at.date() == datetime(2026, 4, 3, tzinfo=TZ8).date() and symbol == TARGET:
                raw = (96, 99, 93, 94)
            else:
                raw = (stock_base, stock_base + 1, stock_base - 1, stock_base + 0.4)
            daily.append(_record(
                at,
                instrument_type="stock",
                symbol=symbol,
                open=raw[0],
                high=raw[1],
                low=raw[2],
                close=raw[3],
                volume=100_000,
                amount=50_000_000 if symbol == TARGET else 10_000_000,
                adjustment_factor=adjustment_factor,
                suspended=False,
                limit_locked=False,
                is_st=False,
                delisting=False,
                listed_days=500,
                industry="fixture-industry",
            ))

    intraday = []
    previous = datetime(2026, 4, 1, 9, 45, tzinfo=TZ8)
    for index in range(20):
        at = previous + timedelta(minutes=15 * index)
        for code in ("000300", "000001", "000852", "399006"):
            value = 90 + index * 0.2
            intraday.append(_record(
                at,
                instrument_type="index",
                symbol=code,
                open=value,
                high=value + 0.4,
                low=value - 0.2,
                close=value + 0.3,
                volume=100_000,
                amount=10_000_000,
                adjustment_factor=1,
            ))
        value = 98 + index * 0.05
        intraday.append(_record(
            at,
            instrument_type="stock",
            symbol=TARGET,
            open=value,
            high=value + 0.3,
            low=value - 0.2,
            close=value + 0.2,
            volume=100_000,
            amount=10_000_000,
            adjustment_factor=1,
            suspended=False,
            limit_locked=False,
            is_st=False,
            delisting=False,
            listed_days=500,
            industry="fixture-industry",
        ))
    current_rows = [
        (datetime(2026, 4, 2, 9, 45, tzinfo=TZ8), 101.0, 102.0),
        (datetime(2026, 4, 2, 10, 0, tzinfo=TZ8), 102.0, 103.0),
        (datetime(2026, 4, 2, 10, 15, tzinfo=TZ8), 103.0, 104.0),
    ]
    for at, open_price, close in current_rows:
        for code in ("000300", "000001", "000852", "399006"):
            intraday.append(_record(
                at,
                instrument_type="index",
                symbol=code,
                open=120,
                high=122,
                low=119.5,
                close=121.5,
                volume=100_000,
                amount=10_000_000,
                adjustment_factor=1,
            ))
        intraday.append(_record(
            at,
            instrument_type="stock",
            symbol=TARGET,
            open=open_price,
            high=close + 0.5,
            low=open_price - 0.2,
            close=close,
            volume=100_000,
            amount=10_000_000,
            adjustment_factor=1,
            suspended=False,
            limit_locked=False,
            is_st=False,
            delisting=False,
            listed_days=500,
            industry="fixture-industry",
        ))

    sentiment = [_record(
        datetime(2026, 4, 1, 15, tzinfo=TZ8),
        advancing_pct=70,
        hs300_above_ma20_pct=75,
        limit_up_count=60,
        limit_down_count=2,
        seal_success_pct=90,
        blast_board_pct=5,
        new_high_count=200,
        new_low_count=10,
        turnover_vs_ma20=1.2,
        broad_index_positive=True,
    )]
    for at, _, _ in current_rows[:2]:
        sentiment.append(_record(
            at,
            advancing_pct=70,
            hs300_above_ma20_pct=75,
            limit_up_count=60,
            limit_down_count=2,
            seal_success_pct=90,
            blast_board_pct=5,
            new_high_count=200,
            new_low_count=10,
            turnover_vs_ma20=1.2,
            broad_index_positive=True,
        ))
    item_published = (
        "2026-04-03T09:00:00+08:00" if future_hotspot else PUBLISHED
    )
    hotspots = []
    for at, _, _ in current_rows[:2]:
        hotspots.append(_record(
            at,
            frozen_at=at.isoformat(),
            symbol=TARGET,
            industries=["fixture-industry"],
            concepts=["fixture-theme"],
            relative_strength_percentile=100,
            advancing_pct=100,
            main_net_inflow_pct=3,
            turnover_vs_recent=1.5,
            persistence_days=5,
            positive_score=3,
            items=[{
                "item_id": f"fixture-hotspot-{at.minute}",
                "title": "fixture",
                "source": "fixture exchange",
                "source_url": "https://example.test/hotspot",
                "source_kind": (
                    "social_media" if untrusted_hotspot else "exchange_announcement"
                ),
                "published_at": item_published,
                "official": True,
                "verified": True,
                "positive": True,
            }],
        ))
    records = {
        "constituents.jsonl": constituents,
        "daily.jsonl": daily,
        "intraday.jsonl": intraday,
        "sentiment.jsonl": sentiment,
        "hotspots.jsonl": hotspots,
    }
    kinds = {
        "constituents.jsonl": "historical_constituents",
        "daily.jsonl": "daily_bars",
        "intraday.jsonl": "intraday_15m",
        "sentiment.jsonl": "market_sentiment",
        "hotspots.jsonl": "hotspots",
    }
    source_kinds = {
        "constituents.jsonl": "csindex_official",
        "daily.jsonl": "eastmoney_market",
        "intraday.jsonl": "eastmoney_market",
        "sentiment.jsonl": "eastmoney_market",
        "hotspots.jsonl": "eastmoney_news",
    }
    source_urls = {
        "constituents.jsonl": "https://www.csindex.com.cn/constituents.jsonl",
        "daily.jsonl": "https://push2his.eastmoney.com/daily.jsonl",
        "intraday.jsonl": "https://push2his.eastmoney.com/intraday.jsonl",
        "sentiment.jsonl": "https://push2.eastmoney.com/sentiment.jsonl",
        "hotspots.jsonl": "https://finance.eastmoney.com/hotspots.jsonl",
    }
    payloads = {
        name: ("\n".join(json.dumps(item) for item in values) + "\n").encode()
        for name, values in records.items()
    }
    manifest = {
        "schema_version": "pa_oos_bundle_v1",
        "strategy_version": "hs300_topdown_4321_intraday_v1",
        "dataset": "out_of_sample",
        "period_start": "2026-01-01T00:00:00+08:00",
        "period_end": "2026-04-30T23:59:59+08:00",
        "artifacts": [{
            "path": name,
            "kind": kinds[name],
            "source_kind": source_kinds[name],
            "source_url": source_urls[name],
            "source_published_at": PUBLISHED,
            "sha256": hashlib.sha256(payload).hexdigest(),
        } for name, payload in payloads.items()],
    }
    path = tmp_path / "portfolio-oos.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return path


def test_oos_portfolio_backtest_runs_full_path_but_does_not_promote_small_sample(
    tmp_path,
) -> None:
    path = _build_bundle(tmp_path)
    engine = OosPortfolioBacktester(
        settings=OosBacktestSettings(pool_size=1),
        daily_strategy=_OneSignalStrategy(),
    )

    report = engine.run(path)
    repeated = engine.run(path)

    assert report.status == "complete"
    assert report.data_gaps == []
    assert report.daily_signal_count == 1
    assert report.score_frame_count == 3
    assert report.eligible_opportunity_count == 1
    assert len(report.trades) == 1
    assert report.trades[0].entered_at.startswith("2026-04-02T10:15")
    assert report.trades[0].exited_at.startswith("2026-04-03")
    assert report.trades[0].exit_reason == "protective_stop"
    assert report.performance_evidence["trade_count"] == 1
    assert report.performance_evidence["point_in_time_universe_verified"]
    assert report.performance_evidence["source_time_alignment_verified"]
    assert report.performance_evidence["execution_rules_verified"]
    assert report.performance_evidence["hotspot_sentiment_history_verified"]
    assert not report.promotion_eligible
    assert "trade_count_below_200:1" in report.gate_failures
    assert report.input_hash == repeated.input_hash
    assert report.trades == repeated.trades
    assert report.performance_evidence == repeated.performance_evidence


def test_oos_portfolio_backtest_fails_closed_on_future_hotspot(tmp_path) -> None:
    report = OosPortfolioBacktester(
        settings=OosBacktestSettings(pool_size=1),
        daily_strategy=_OneSignalStrategy(),
    ).run(_build_bundle(tmp_path, future_hotspot=True))

    assert report.status == "data_incomplete"
    assert not report.promotion_eligible
    assert any("hotspot_item_from_future" in gap for gap in report.data_gaps)
    assert report.trades == []


def test_oos_portfolio_backtest_fails_closed_on_untrusted_hotspot_source(
    tmp_path,
) -> None:
    report = OosPortfolioBacktester(
        settings=OosBacktestSettings(pool_size=1),
        daily_strategy=_OneSignalStrategy(),
    ).run(_build_bundle(tmp_path, untrusted_hotspot=True))

    assert report.status == "data_incomplete"
    assert not report.promotion_eligible
    assert any("hotspot_source_kind_untrusted" in gap for gap in report.data_gaps)
    assert not report.performance_evidence["hotspot_sentiment_history_verified"]


def test_oos_portfolio_backtest_rejects_unsupported_corporate_action_during_trade(
    tmp_path,
) -> None:
    report = OosPortfolioBacktester(
        settings=OosBacktestSettings(pool_size=1),
        daily_strategy=_OneSignalStrategy(),
    ).run(_build_bundle(tmp_path, corporate_action=True))

    assert report.status == "data_incomplete"
    assert not report.promotion_eligible
    assert any(
        "corporate_action_during_open_position_unsupported" in gap
        for gap in report.data_gaps
    )
    assert not report.performance_evidence["execution_rules_verified"]
