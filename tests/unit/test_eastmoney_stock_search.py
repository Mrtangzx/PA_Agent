from __future__ import annotations

import pytest

from pa_agent.data import eastmoney_client
from pa_agent.data.eastmoney_extended import _em_code, _secucode


@pytest.mark.parametrize(
    ("symbol", "market", "exchange"),
    [
        ("600519", 1, "SSE"),
        ("688158", 1, "SSE"),
        ("300750", 0, "SZSE"),
        ("839494", 0, "BSE"),
        ("920002", 0, "BSE"),
    ],
)
def test_a_share_market_route_handles_beijing_new_codes(
    symbol: str,
    market: int,
    exchange: str,
) -> None:
    assert eastmoney_client.stock_market_code(symbol) == market
    assert eastmoney_client.stock_secid(symbol) == f"{market}.{symbol}"
    assert eastmoney_client._a_share_exchange(symbol) == exchange


@pytest.mark.parametrize(
    ("symbol", "em_code", "secucode"),
    [
        ("600519", "SH600519", "600519.SH"),
        ("300750", "SZ300750", "300750.SZ"),
        ("839494", "BJ839494", "839494.BJ"),
        ("920002", "BJ920002", "920002.BJ"),
    ],
)
def test_extended_data_routes_beijing_as_beijing(
    symbol: str,
    em_code: str,
    secucode: str,
) -> None:
    assert _em_code(symbol) == em_code
    assert _secucode(symbol) == secucode


def test_search_a_share_stocks_filters_same_name_hk_and_non_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        eastmoney_client,
        "_fetch_stock_search_payload",
        lambda query, *, count: {
            "QuotationCodeTable": {
                "Data": [
                    {
                        "Code": "600941",
                        "Name": "中国移动",
                        "Classify": "AStock",
                    },
                    {
                        "Code": "00941",
                        "Name": "中国移动",
                        "Classify": "HK",
                    },
                    {
                        "Code": "510300",
                        "Name": "沪深300ETF",
                        "Classify": "Fund",
                    },
                ]
            }
        },
    )

    assert eastmoney_client.search_a_share_stocks("中国移动") == [{
        "symbol": "600941",
        "name": "中国移动",
        "exchange": "SSE",
    }]
    assert eastmoney_client.resolve_a_share_stock_name("中国移动") == (
        "SSE",
        "600941",
    )


def test_preferred_pool_name_resolves_without_external_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(query: str, *, count: int = 20) -> list[dict[str, str]]:
        raise AssertionError("current-pool exact names must resolve locally")

    monkeypatch.setattr(eastmoney_client, "search_a_share_stocks", fail_if_called)

    assert eastmoney_client.resolve_a_share_stock_name(
        "中国移动",
        preferred_members=[{"symbol": "600941", "name": "中国移动"}],
    ) == ("SSE", "600941")


def test_ambiguous_a_share_name_requires_six_digit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        eastmoney_client,
        "search_a_share_stocks",
        lambda query: [
            {"symbol": "600001", "name": "示例科技", "exchange": "SSE"},
            {"symbol": "300001", "name": "示例股份", "exchange": "SZSE"},
        ],
    )

    with pytest.raises(ValueError, match="存在多个候选"):
        eastmoney_client.resolve_a_share_stock_name("示例")


def test_unknown_name_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        eastmoney_client,
        "search_a_share_stocks",
        lambda query: [],
    )

    with pytest.raises(ValueError, match="未找到A股股票名称"):
        eastmoney_client.resolve_a_share_stock_name("不存在的公司")
