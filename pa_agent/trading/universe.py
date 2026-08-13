"""Point-in-time HS300 history and the user-defined current trading universe.

Historical validation accepts caller-supplied dated membership only.  The
current-month service downloads the official CSI constituent workbook and may
not be used to backfill historical membership.
"""
from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

OFFICIAL_HS300_CURRENT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "file/autofile/cons/000300cons.xls"
)


class OfficialConstituent(BaseModel):
    symbol: str
    name: str
    exchange: str = ""


class OfficialConstituentFile(BaseModel):
    source_as_of: date
    source_url: str
    source_hash: str
    constituents: list[OfficialConstituent]


class CurrentUniverseMember(BaseModel):
    rank: int = Field(ge=1)
    symbol: str
    name: str
    exchange: str = ""
    industry: str = ""
    theme: str = ""
    tier: str = ""
    board: str = ""
    average_amount_20: float = Field(ge=0)
    latest_price: float | None = None
    latest_pct_chg: float | None = None
    listing_date: date | None = None
    data_updated_at: str = ""
    authorization_eligible: bool = True
    eligibility_reasons: list[str] = Field(default_factory=list)


class UniverseMember(BaseModel):
    symbol: str
    name: str
    effective_from: date
    effective_to: date | None = None
    listing_date: date
    average_amount_20: float = Field(ge=0)
    is_st: bool = False
    delisting整理: bool = False
    suspended: bool = False
    data_complete: bool = True
    price_limit_untradeable: bool = False


class UniverseSnapshot(BaseModel):
    as_of: date
    version: str
    symbols: list[str]
    rejected: dict[str, list[str]] = Field(default_factory=dict)
    members: list[CurrentUniverseMember] = Field(default_factory=list)
    source_kind: str = "historical_point_in_time"
    source_url: str = ""
    source_hash: str = ""
    source_as_of: date | None = None
    input_member_count: int = 0
    data_complete: bool = True
    completeness_reasons: list[str] = Field(default_factory=list)


class FixedThemeConstituent(BaseModel):
    """One user-selected member; labels are hypotheses, not fundamentals."""

    symbol: str
    name: str
    tier: str
    theme: str
    exchange: str
    board: str
    authorization_eligible: bool = True
    eligibility_reasons: list[str] = Field(default_factory=list)


CLOUD_AI_UNIVERSE_ID = "cloud_ai_11_v1"
CLOUD_AI_RISK_THEME = "云算力主题"
CLOUD_AI_CONSTITUENTS: tuple[FixedThemeConstituent, ...] = (
    FixedThemeConstituent(symbol="688158", name="优刻得-W", tier="第一梯队", theme="AI算力租赁", exchange="SH", board="科创板"),
    FixedThemeConstituent(symbol="300846", name="首都在线", tier="第一梯队", theme="AI算力租赁", exchange="SZ", board="创业板"),
    FixedThemeConstituent(symbol="300857", name="协创数据", tier="第一梯队", theme="AI算力租赁", exchange="SZ", board="创业板"),
    FixedThemeConstituent(symbol="301396", name="宏景科技", tier="第一梯队", theme="AI算力租赁", exchange="SZ", board="创业板"),
    FixedThemeConstituent(
        symbol="839494", name="并行科技", tier="第一梯队", theme="AI算力租赁",
        exchange="BJ", board="北交所", authorization_eligible=False,
        eligibility_reasons=["beijing_exchange_analysis_only"],
    ),
    FixedThemeConstituent(symbol="300017", name="网宿科技", tier="第二梯队", theme="边缘云/配套", exchange="SZ", board="创业板"),
    FixedThemeConstituent(symbol="300442", name="润泽科技", tier="第二梯队", theme="边缘云/配套", exchange="SZ", board="创业板"),
    FixedThemeConstituent(symbol="603629", name="利通电子", tier="第二梯队", theme="边缘云/配套", exchange="SH", board="主板"),
    FixedThemeConstituent(symbol="601728", name="中国电信", tier="第三梯队", theme="国资云", exchange="SH", board="主板"),
    FixedThemeConstituent(symbol="600941", name="中国移动", tier="第三梯队", theme="国资云", exchange="SH", board="主板"),
    FixedThemeConstituent(symbol="002261", name="拓维信息", tier="第三梯队", theme="国资云", exchange="SZ", board="主板"),
)
CLOUD_AI_SYMBOLS = tuple(item.symbol for item in CLOUD_AI_CONSTITUENTS)


def cloud_ai_universe_version(as_of: date | datetime) -> str:
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    return f"{CLOUD_AI_UNIVERSE_ID}-{day:%Y-%m}"


def cloud_ai_member(symbol: str) -> FixedThemeConstituent | None:
    return next((item for item in CLOUD_AI_CONSTITUENTS if item.symbol == symbol), None)


