from __future__ import annotations

import json
import zipfile
from datetime import datetime, time, timedelta, timezone

import pytest

from pa_agent.trading.oos_bundle import validate_oos_bundle
from pa_agent.trading.oos_export import (
    ALL_MARKET_SYMBOLS,
    INDEX_SYMBOLS,
    SCORING_SLOT_TIMES,
    OosObservationExporter,
)
from pa_agent.trading.oos_observations import OosObservationRecorder
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID
from pa_agent.trading.universe import (
    CLOUD_AI_AUTHORIZATION_SYMBOLS,
    CLOUD_AI_SYMBOLS,
)

TZ8 = timezone(timedelta(hours=8))
PUBLISHED = "2026-08-14T15:55:00+08:00"


def _add(store, kind, at, *, symbol="", payload=None, source_kind="eastmoney_market"):
    value = {
        "effective_at": at.isoformat(),
        "source_published_at": PUBLISHED,
        **(payload or {}),
    }
    if symbol:
        value["symbol"] = symbol
    if kind in {"daily_bars", "intraday_15m", "hotspots"}:
        value.setdefault(
            "observed_at", (at + timedelta(seconds=30)).isoformat()
        )
    store.add_oos_observation(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind=kind,
        symbol=symbol,
        effective_at=at.isoformat(),
        source_published_at=PUBLISHED,
        source_kind=source_kind,
        source_url="https://push2.eastmoney.com/fixture",
        payload=value,
    )


def _complete_store(tmp_path):
    store = TradeStore(tmp_path / "trades.db")
    OosObservationRecorder(store).record_strategy_definition()
    start = datetime(2026, 8, 15, 15, tzinfo=TZ8)
    sessions = [start + timedelta(days=index) for index in range(65)]
    for day in sessions:
        for symbol in ALL_MARKET_SYMBOLS:
            _add(store, "daily_bars", day, symbol=symbol, payload={
                "instrument_type": "index" if symbol in INDEX_SYMBOLS else "stock",
                "open": 10, "high": 11, "low": 9, "close": 10.5,
                "volume": 100, "amount": 1_000, "adjustment_factor": 1,
                "suspended": False, "limit_locked": False,
                "is_st": False, "delisting": False,
                "listed_days": 500, "industry": "fixture",
            })
    # The 65th daily close is not available to intraday decisions from that
    # same date.  OOS scoring starts on the following observed session.
    session = (sessions[-1] + timedelta(days=1)).date()
    for slot_time in SCORING_SLOT_TIMES:
        slot = datetime.combine(session, slot_time, tzinfo=TZ8)
        for symbol in ALL_MARKET_SYMBOLS:
            _add(store, "intraday_15m", slot, symbol=symbol, payload={
                "instrument_type": "index" if symbol in INDEX_SYMBOLS else "stock",
                "open": 10, "high": 11, "low": 9, "close": 10.5,
                "volume": 100, "amount": 1_000, "adjustment_factor": 1,
                "suspended": False, "limit_locked": False,
            })
        _add(store, "market_sentiment", slot, payload={
            "observed_at": (slot + timedelta(seconds=30)).isoformat(),
            "advancing_pct": 60,
            "hs300_above_ma20_pct": 65,
            "limit_up_count": 20,
            "limit_down_count": 2,
            "seal_success_pct": 80,
            "blast_board_pct": 10,
            "new_high_count": 100,
            "new_low_count": 20,
            "turnover_vs_ma20": 1.1,
            "broad_index_positive": True,
        })
        for symbol in CLOUD_AI_SYMBOLS:
            flows = [{
                "pct_chg": 1,
                "main_net_pct": 2,
                "advancing_pct": 70,
                "turnover_vs_recent": 1.2,
                "persistence_days": 3,
                "relative_strength_percentile": 80,
            }] if symbol in CLOUD_AI_AUTHORIZATION_SYMBOLS else []
            _add(store, "hotspots", slot, symbol=symbol, source_kind="eastmoney_news", payload={
                "captured_at": slot.isoformat(),
                "frozen_at": slot.isoformat(),
                "industries": ["fixture"],
                "concepts": [],
                "items": [],
                "board_strength": {"flows": flows},
                "positive_score": 0,
                "negative_blocks": [],
                "data_gaps": [],
                "rule_version": "hotspot_time_window_v2",
                "effective_windows_days": {"announcement": 30, "news": 3},
            })
    return store


