from datetime import date, datetime

from pa_agent.trading.universe import (
    CLOUD_AI_SYMBOLS,
    CurrentHs300UniverseService,
    FixedCloudAiUniverseService,
    Hs300HistoricalUniverse,
    OfficialConstituent,
    OfficialConstituentFile,
    UniverseMember,
)


def _member(symbol: str, amount: float, **updates) -> UniverseMember:
    values = {
        "symbol": symbol,
        "name": symbol,
        "effective_from": date(2025, 1, 1),
        "listing_date": date(2020, 1, 1),
        "average_amount_20": amount,
    }
    values.update(updates)
    return UniverseMember(**values)


def test_point_in_time_universe_uses_historical_effective_dates_and_liquidity() -> None:
    module = Hs300HistoricalUniverse([
        _member("600001", 10),
        _member("600002", 30),
        _member("600003", 20, effective_from=date(2027, 1, 1)),
        _member("600004", 40, is_st=True),
    ], pool_size=2)
    result = module.snapshot(date(2026, 8, 1))
    assert result.symbols == ["600002", "600001"]
    assert result.rejected["600004"] == ["st"]
    assert "600003" not in result.symbols


def test_current_universe_uses_official_members_and_ranks_by_20_day_amount() -> None:
    official = OfficialConstituentFile(
        source_as_of=date(2026, 8, 12),
        source_url="https://official.example/000300cons.xls",
        source_hash="a" * 64,
        constituents=[
            OfficialConstituent(symbol=f"600{index:03d}", name=f"股票{index}")
            for index in range(35)
        ],
    )

    def daily(symbol: str, **_kwargs):
        rank = int(symbol[-3:])
        return [
            {
                "time": datetime(2026, 7, 1 + offset),
                "open": 10.0,
                "close": 10.0,
                "high": 10.2,
                "low": 9.8,
                "volume": 1000,
                "amount": float((rank + 1) * 1_000_000),
            }
            for offset in range(20)
        ]

    service = CurrentHs300UniverseService(
        pool_size=30,
        official_loader=lambda: official,
        daily_loader=daily,
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "listing_date": "20000101",
        },
        expected_constituent_count=None,
    )
    snapshot = service.generate(as_of=date(2026, 8, 13))

    assert len(snapshot.symbols) == 30
    assert snapshot.symbols[0] == "600034"
    assert snapshot.symbols[-1] == "600005"
    assert snapshot.members[0].rank == 1
    assert snapshot.members[0].average_amount_20 == 35_000_000
    assert snapshot.data_complete
    assert snapshot.source_kind == "official_current_constituents"


def test_current_universe_fails_closed_when_one_member_data_is_missing() -> None:
    official = OfficialConstituentFile(
        source_as_of=date(2026, 8, 12),
        source_url="official",
        source_hash="b" * 64,
        constituents=[
            OfficialConstituent(symbol=f"000{index:03d}", name=f"股票{index}")
            for index in range(31)
        ],
    )

    def daily(symbol: str, **_kwargs):
        if symbol == "000030":
            return []
        return [
            {
                "time": datetime(2026, 7, 1 + offset),
                "open": 10.0,
                "close": 10.0,
                "high": 10.1,
                "low": 9.9,
                "volume": 1000,
                "amount": 1_000_000,
            }
            for offset in range(20)
        ]

    service = CurrentHs300UniverseService(
        pool_size=30,
        official_loader=lambda: official,
        daily_loader=daily,
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "listing_date": "20000101",
        },
        expected_constituent_count=None,
    )
    snapshot = service.generate(as_of=date(2026, 8, 13))

    assert not snapshot.data_complete
    assert snapshot.rejected["000030"] == ["insufficient_20_day_amount_data"]
    assert "member_data_incomplete" in snapshot.completeness_reasons


def test_fixed_cloud_ai_universe_is_the_exact_user_selected_pool() -> None:
    def daily(symbol: str, **_kwargs):
        return [
            {
                "time": datetime(2026, 7, 1 + offset),
                "open": 10.0,
                "close": 10.0,
                "high": 10.2,
                "low": 9.8,
                "volume": 1000,
                "amount": 1_000_000,
            }
            for offset in range(20)
        ]

    service = FixedCloudAiUniverseService(
        daily_loader=daily,
        profile_loader=lambda symbol: {"symbol": symbol, "listing_date": "20000101"},
    )
    snapshot = service.generate(as_of=date(2026, 8, 13))

    assert snapshot.version == "cloud_ai_11_v1-2026-08"
    assert snapshot.symbols == list(CLOUD_AI_SYMBOLS)
    assert len(snapshot.symbols) == 11
    assert {item.symbol for item in snapshot.members} == set(CLOUD_AI_SYMBOLS)
    assert all(item.industry == "云算力主题" for item in snapshot.members)
    parallel = next(item for item in snapshot.members if item.symbol == "839494")
    assert parallel.board == "北交所"
    assert not parallel.authorization_eligible
    assert "beijing_exchange_analysis_only" in parallel.eligibility_reasons


def test_fixed_pool_keeps_missing_member_visible_but_analysis_only() -> None:
    service = FixedCloudAiUniverseService(
        daily_loader=lambda symbol, **_: [] if symbol == "300017" else [
            {
                "time": datetime(2026, 7, 1 + offset),
                "open": 10.0, "close": 10.0, "high": 10.2, "low": 9.8,
                "volume": 1000, "amount": 1_000_000,
            }
            for offset in range(20)
        ],
        profile_loader=lambda symbol: {"symbol": symbol, "listing_date": "20000101"},
    )

    snapshot = service.generate(as_of=date(2026, 8, 13))

    assert snapshot.symbols == list(CLOUD_AI_SYMBOLS)
    member = next(item for item in snapshot.members if item.symbol == "300017")
    assert not member.authorization_eligible
    assert "insufficient_20_day_amount_data" in member.eligibility_reasons
