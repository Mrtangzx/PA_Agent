from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pa_agent.trading.daily_candidates import DailyCandidateScanner
from pa_agent.trading.quant import Hs300DailyPullbackStrategy
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import MANUAL_EXCEPTION_STRATEGY_ID

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 13, 16, 0, tzinfo=TZ)


def _rows(symbol: str, count: int = 80) -> list[dict]:
    offset = 1.0 if symbol == "600000" else -0.2
    result = []
    for index in range(count):
        close = 100 + index * offset
        result.append({
            "time": (NOW - timedelta(days=count - 1 - index)).replace(tzinfo=None),
            "open": close - 0.2,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
            "volume": 1_000_000,
            "amount": close * 1_000_000,
            "pct_chg": 0.5,
        })
    return result


def test_daily_scanner_freezes_pool_breadth_and_is_store_idempotent(tmp_path) -> None:
    scanner = DailyCandidateScanner(
        Hs300DailyPullbackStrategy(),
        stock_daily_loader=lambda symbol, **_: _rows(symbol),
        index_daily_loader=lambda symbol, **_: _rows(symbol),
    )
    result = scanner.scan({
        "version": "hs300-2026-08",
        "symbols": ["600000", "000001"],
        "data_complete": True,
    }, captured_at=NOW)

    assert result.data_complete
    assert result.signal_date == NOW.date()
    assert result.market_breadth_pct == 50.0
    assert [item.symbol for item in result.decisions] == ["600000", "000001"]
    assert all(item.pool_version == "hs300-2026-08" for item in result.decisions)

    store = TradeStore(tmp_path / "trades.db")
    first = store.add_quant_signal(result.decisions[0])
    second = store.add_quant_signal(result.decisions[0])
    assert first == second
    assert len(store.list_quant_signals()) == 1


def test_daily_scanner_fails_closed_when_one_pool_member_is_missing() -> None:
    def load(symbol: str, **_) -> list[dict]:
        return _rows(symbol) if symbol == "600000" else []

    scanner = DailyCandidateScanner(
        Hs300DailyPullbackStrategy(),
        stock_daily_loader=load,
        index_daily_loader=lambda symbol, **_: _rows(symbol),
    )
    result = scanner.scan({
        "version": "hs300-2026-08",
        "symbols": ["600000", "000001"],
        "data_complete": True,
    }, captured_at=NOW)

    assert not result.data_complete
    assert not result.decisions
    assert "stock_000001_requires_65_closed_daily_bars" in result.data_gaps


def test_analysis_only_member_does_not_block_the_tradable_pool() -> None:
    loaded: list[str] = []

    def load(symbol: str, **_) -> list[dict]:
        loaded.append(symbol)
        if symbol == "839494":
            raise AssertionError("analysis-only member must not enter candidate loader")
        return _rows(symbol)

    scanner = DailyCandidateScanner(
        Hs300DailyPullbackStrategy(),
        stock_daily_loader=load,
        index_daily_loader=lambda symbol, **_: _rows(symbol),
    )
    result = scanner.scan({
        "version": "cloud_ai_11_v1-2026-08",
        "symbols": ["300017", "839494"],
        "members": [
            {"symbol": "300017", "authorization_eligible": True},
            {
                "symbol": "839494",
                "authorization_eligible": False,
                "eligibility_reasons": ["beijing_exchange_analysis_only"],
            },
        ],
        "data_complete": True,
    }, captured_at=NOW)

    assert result.data_complete
    assert loaded == ["300017"]
    assert [item.symbol for item in result.decisions] == ["300017"]


def test_manual_exception_reuses_daily_strategy_without_mutating_base_pool() -> None:
    scanner = DailyCandidateScanner(
        Hs300DailyPullbackStrategy(),
        stock_daily_loader=lambda symbol, **_: _rows(symbol),
        index_daily_loader=lambda symbol, **_: _rows(symbol),
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "name": "贵州茅台",
            "industry": "白酒",
            "listing_date": "20010827",
        },
    )

    decision = scanner.evaluate_manual_exception(
        "600519",
        base_pool_version="cloud_ai_11_v1-2026-08",
        market_breadth_pct=60.0,
        captured_at=NOW,
    )

    assert decision.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID
    assert decision.pool_version.startswith(
        "manual-exception-cloud_ai_11_v1-2026-08-"
    )
    assert decision.condition_snapshot["manual_exception"] is True
    assert decision.condition_snapshot["base_pool_version"] == "cloud_ai_11_v1-2026-08"
    assert decision.condition_snapshot["expected_security_name"] == "贵州茅台"
    assert decision.condition_snapshot["industry"] == "白酒"
    assert decision.condition_snapshot["risk_multiplier"] == 0.5


def test_manual_exception_rejects_non_stock_before_data_fetch() -> None:
    calls: list[str] = []
    scanner = DailyCandidateScanner(
        Hs300DailyPullbackStrategy(),
        stock_daily_loader=lambda symbol, **_: calls.append(symbol) or _rows(symbol),
        index_daily_loader=lambda symbol, **_: _rows(symbol),
        profile_loader=lambda symbol: calls.append(symbol) or {},
    )

    decision = scanner.evaluate_manual_exception(
        "510300",
        base_pool_version="cloud_ai_11_v1-2026-08",
        market_breadth_pct=60.0,
        captured_at=NOW,
    )

    assert decision.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID
    assert decision.status.value == "reject"
    assert decision.reasons == ["a_share_stock_symbol_required"]
    assert calls == []


def test_manual_exception_refreshes_base_pool_breadth_without_mutating_pool() -> None:
    pool = {
        "version": "cloud_ai_11_v1-2026-08",
        "symbols": ["600000", "000001"],
        "data_complete": True,
    }
    original = {**pool, "symbols": list(pool["symbols"])}
    scanner = DailyCandidateScanner(
        Hs300DailyPullbackStrategy(),
        stock_daily_loader=lambda symbol, **_: _rows(symbol),
        index_daily_loader=lambda symbol, **_: _rows(symbol),
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "name": "贵州茅台",
            "industry": "白酒",
            "listing_date": "20010827",
        },
    )

    evaluation = scanner.evaluate_manual_exception_from_pool(
        "600519",
        pool_snapshot=pool,
        captured_at=NOW,
    )

    assert pool == original
    assert evaluation.base_scan.data_complete
    assert evaluation.base_scan.market_breadth_pct == 50.0
    assert evaluation.decision.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID
    assert evaluation.decision.condition_snapshot["base_pool_version"] == pool["version"]


def test_manual_exception_fails_closed_when_base_pool_breadth_is_incomplete() -> None:
    scanner = DailyCandidateScanner(
        Hs300DailyPullbackStrategy(),
        stock_daily_loader=lambda symbol, **_: (
            [] if symbol == "000001" else _rows(symbol)
        ),
        index_daily_loader=lambda symbol, **_: _rows(symbol),
        profile_loader=lambda _symbol: (_ for _ in ()).throw(
            AssertionError("manual stock must not load without trusted base breadth")
        ),
    )

    evaluation = scanner.evaluate_manual_exception_from_pool(
        "600519",
        pool_snapshot={
            "version": "cloud_ai_11_v1-2026-08",
            "symbols": ["600000", "000001"],
            "data_complete": True,
        },
        captured_at=NOW,
    )

    assert not evaluation.base_scan.data_complete
    assert evaluation.decision.status.value == "reject"
    assert "base_pool_breadth_incomplete" in evaluation.decision.reasons
    assert "stock_000001_requires_65_closed_daily_bars" in evaluation.decision.reasons