def test_audit_blocks_export_and_explains_missing_production_kinds(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    OosObservationRecorder(store).record_strategy_definition()
    exporter = OosObservationExporter(store)

    audit = exporter.audit()

    assert audit.status == "data_incomplete"
    assert not audit.export_ready
    assert "observation_kind_missing:daily_bars" in audit.data_gaps
    assert "observation_kind_missing:intraday_15m" in audit.data_gaps
    assert "observation_kind_missing:market_sentiment" in audit.data_gaps
    with pytest.raises(ValueError, match="production_oos_export_blocked"):
        exporter.export(tmp_path / "blocked.zip")
    assert not (tmp_path / "blocked.zip").exists()


def test_export_excludes_only_invalid_pre_warmup_observations(tmp_path) -> None:
    store = _complete_store(tmp_path)
    setup_slot = datetime(2026, 8, 15, 9, 45, tzinfo=TZ8)
    for symbol in ALL_MARKET_SYMBOLS:
        store.add_oos_observation(
            strategy_version=TOPDOWN_STRATEGY_ID,
            kind="intraday_15m",
            symbol=symbol,
            effective_at=setup_slot.isoformat(),
            source_published_at=PUBLISHED,
            source_kind="eastmoney_market",
            source_url="https://push2.eastmoney.com/fixture",
            payload={
                "symbol": symbol,
                "effective_at": setup_slot.isoformat(),
                "instrument_type": "index" if symbol in INDEX_SYMBOLS else "stock",
                "open": 10, "high": 11, "low": 9, "close": 10.5,
                "volume": 100, "amount": 1_000, "adjustment_factor": 1,
                "suspended": False, "limit_locked": False,
            },
        )
    store.add_oos_observation(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="market_sentiment",
        symbol="",
        effective_at=setup_slot.isoformat(),
        source_published_at=PUBLISHED,
        source_kind="eastmoney_market",
        source_url="https://push2.eastmoney.com/fixture",
        payload={"effective_at": setup_slot.isoformat(), "advancing_pct": 60},
    )
    for symbol in CLOUD_AI_SYMBOLS:
        store.add_oos_observation(
            strategy_version=TOPDOWN_STRATEGY_ID,
            kind="hotspots",
            symbol=symbol,
            effective_at=setup_slot.isoformat(),
            source_published_at=PUBLISHED,
            source_kind="eastmoney_news",
            source_url="https://finance.eastmoney.com/fixture",
            payload={
                "symbol": symbol,
                "effective_at": setup_slot.isoformat(),
                "frozen_at": setup_slot.isoformat(),
                "items": [],
            },
        )

    exporter = OosObservationExporter(store)
    audit = exporter.audit()
    assert audit.export_ready
    raw_count = audit.record_counts["intraday_15m"]
    assert audit.non_scoreable_setup_records == {
        "intraday_15m": len(ALL_MARKET_SYMBOLS),
        "market_sentiment": 1,
        "hotspots": len(CLOUD_AI_SYMBOLS),
    }

    destination = exporter.export(tmp_path / "production.zip")

    assert len(store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="intraday_15m",
        limit=1_000_000,
    )) == raw_count
    with zipfile.ZipFile(destination) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        records = [
            json.loads(line)
            for line in archive.read("intraday.jsonl").decode("utf-8").splitlines()
            if line
        ]
    assert manifest["export_policy"] == "scoreable_observations_v1"
    assert manifest["excluded_pre_warmup_records"] == {
        "intraday_15m": len(ALL_MARKET_SYMBOLS),
        "market_sentiment": 1,
        "hotspots": len(CLOUD_AI_SYMBOLS),
    }
    assert len(records) == raw_count - len(ALL_MARKET_SYMBOLS)
    assert all(record.get("observed_at") for record in records)
    assert validate_oos_bundle(destination).status == "complete"


