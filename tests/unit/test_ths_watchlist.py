from __future__ import annotations

from datetime import date

import pytest

from pa_agent.trading.daily_candidates import DailyCandidateScanResult
from pa_agent.trading.quant import SignalDecision
from pa_agent.trading.store import TradeStore
from pa_agent.trading.ths_watchlist import (
    THS_WATCHLIST_SOURCE,
    ThsWatchlistFileReader,
    ThsWatchlistScanService,
)
from pa_agent.trading.topdown import MANUAL_EXCEPTION_STRATEGY_ID
from pa_agent.trading.universe import CurrentUniverseMember, UniverseSnapshot


def _write_watchlist(root, blocks: list[tuple[str, list[tuple[str, str]]]]) -> None:
    source = root / "bin" / "users" / "current" / "blockstockV3.xml"
    source.parent.mkdir(parents=True)
    parts = ["<Root>"]
    for name, securities in blocks:
        parts.append(f'<Block name="{name}">')
        parts.extend(
            f'<security market="{market}" code="{symbol}" />'
            for market, symbol in securities
        )
        parts.append("</Block>")
    parts.append("</Root>")
    source.write_text("".join(parts), encoding="utf-8")


def _seed_universe(store: TradeStore) -> None:
    store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 14),
            version="cloud-ai-pool-2026-08",
            symbols=["600519"],
            members=[
                CurrentUniverseMember(
                    rank=1,
                    symbol="600519",
                    name="贵州茅台",
                    industry="白酒",
                    latest_price=1418.2,
                    average_amount_20=4_700_000_000,
                )
            ],
            source_kind="test_fixed_pool",
            source_as_of=date(2026, 8, 14),
            input_member_count=1,
        ),
        source_updated_at="2026-08-14T15:00:00+08:00",
    )


def _decision(
    symbol: str,
    *,
    strategy_id: str,
    pool_version: str,
    name: str,
    industry: str,
) -> SignalDecision:
    return SignalDecision(
        status="allow",
        strategy_id=strategy_id,
        parameter_version="1.0.0",
        pool_version=pool_version,
        symbol=symbol,
        signal_time="2026-08-14T15:00:00+08:00",
        condition_snapshot={
            "expected_security_name": name,
            "industry": industry,
            "base_pool_version": "cloud-ai-pool-2026-08",
            "pullback_atr": 1.25,
            "volume_ratio": 1.08,
            "market_breadth_pct": 61.0,
        },
        trigger_price=10.01,
        max_entry_price=10.35,
        initial_stop=9.55,
        valid_until="2026-08-17T15:00:00+08:00",
    )


class _CompleteScanner:
    def __init__(self) -> None:
        self.scan_calls = 0
        self.manual_calls: list[str] = []
        self.strategy = type(
            "Strategy",
            (),
            {"settings": type("Settings", (), {"strategy_id": "cloud_ai_daily_pullback_v1"})()},
        )()

    def scan(self, pool, *, progress=None):
        self.scan_calls += 1
        return DailyCandidateScanResult(
            pool_version=pool["version"],
            signal_date=date(2026, 8, 14),
            market_breadth_pct=61.0,
            decisions=[
                _decision(
                    "600519",
                    strategy_id="cloud_ai_daily_pullback_v1",
                    pool_version=pool["version"],
                    name="贵州茅台",
                    industry="白酒",
                )
            ],
        )

    def evaluate_manual_exception(
        self, symbol, *, base_pool_version, market_breadth_pct
    ):
        self.manual_calls.append(symbol)
        return _decision(
            symbol,
            strategy_id=MANUAL_EXCEPTION_STRATEGY_ID,
            pool_version=f"manual-{base_pool_version}-{symbol}",
            name="平安银行",
            industry="银行",
        )


class _IncompleteScanner:
    def scan(self, pool, *, progress=None):
        return DailyCandidateScanResult(
            pool_version=pool["version"],
            data_complete=False,
            data_gaps=["eastmoney_daily_unavailable"],
        )