def risk_theme_for_symbol(symbol: str) -> str:
    return CLOUD_AI_RISK_THEME if symbol in CLOUD_AI_SYMBOLS else ""


class Hs300HistoricalUniverse:
    """Build one monthly point-in-time universe from dated constituent records."""

    def __init__(self, members: list[UniverseMember], *, pool_size: int = 30) -> None:
        self.members = members
        self.pool_size = pool_size

    def snapshot(self, as_of: date | datetime) -> UniverseSnapshot:
        day = as_of.date() if isinstance(as_of, datetime) else as_of
        accepted: list[UniverseMember] = []
        rejected: dict[str, list[str]] = {}
        for item in self.members:
            reasons: list[str] = []
            if item.effective_from > day or (item.effective_to and item.effective_to < day):
                continue
            if (day - item.listing_date).days < 120:
                reasons.append("listed_less_than_120_days")
            if item.is_st:
                reasons.append("st")
            if item.delisting整理:
                reasons.append("delisting_period")
            if item.suspended:
                reasons.append("suspended")
            if not item.data_complete:
                reasons.append("data_incomplete")
            if item.price_limit_untradeable:
                reasons.append("price_limit_untradeable")
            if reasons:
                rejected[item.symbol] = reasons
            else:
                accepted.append(item)
        accepted.sort(key=lambda item: (-item.average_amount_20, item.symbol))
        selected = [item.symbol for item in accepted[: self.pool_size]]
        return UniverseSnapshot(
            as_of=day,
            version=f"hs300-{day:%Y-%m}",
            symbols=selected,
            rejected=rejected,
        )