def test_partial_live_day_counts_complete_observed_intraday_slots(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    OosObservationRecorder(store).record_strategy_definition()
    session = datetime(2026, 8, 15, tzinfo=TZ8)
    for slot_time in (time(9, 45), time(10, 0)):
        slot = datetime.combine(session.date(), slot_time, tzinfo=TZ8)
        for symbol in ALL_MARKET_SYMBOLS:
            _add(store, "intraday_15m", slot, symbol=symbol, payload={
                "instrument_type": "index" if symbol in INDEX_SYMBOLS else "stock",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1_000,
                "adjustment_factor": 1,
            })

    audit = OosObservationExporter(store).audit()

    assert audit.complete_intraday_slots == 2
    assert "intraday_unexpected_slot_count:2" not in audit.data_gaps
    assert "complete_intraday_slots_missing" not in audit.data_gaps
    assert not audit.export_ready


def test_65th_daily_close_requires_a_later_intraday_session(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    OosObservationRecorder(store).record_strategy_definition()
    start = datetime(2026, 8, 15, 15, tzinfo=TZ8)
    sessions = [start + timedelta(days=index) for index in range(65)]
    for day in sessions:
        for symbol in ALL_MARKET_SYMBOLS:
            _add(store, "daily_bars", day, symbol=symbol, payload={
                "instrument_type": "index" if symbol in INDEX_SYMBOLS else "stock",
                "open": 10, "high": 11, "low": 9, "close": 10.5,
                "volume": 100, "amount": 1_000, "adjustment_factor": 1,
                "suspended": False, "limit_locked": False,
                "is_st": False, "delisting": False,
                "listed_days": 500, "industry": "fixture",
            })
    same_day_slot = datetime.combine(
        sessions[-1].date(), time(15, 0), tzinfo=TZ8
    )
    for symbol in ALL_MARKET_SYMBOLS:
        _add(store, "intraday_15m", same_day_slot, symbol=symbol, payload={
            "instrument_type": "index" if symbol in INDEX_SYMBOLS else "stock",
            "open": 10, "high": 11, "low": 9, "close": 10.5,
            "volume": 100, "amount": 1_000, "adjustment_factor": 1,
            "suspended": False, "limit_locked": False,
        })

    audit = OosObservationExporter(store).audit()

    assert "post_warmup_intraday_slots_missing" in audit.data_gaps
    assert not audit.export_ready


def test_pre_warmup_intraday_setup_gap_does_not_poison_export(tmp_path) -> None:
    store = _complete_store(tmp_path)
    setup_slot = datetime(2026, 8, 15, 9, 45, tzinfo=TZ8)
    for symbol in ALL_MARKET_SYMBOLS:
        _add(store, "intraday_15m", setup_slot, symbol=symbol, payload={
            "instrument_type": "index" if symbol in INDEX_SYMBOLS else "stock",
            "open": 10, "high": 11, "low": 9, "close": 10.5,
            "volume": 100, "amount": 1_000, "adjustment_factor": 1,
            "suspended": False, "limit_locked": False,
        })

    audit = OosObservationExporter(store).audit()

    assert audit.export_ready
    assert not any(
        gap.startswith("sentiment_slot_missing_or_ambiguous")
        for gap in audit.data_gaps
    )


def test_complete_ledger_exports_v2_bundle_with_derived_theme_metrics(tmp_path) -> None:
    store = _complete_store(tmp_path)
    exporter = OosObservationExporter(store)

    audit = exporter.audit()
    path = exporter.export(tmp_path / "production-oos.zip")

    assert audit.export_ready
    assert audit.session_count == 65
    assert audit.complete_intraday_slots == 16
    assert validate_oos_bundle(path).status == "complete"
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        first_hotspot = json.loads(
            archive.read("hotspots.jsonl").decode("utf-8").splitlines()[0]
        )
    assert manifest["schema_version"] == "pa_oos_bundle_v2"
    assert first_hotspot["theme_metrics"]["persistence_days"] == 3
    assert first_hotspot["relative_strength_percentile"] == 80.0
