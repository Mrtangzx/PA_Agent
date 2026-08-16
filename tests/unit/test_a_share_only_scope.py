from __future__ import annotations

import pytest
from pydantic import ValidationError

from pa_agent.config.settings import Settings, load_settings, save_settings
from pa_agent.data.ashare_common import is_a_share_stock_symbol
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.trading.store import TradeStore
from pa_agent.trading.universe import UniverseSnapshot


@pytest.mark.parametrize(
    "symbol",
    ["600519", "000001", "300750", "688158", "839494", "SH600519", "600519.SH"],
)
def test_a_share_stock_symbols_are_accepted(symbol: str) -> None:
    assert is_a_share_stock_symbol(symbol)


@pytest.mark.parametrize(
    "symbol",
    [
        "XAUUSD",
        "EURUSD",
        "BTCUSDT",
        "00700",
        "AAPL",
        "000300",
        "399006",
        "sh000001",
        "000001.SH",
        "510300",
        "200002",
        "400001",
        "800001",
    ],
)
def test_non_a_share_or_non_stock_symbols_are_rejected(symbol: str) -> None:
    assert not is_a_share_stock_symbol(symbol)


def test_settings_migrate_legacy_market_to_a_share_only(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.general.last_data_source = "mt5"
    settings.general.last_symbol = "XAUUSDm"

    save_settings(settings, path)
    loaded = load_settings(path)

    assert loaded.general.investment_scope == "a_share_only"
    assert loaded.general.last_data_source == "eastmoney"
    assert loaded.general.last_symbol == "600519"


def test_eastmoney_production_subscription_rejects_index_and_other_markets() -> None:
    source = EastMoneySource()
    source.connect()
    source.subscribe("600519", "15m")
    assert source._symbol == "600519"

    with pytest.raises(ValueError):
        source.subscribe("000300", "15m")
    with pytest.raises(ValueError):
        source.subscribe("sh000001", "15m")
    with pytest.raises(ValueError):
        source.subscribe("EURUSD", "15m")


@pytest.mark.parametrize("symbol", ["000300", "510300", "00700", "AAPL", "XAUUSD"])
def test_universe_model_rejects_non_a_share_instruments(symbol: str) -> None:
    with pytest.raises(ValidationError, match="交易股票池仅允许A股股票"):
        UniverseSnapshot(
            as_of="2026-08-14",
            version="invalid-universe",
            symbols=["600519", symbol],
        )


def test_store_rejects_non_a_share_universe_dict_before_persisting(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")

    with pytest.raises(ValueError, match="拒绝保存非A股或非股票标的"):
        store.upsert_universe_snapshot({
            "as_of": "2026-08-14",
            "version": "invalid-universe",
            "symbols": ["600519", "510300"],
        })

    assert store.list_universe_snapshots() == []