class FixedCloudAiUniverseService:
    """Build the only active production pool from the user's fixed 11 symbols.

    The service refreshes market metadata for display and audit. A transient
    quote/profile failure does not silently remove a requested member; that
    member remains visible and is marked analysis-only until the gap is fixed.
    """

    universe_id = CLOUD_AI_UNIVERSE_ID

    def __init__(
        self,
        *,
        daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
        profile_loader: Callable[[str], dict[str, Any] | None] | None = None,
        max_workers: int = 6,
    ) -> None:
        if daily_loader is None or profile_loader is None:
            from pa_agent.data.eastmoney_client import (
                fetch_stock_daily_recent,
                fetch_stock_listing_profile,
            )

            daily_loader = daily_loader or fetch_stock_daily_recent
            profile_loader = profile_loader or fetch_stock_listing_profile
        self.daily_loader = daily_loader
        self.profile_loader = profile_loader
        self.max_workers = max_workers

    def current_version(self, as_of: date | datetime | None = None) -> str:
        point = as_of or datetime.now().astimezone()
        return cloud_ai_universe_version(point)

    def generate(
        self,
        *,
        as_of: date | datetime | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> UniverseSnapshot:
        day = (
            as_of.date() if isinstance(as_of, datetime)
            else as_of if isinstance(as_of, date)
            else datetime.now().astimezone().date()
        )
        raw: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._load_member, item): item
                for item in CLOUD_AI_CONSTITUENTS
            }
            for completed, future in enumerate(as_completed(futures), 1):
                item = futures[future]
                try:
                    raw[item.symbol] = future.result()
                except Exception as exc:  # noqa: BLE001
                    raw[item.symbol] = {"error": f"{type(exc).__name__}:{exc}"}
                if progress is not None:
                    progress(completed, len(CLOUD_AI_CONSTITUENTS), item.symbol)

        members: list[CurrentUniverseMember] = []
        rejected: dict[str, list[str]] = {}
        for rank, fixed in enumerate(CLOUD_AI_CONSTITUENTS, 1):
            item = raw.get(fixed.symbol) or {}
            bars = list(item.get("bars") or [])
            profile = dict(item.get("profile") or {})
            eligibility = list(fixed.eligibility_reasons)
            if item.get("error"):
                eligibility.append("member_data_fetch_failed")
            if len(bars) < 20 or any(float(row.get("amount") or 0) <= 0 for row in bars[-20:]):
                eligibility.append("insufficient_20_day_amount_data")
            listing_date = _parse_date(profile.get("listing_date"))
            if listing_date is None:
                eligibility.append("missing_listing_date")
            elif (day - listing_date).days < 120:
                eligibility.append("listed_less_than_120_days")
            name = str(profile.get("name") or fixed.name)
            if "ST" in name.upper():
                eligibility.append("st")
            if "退" in name:
                eligibility.append("delisting_period")
            if len(bars) >= 2 and _locked_at_price_limit(fixed.symbol, name, bars[-2], bars[-1]):
                eligibility.append("price_limit_untradeable")
            eligibility = list(dict.fromkeys(eligibility))
            if eligibility:
                rejected[fixed.symbol] = eligibility
            latest = bars[-1] if bars else {}
            amount = (
                sum(float(row.get("amount") or 0) for row in bars[-20:]) / 20
                if len(bars) >= 20 else 0.0
            )
            members.append(CurrentUniverseMember(
                rank=rank,
                symbol=fixed.symbol,
                name=fixed.name,
                exchange=fixed.exchange,
                industry=CLOUD_AI_RISK_THEME,
                theme=fixed.theme,
                tier=fixed.tier,
                board=fixed.board,
                average_amount_20=round(amount, 2),
                latest_price=(float(latest["close"]) if latest.get("close") is not None else None),
                latest_pct_chg=(float(latest["pct_chg"]) if latest.get("pct_chg") is not None else None),
                listing_date=listing_date,
                data_updated_at=str(latest.get("time") or ""),
                authorization_eligible=not eligibility and fixed.authorization_eligible,
                eligibility_reasons=eligibility,
            ))

        definition = [item.model_dump(mode="json") for item in CLOUD_AI_CONSTITUENTS]
        return UniverseSnapshot(
            as_of=day,
            version=cloud_ai_universe_version(day),
            symbols=list(CLOUD_AI_SYMBOLS),
            rejected=rejected,
            members=members,
            source_kind="user_fixed_theme_universe",
            source_url="user_defined:2026-08-13-cloud-ai-11",
            source_hash=hashlib.sha256(
                json.dumps(definition, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            source_as_of=day,
            input_member_count=len(CLOUD_AI_CONSTITUENTS),
            # The requested pool definition is complete even when an individual
            # member is temporarily analysis-only. Eligibility is per member.
            data_complete=True,
            completeness_reasons=[],
        )

    def _load_member(self, item: FixedThemeConstituent) -> dict[str, Any]:
        return {
            "bars": self.daily_loader(item.symbol, n=25, adjust="none"),
            "profile": self.profile_loader(item.symbol) or {},
        }


class CurrentHs300UniverseService:
    """Generate the current month's liquid top-30 from official CSI members."""

    def __init__(
        self,
        *,
        pool_size: int = 30,
        official_loader: Callable[[], OfficialConstituentFile] | None = None,
        daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
        profile_loader: Callable[[str], dict[str, Any] | None] | None = None,
        expected_constituent_count: int | None = 300,
        max_workers: int = 6,
    ) -> None:
        if daily_loader is None or profile_loader is None:
            from pa_agent.data.eastmoney_client import (
                fetch_stock_daily_recent,
                fetch_stock_listing_profile,
            )

            daily_loader = daily_loader or fetch_stock_daily_recent
            profile_loader = profile_loader or fetch_stock_listing_profile
        self.pool_size = pool_size
        self.official_loader = official_loader or load_official_current_hs300
        self.daily_loader = daily_loader
        self.profile_loader = profile_loader
        self.expected_constituent_count = expected_constituent_count
        self.max_workers = max_workers

    def generate(
        self,
        *,
        as_of: date | datetime | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> UniverseSnapshot:
        day = (
            as_of.date() if isinstance(as_of, datetime)
            else as_of if isinstance(as_of, date)
            else datetime.now().astimezone().date()
        )
        official = self.official_loader()
        constituents = {
            item.symbol: item for item in official.constituents if item.symbol.isdigit()
        }
        completeness: list[str] = []
        if (
            self.expected_constituent_count is not None
            and len(constituents) != self.expected_constituent_count
        ):
            completeness.append(
                f"official_member_count_{len(constituents)}_expected_"
                f"{self.expected_constituent_count}"
            )

        raw: dict[str, dict[str, Any]] = {}
        total = len(constituents)
        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._load_member, item): item.symbol
                for item in constituents.values()
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    raw[symbol] = future.result()
                except Exception as exc:  # noqa: BLE001
                    raw[symbol] = {"error": f"{type(exc).__name__}:{exc}"}
                completed += 1
                if progress is not None:
                    progress(completed, total, symbol)

        latest_market_date = max(
            (
                item["bars"][-1]["time"].date()
                for item in raw.values()
                if item.get("bars") and isinstance(item["bars"][-1].get("time"), datetime)
            ),
            default=None,
        )
        accepted: list[CurrentUniverseMember] = []
        rejected: dict[str, list[str]] = {}
        member_incomplete = False
        for symbol, official_item in sorted(constituents.items()):
            item = raw.get(symbol) or {}
            reasons: list[str] = []
            bars = list(item.get("bars") or [])
            profile = dict(item.get("profile") or {})
            name = str(profile.get("name") or official_item.name or symbol)
            if item.get("error"):
                reasons.append("member_data_fetch_failed")
                member_incomplete = True
            if len(bars) < 20 or any(float(row.get("amount") or 0) <= 0 for row in bars[-20:]):
                reasons.append("insufficient_20_day_amount_data")
                member_incomplete = True
            listing_date = _parse_date(profile.get("listing_date"))
            if listing_date is None:
                reasons.append("missing_listing_date")
                member_incomplete = True
            elif (day - listing_date).days < 120:
                reasons.append("listed_less_than_120_days")
            upper_name = name.upper()
            if "ST" in upper_name:
                reasons.append("st")
            if "退" in name:
                reasons.append("delisting_period")
            if bars and latest_market_date is not None:
                bar_date = _row_date(bars[-1])
                if bar_date is None:
                    reasons.append("invalid_latest_bar_time")
                    member_incomplete = True
                elif bar_date < latest_market_date:
                    reasons.append("suspended")
            if len(bars) >= 2 and _locked_at_price_limit(symbol, name, bars[-2], bars[-1]):
                reasons.append("price_limit_untradeable")
            if reasons:
                rejected[symbol] = list(dict.fromkeys(reasons))
                continue
            latest = bars[-1]
            average_amount = sum(float(row["amount"]) for row in bars[-20:]) / 20
            accepted.append(CurrentUniverseMember(
                rank=1,
                symbol=symbol,
                name=name,
                exchange=official_item.exchange,
                industry=str(profile.get("industry") or ""),
                average_amount_20=round(average_amount, 2),
                latest_price=float(latest["close"]),
                latest_pct_chg=(
                    float(latest["pct_chg"])
                    if latest.get("pct_chg") is not None else None
                ),
                listing_date=listing_date,
                data_updated_at=str(latest.get("time") or ""),
            ))
        if member_incomplete:
            completeness.append("member_data_incomplete")
        accepted.sort(key=lambda value: (-value.average_amount_20, value.symbol))
        selected = [
            value.model_copy(update={"rank": rank})
            for rank, value in enumerate(accepted[: self.pool_size], 1)
        ]
        if len(selected) < self.pool_size:
            completeness.append(f"selected_{len(selected)}_below_pool_size_{self.pool_size}")
        return UniverseSnapshot(
            as_of=day,
            version=f"hs300-{day:%Y-%m}",
            symbols=[item.symbol for item in selected],
            rejected=rejected,
            members=selected,
            source_kind="official_current_constituents",
            source_url=official.source_url,
            source_hash=official.source_hash,
            source_as_of=official.source_as_of,
            input_member_count=len(constituents),
            data_complete=not completeness,
            completeness_reasons=list(dict.fromkeys(completeness)),
        )

    def _load_member(self, item: OfficialConstituent) -> dict[str, Any]:
        return {
            "bars": self.daily_loader(item.symbol, n=25, adjust="none"),
            "profile": self.profile_loader(item.symbol) or {},
        }


def load_official_current_hs300(
    *, url: str = OFFICIAL_HS300_CURRENT_URL,
) -> OfficialConstituentFile:
    """Download and parse the official current CSI 300 constituent workbook."""
    import pandas as pd
    import requests

    response = requests.get(url, timeout=30)
    response.raise_for_status()
    content = response.content
    if not content.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError("official HS300 response is not an Excel workbook")
    frame = pd.read_excel(io.BytesIO(content))
    if frame.shape[1] < 9:
        raise ValueError("official HS300 workbook schema is incompatible")
    source_dates = [_parse_date(value) for value in frame.iloc[:, 0].tolist()]
    source_date = max((value for value in source_dates if value is not None), default=None)
    if source_date is None:
        raise ValueError("official HS300 workbook has no valid source date")
    result: list[OfficialConstituent] = []
    for row in frame.itertuples(index=False, name=None):
        raw_code = row[4]
        try:
            code = str(int(raw_code)).zfill(6)
        except (TypeError, ValueError):
            continue
        result.append(OfficialConstituent(
            symbol=code,
            name=str(row[5] or code).strip(),
            exchange=str(row[8] or "").strip(),
        ))
    if len({item.symbol for item in result}) != 300:
        raise ValueError(f"official HS300 workbook returned {len(result)} unique members")
    return OfficialConstituentFile(
        source_as_of=source_date,
        source_url=url,
        source_hash=hashlib.sha256(content).hexdigest(),
        constituents=result,
    )


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().replace("-", "")[:8]
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _row_date(row: dict[str, Any]) -> date | None:
    return _parse_date(row.get("time"))


def _locked_at_price_limit(
    symbol: str,
    name: str,
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    from pa_agent.data.ashare_limits import limit_pct, limit_prices

    previous_close = float(previous.get("close") or 0)
    if previous_close <= 0:
        return False
    upper, lower = limit_prices(previous_close, limit_pct(symbol, name))
    values = [float(current.get(key) or 0) for key in ("open", "high", "low", "close")]
    return all(abs(value - upper) <= 0.015 for value in values) or all(
        abs(value - lower) <= 0.015 for value in values
    )
