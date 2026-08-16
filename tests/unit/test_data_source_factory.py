"""Tests for data source factory and settings."""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    PRODUCTION_DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    normalize_data_source_kind,
)
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.data.eastmoney_futures_source import EastMoneyFuturesSource
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.tushare_source import TushareSource
from pa_agent.data.tradingview import TradingViewSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "eastmoney"
    assert normalize_data_source_kind(None) == "eastmoney"


def test_normalize_data_source_kind_hidden_sources():
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("eastmoney") == "eastmoney"
    assert normalize_data_source_kind("tushare") == "tushare"
    assert normalize_data_source_kind("yfinance") == "yfinance"


def test_eastmoney_sources_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "eastmoney" in ui_kinds
    assert "akshare" not in ui_kinds
    assert "eastmoney_futures" in ui_kinds


def test_production_ui_only_exposes_a_share_source():
    assert PRODUCTION_DATA_SOURCE_CHOICES == tuple(
        item for item in DATA_SOURCE_CHOICES if item[0] == "eastmoney"
    )


def test_tushare_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tushare" not in ui_kinds


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("mt5"), MT5Source)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("eastmoney"), EastMoneySource)
    assert isinstance(create_data_source("eastmoney_futures"), EastMoneyFuturesSource)
    assert isinstance(create_data_source("tushare"), TushareSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("mt5") == "XAUUSDm"
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("eastmoney") == "600519"
    assert default_symbol_for_kind("eastmoney_futures") == "AU0 黄金"
    assert default_symbol_for_kind("tushare") == "600519"


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "eastmoney"
    assert g.last_symbol == "600519"
