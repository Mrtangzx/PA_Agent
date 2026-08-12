"""Market classification and instrument-profile helpers."""
from __future__ import annotations

import re

from pa_agent.trading.models import AssetClass, InstrumentProfile

_A_SHARE = re.compile(r"^(?:SH|SZ|BJ)?\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_FUTURES = re.compile(r"^(?P<product>[A-Z]{1,3})(?P<delivery>\d{1,4})$", re.IGNORECASE)
_CONTINUOUS_DELIVERIES = {"0", "00", "000", "888", "8888", "999", "9999"}


def infer_asset_class(symbol: str, data_source: str = "") -> AssetClass:
    source = (data_source or "").strip().lower()
    text = (symbol or "").strip().upper().replace(" ", "")
    if _A_SHARE.fullmatch(text):
        return AssetClass.A_SHARE
    if _FUTURES.fullmatch(text):
        return AssetClass.CN_FUTURES
    if source in {"akshare", "eastmoney", "tushare", "baostock", "a_share"}:
        return AssetClass.A_SHARE
    if source == "eastmoney_futures":
        return AssetClass.CN_FUTURES
    return AssetClass.UNKNOWN


def is_continuous_futures_symbol(symbol: str) -> bool:
    match = _FUTURES.fullmatch((symbol or "").strip().upper())
    return bool(match and match.group("delivery") in _CONTINUOUS_DELIVERIES)


def futures_product_code(symbol: str) -> str:
    match = _FUTURES.fullmatch((symbol or "").strip().upper())
    return match.group("product").upper() if match else ""


def default_profile(symbol: str, data_source: str = "", adjustment_mode: str = "") -> InstrumentProfile:
    asset = infer_asset_class(symbol, data_source)
    if asset is AssetClass.A_SHARE:
        return InstrumentProfile(
            asset_class=asset,
            symbol=symbol,
            instrument_code=symbol,
            allow_short=False,
            board_lot=100,
            t_plus_one=True,
            adjustment_mode=adjustment_mode if adjustment_mode in {"qfq", "hfq", "none"} else "",
        )
    if asset is AssetClass.CN_FUTURES:
        return InstrumentProfile(
            asset_class=asset,
            symbol=symbol,
            instrument_code=symbol,
            product_code=futures_product_code(symbol),
            allow_short=True,
            t_plus_one=False,
        )
    return InstrumentProfile(asset_class=asset, symbol=symbol, instrument_code=symbol)