def test_reader_preserves_all_categories_deduplicates_and_excludes_non_a_shares(
    tmp_path,
) -> None:
    _write_watchlist(
        tmp_path,
        [
            ("趋势", [("USHA", "600519"), ("URFI", "881001")]),
            ("主力资金", [("USHA", "600519"), ("USZA", "000001")]),
            ("3个交易日资金净流入", []),
        ],
    )

    snapshot = ThsWatchlistFileReader().read(tmp_path)

    assert snapshot.categories == ["趋势", "主力资金", "3个交易日资金净流入"]
    assert [item.symbol for item in snapshot.members] == ["000001", "600519"]
    by_symbol = {item.symbol: item for item in snapshot.members}
    assert by_symbol["600519"].categories == ["趋势", "主力资金"]
    assert snapshot.rejected == [
        {"category": "趋势", "symbol": "881001", "reason": "非沪深A股，已排除"}
    ]


def test_reader_refuses_to_guess_when_multiple_user_files_exist(tmp_path) -> None:
    _write_watchlist(tmp_path, [("趋势", [("USHA", "600519")])])
    second = tmp_path / "bin" / "users" / "second" / "blockstockV3.xml"
    second.parent.mkdir(parents=True)
    second.write_text("<Root />", encoding="utf-8")

    with pytest.raises(RuntimeError, match="多个同花顺用户自选文件"):
        ThsWatchlistFileReader().read(tmp_path)


def test_source_sync_never_deletes_personal_origin_or_verified_name(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    store.upsert_watchlist_member(
        symbol="600519",
        name="贵州茅台",
        source="user_watchlist",
        metadata={"industry": "白酒"},
    )

    store.sync_watchlist_source(
        THS_WATCHLIST_SOURCE,
        [{"symbol": "600519", "name": "600519", "metadata": {"ths_categories": ["趋势"]}}],
    )
    assert store.get_watchlist_member("600519")["name"] == "贵州茅台"

    store.sync_watchlist_source(THS_WATCHLIST_SOURCE, [])

    active = store.list_watchlist_members()
    assert [item["symbol"] for item in active] == ["600519"]
    sources = {item["source"]: item for item in active[0]["sources"]}
    assert sources["user_watchlist"]["active"] is True
    assert sources[THS_WATCHLIST_SOURCE]["active"] is False


def test_full_scan_routes_pool_external_stock_and_reuses_idempotent_cache(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed_universe(store)
    _write_watchlist(
        tmp_path / "ths",
        [
            ("白酒", [("USHA", "600519")]),
            ("银行", [("USZA", "000001")]),
        ],
    )
    scanner = _CompleteScanner()
    service = ThsWatchlistScanService(
        store,
        scanner,
        install_root=tmp_path / "ths",
        max_workers=2,
    )

    first = service.scan()
    second = service.scan()

    assert first.total == 2
    assert first.next_session_candidates == 2
    assert first.actionable == 0
    assert scanner.scan_calls == 1
    assert scanner.manual_calls == ["000001"]
    assert second.scan_id == first.scan_id
    assert len(store.list_quant_signals(limit=20)) == 2
    results = {item["symbol"]: item for item in first.results}
    assert results["600519"]["in_system_pool"] is True
    assert results["600519"]["name"] == "贵州茅台"
    assert results["000001"]["strategy_id"] == MANUAL_EXCEPTION_STRATEGY_ID
    assert results["000001"]["manual_exception_eligible"] is True
    assert "下个交易日" in results["000001"]["reason_text"]
    assert "半风险通道" in results["000001"]["reason_text"]
    row = store.list_watchlist_members(source=THS_WATCHLIST_SOURCE)[0]
    source = next(item for item in row["sources"] if item["source"] == THS_WATCHLIST_SOURCE)
    assert source["metadata"]["ths_reason"]
    assert source["metadata"]["ths_scan_id"] == first.scan_id


def test_incomplete_scan_fails_closed_and_is_visible_on_watchlist_source(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed_universe(store)
    _write_watchlist(
        tmp_path / "ths",
        [("趋势", [("USZA", "000001")])],
    )
    service = ThsWatchlistScanService(
        store,
        _IncompleteScanner(),
        install_root=tmp_path / "ths",
    )

    report = service.scan()

    assert report.data_complete is False
    assert report.results[0]["actionable_stage"] == "data_incomplete"
    row = store.list_watchlist_members(source=THS_WATCHLIST_SOURCE)[0]
    source = next(item for item in row["sources"] if item["source"] == THS_WATCHLIST_SOURCE)
    assert source["metadata"]["authorization_eligible"] is False
    assert "数据不完整" in source["metadata"]["ths_reason"]
