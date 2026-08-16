"""Point-in-time HS300 history and the user-defined current trading universe.

Historical validation accepts caller-supplied dated membership only.  The
current-month service downloads the official CSI constituent workbook and may
not be used to backfill historical membership.
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from pa_agent.data.ashare_common import is_a_share_stock_symbol

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
    parent_version: str = ""
    member_hash: str = ""
    change_kind: str = ""
    change_symbol: str = ""
    change_summary: str = ""
    revision_created_at: str = ""

    @model_validator(mode="after")
    def require_a_share_stock_members(self) -> UniverseSnapshot:
        """Fail closed before a non-A-share instrument can enter a live pool."""
        candidates = [*self.symbols, *(item.symbol for item in self.members)]
        invalid = sorted({
            str(symbol).strip()
            for symbol in candidates
            if not is_a_share_stock_symbol(str(symbol))
        })
        if invalid:
            raise ValueError(
                "交易股票池仅允许A股股票，发现非A股或非股票标的: "
                + ", ".join(invalid)
            )
        return self


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


class UniverseMutationResult(BaseModel):
    """One committed, auditable change to the private A-share pool."""

    action: str
    symbol: str
    name: str
    previous_version: str
    previous_count: int = Field(ge=0)
    snapshot: UniverseSnapshot


class UniverseMutationBlocked(ValueError):
    """Raised when removing a symbol would hide live account or plan risk."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(dict.fromkeys(reasons))
        super().__init__("；".join(self.reasons))


CLOUD_AI_UNIVERSE_ID = "cloud_ai_11_v1"
CLOUD_AI_STRATEGY_FROZEN_AT = "2026-08-14T15:55:00+08:00"
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
CLOUD_AI_AUTHORIZATION_SYMBOLS = tuple(
    item.symbol for item in CLOUD_AI_CONSTITUENTS if item.authorization_eligible
)


def cloud_ai_universe_version(as_of: date | datetime) -> str:
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    return f"{CLOUD_AI_UNIVERSE_ID}-{day:%Y-%m}"


def cloud_ai_definition_hash() -> str:
    payload = [item.model_dump(mode="json") for item in CLOUD_AI_CONSTITUENTS]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


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

        return UniverseSnapshot(
            as_of=day,
            version=cloud_ai_universe_version(day),
            symbols=list(CLOUD_AI_SYMBOLS),
            rejected=rejected,
            members=members,
            source_kind="user_fixed_theme_universe",
            source_url="user_defined:2026-08-13-cloud-ai-11",
            source_hash=cloud_ai_definition_hash(),
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


PRIVATE_A_SHARE_UNIVERSE_ID = "ashare_private_pool"


class ManagedAshareUniverseService:
    """Versioned private A-share pool layered on the original fixed seed pool.

    The database is the current-definition authority once a user revision has
    been committed. Historical snapshots are never edited in place. Refreshes
    of a managed pool also create a new revision so old signals and validation
    evidence remain traceable to the exact member set that produced them.
    """

    universe_id = PRIVATE_A_SHARE_UNIVERSE_ID

    def __init__(
        self,
        store: Any,
        *,
        daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
        profile_loader: Callable[[str], dict[str, Any] | None] | None = None,
        base_service: FixedCloudAiUniverseService | None = None,
        validation_epochs: Any | None = None,
        now: Callable[[], datetime] | None = None,
        max_workers: int = 6,
    ) -> None:
        if daily_loader is None or profile_loader is None:
            from pa_agent.data.eastmoney_client import (
                fetch_stock_daily_recent,
                fetch_stock_listing_profile,
            )

            daily_loader = daily_loader or fetch_stock_daily_recent
            profile_loader = profile_loader or fetch_stock_listing_profile
        self.store = store
        self.daily_loader = daily_loader
        self.profile_loader = profile_loader
        self.base_service = base_service or FixedCloudAiUniverseService(
            daily_loader=daily_loader,
            profile_loader=profile_loader,
            max_workers=max_workers,
        )
        self.validation_epochs = validation_epochs
        self._now = now or (lambda: datetime.now().astimezone())
        self.max_workers = max_workers
        self._mutation_lock = threading.RLock()

    def current_version(self, as_of: date | datetime | None = None) -> str:
        current = self._latest_snapshot()
        if current is not None and current.source_kind == "user_managed_a_share_universe":
            return current.version
        return self.base_service.current_version(as_of)

    def generate(
        self,
        *,
        as_of: date | datetime | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> UniverseSnapshot:
        """Generate the seed pool or a new metadata-refresh revision."""
        with self._mutation_lock:
            current = self._latest_snapshot()
            if current is None or current.source_kind != "user_managed_a_share_universe":
                return self.base_service.generate(as_of=as_of, progress=progress)
            return self._build_revision(
                symbols=current.symbols,
                previous=current,
                change_kind="refresh",
                change_symbol="",
                change_summary="刷新全部成员行情与交易资格",
                as_of=as_of,
                progress=progress,
            )

    def add_member(
        self,
        value: str,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> UniverseMutationResult:
        """Resolve, strictly validate and commit one new A-share member."""
        with self._mutation_lock:
            previous = self._current_or_seed(progress=progress)
            preferred = [item.model_dump(mode="json") for item in previous.members]
            raw = str(value or "").strip()
            if raw.isdigit() and len(raw) != 6:
                raise ValueError("请输入6位A股股票代码，或输入完整A股名称")
            from pa_agent.data.eastmoney_client import resolve_a_share_stock_name

            _exchange, symbol = resolve_a_share_stock_name(
                raw,
                preferred_members=preferred,
            )
            if not is_a_share_stock_symbol(symbol):
                raise ValueError("交易股票池仅允许6位A股股票代码")
            if symbol in previous.symbols:
                member = next(
                    (item for item in previous.members if item.symbol == symbol),
                    None,
                )
                name = member.name if member is not None else symbol
                raise ValueError(f"{name}（{symbol}）已在当前股票池中")
            validated = self._load_managed_member(
                symbol,
                previous=None,
                day=self._now().date(),
                strict=True,
            )
            snapshot = self._build_revision(
                symbols=[*previous.symbols, symbol],
                previous=previous,
                change_kind="add",
                change_symbol=symbol,
                change_summary=f"新增 {validated.name}（{symbol}）",
                preloaded={symbol: validated},
                progress=progress,
            )
            self._commit_revision(snapshot)
            return UniverseMutationResult(
                action="add",
                symbol=symbol,
                name=validated.name,
                previous_version=previous.version,
                previous_count=len(previous.symbols),
                snapshot=snapshot,
            )

    def validate_watchlist_member(self, value: str) -> CurrentUniverseMember:
        """Resolve and validate an A share without mutating the strategy universe.

        Personal monitoring must not silently reset the versioned strategy pool
        or its OOS epoch.  This method intentionally reuses the exact security,
        listing-age, liquidity, ST and price-limit checks used by ``add_member``
        and returns a profile that the watchlist can persist independently.
        """
        current = self._latest_snapshot()
        preferred = (
            [item.model_dump(mode="json") for item in current.members]
            if current is not None else []
        )
        raw = str(value or "").strip()
        if raw.isdigit() and len(raw) != 6:
            raise ValueError("请输入6位A股股票代码，或输入完整A股名称")
        from pa_agent.data.eastmoney_client import resolve_a_share_stock_name

        _exchange, symbol = resolve_a_share_stock_name(
            raw,
            preferred_members=preferred,
        )
        if not is_a_share_stock_symbol(symbol):
            raise ValueError("我的监控池仅允许6位A股股票代码")
        previous = None
        if current is not None:
            previous = next(
                (item for item in current.members if item.symbol == symbol),
                None,
            )
        return self._load_managed_member(
            symbol,
            previous=previous,
            day=self._now().date(),
            strict=True,
        )

    def remove_member(
        self,
        symbol: str,
        *,
        broker_snapshot: Any | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> UniverseMutationResult:
        """Commit a removal only when no live account or plan risk is hidden."""
        with self._mutation_lock:
            previous = self._current_or_seed(progress=progress)
            code = str(symbol or "").strip()[-6:]
            if code not in previous.symbols:
                raise ValueError(f"{code or '所选股票'}不在当前股票池中")
            member = next(
                (item for item in previous.members if item.symbol == code),
                None,
            )
            name = member.name if member is not None else code
            blockers = self.removal_blockers(code, broker_snapshot=broker_snapshot)
            if blockers:
                raise UniverseMutationBlocked(blockers)
            snapshot = self._build_revision(
                symbols=[item for item in previous.symbols if item != code],
                previous=previous,
                change_kind="remove",
                change_symbol=code,
                change_summary=f"移除 {name}（{code}）",
                progress=progress,
            )
            self._commit_revision(snapshot)
            return UniverseMutationResult(
                action="remove",
                symbol=code,
                name=name,
                previous_version=previous.version,
                previous_count=len(previous.symbols),
                snapshot=snapshot,
            )

    def removal_blockers(
        self,
        symbol: str,
        *,
        broker_snapshot: Any | None = None,
    ) -> list[str]:
        """Return deterministic reasons why a member must stay under management."""
        code = str(symbol or "").strip()[-6:]
        reasons: list[str] = []
        snapshot = broker_snapshot
        if snapshot is None:
            stored = self.store.latest_broker_snapshot()
            snapshot = (stored or {}).get("snapshot")
        payload = (
            snapshot.model_dump(mode="json")
            if hasattr(snapshot, "model_dump")
            else dict(snapshot or {})
        )
        for position in payload.get("positions") or []:
            item = position if isinstance(position, dict) else position.model_dump(mode="json")
            if str(item.get("symbol") or "") == code and int(item.get("quantity") or 0) > 0:
                reasons.append("同花顺账户仍有该股票持仓，请在“持仓与退出”完成退出后再移除")
                break
        terminal_order_statuses = {
            "filled", "cancelled", "canceled", "rejected", "expired", "废单", "已撤",
        }
        for order in payload.get("orders") or []:
            item = order if isinstance(order, dict) else order.model_dump(mode="json")
            if (
                str(item.get("symbol") or "") == code
                and str(item.get("status") or "").strip().casefold()
                not in terminal_order_statuses
            ):
                reasons.append("同花顺仍有该股票未完成委托，请先完成或撤销并对账")
                break
        protected = {
            "proposed", "triggered", "authorized", "awaiting_user_confirmation",
            "reconciliation_required", "submitted", "partially_filled", "filled",
            "executed_open", "exit_detected",
        }
        plans = self.store.list_plans(symbol=code, limit=1000)
        if any(str(item.get("status") or "") in protected for item in plans):
            reasons.append("该股票存在开放交易计划或待对账记录，请先到“交易计划”处理")
        if any(
            str(item.get("shadow_status") or "")
            in {"proposed", "entry_touched", "open", "exit_detected"}
            for item in plans
        ):
            reasons.append("该股票仍有开放影子交易生命周期，不能直接移除")
        return list(dict.fromkeys(reasons))

    def _current_or_seed(
        self,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> UniverseSnapshot:
        return self._latest_snapshot() or self.base_service.generate(progress=progress)

    def _latest_snapshot(self) -> UniverseSnapshot | None:
        if self.store is None or not getattr(self.store, "available", False):
            return None
        rows = self.store.list_universe_snapshots(limit=1)
        if not rows:
            return None
        return UniverseSnapshot.model_validate(rows[0]["snapshot"])

    def _build_revision(
        self,
        *,
        symbols: list[str],
        previous: UniverseSnapshot,
        change_kind: str,
        change_symbol: str,
        change_summary: str,
        as_of: date | datetime | None = None,
        preloaded: dict[str, CurrentUniverseMember] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> UniverseSnapshot:
        point = as_of or self._now()
        day = point.date() if isinstance(point, datetime) else point
        prior_members = {item.symbol: item for item in previous.members}
        loaded: dict[str, CurrentUniverseMember] = dict(preloaded or {})
        pending = [item for item in symbols if item not in loaded]
        if pending:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(
                        self._load_managed_member,
                        symbol,
                        prior_members.get(symbol),
                        day,
                        False,
                    ): symbol
                    for symbol in pending
                }
                for completed, future in enumerate(as_completed(futures), 1):
                    symbol = futures[future]
                    try:
                        loaded[symbol] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        old = prior_members.get(symbol)
                        if old is None:
                            raise
                        reasons = list(dict.fromkeys([
                            *old.eligibility_reasons,
                            f"member_data_fetch_failed:{type(exc).__name__}",
                        ]))
                        loaded[symbol] = old.model_copy(update={
                            "authorization_eligible": False,
                            "eligibility_reasons": reasons,
                        })
                    if progress is not None:
                        progress(completed, len(pending), symbol)
        members: list[CurrentUniverseMember] = []
        rejected: dict[str, list[str]] = {}
        for rank, symbol in enumerate(symbols, 1):
            member = loaded[symbol].model_copy(update={"rank": rank})
            members.append(member)
            if member.eligibility_reasons:
                rejected[symbol] = member.eligibility_reasons
        member_hash = _private_member_hash(symbols)
        now = self._now()
        revision_hash = hashlib.sha256(
            json.dumps(
                {
                    "parent_version": previous.version,
                    "change_kind": change_kind,
                    "change_symbol": change_symbol,
                    "created_at": now.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        version = (
            f"{PRIVATE_A_SHARE_UNIVERSE_ID}-{now:%Y%m%d-%H%M%S%f}-"
            f"{member_hash[:10]}-{revision_hash[:6]}"
        )
        return UniverseSnapshot(
            as_of=day,
            version=version,
            symbols=symbols,
            rejected=rejected,
            members=members,
            source_kind="user_managed_a_share_universe",
            source_url="local:user_managed_private_a_share_pool",
            source_hash=member_hash,
            source_as_of=day,
            input_member_count=len(symbols),
            data_complete=True,
            completeness_reasons=[],
            parent_version=previous.version,
            member_hash=member_hash,
            change_kind=change_kind,
            change_symbol=change_symbol,
            change_summary=change_summary,
            revision_created_at=now.isoformat(),
        )

    def _load_managed_member(
        self,
        symbol: str,
        previous: CurrentUniverseMember | None,
        day: date,
        strict: bool,
    ) -> CurrentUniverseMember:
        bars = list(self.daily_loader(symbol, n=25, adjust="none") or [])
        profile = dict(self.profile_loader(symbol) or {})
        reasons: list[str] = []
        if not profile.get("name") and previous is None:
            reasons.append("missing_security_identity")
        if len(bars) < 20 or any(
            float(row.get("amount") or 0) <= 0 for row in bars[-20:]
        ):
            reasons.append("insufficient_20_day_amount_data")
        listing_date = _parse_date(profile.get("listing_date"))
        if listing_date is None:
            reasons.append("missing_listing_date")
        elif (day - listing_date).days < 120:
            reasons.append("listed_less_than_120_days")
        name = str(profile.get("name") or (previous.name if previous else symbol)).strip()
        if "ST" in name.upper():
            reasons.append("st")
        if "退" in name:
            reasons.append("delisting_period")
        if len(bars) >= 2 and _locked_at_price_limit(symbol, name, bars[-2], bars[-1]):
            reasons.append("price_limit_untradeable")
        if previous is not None and "beijing_exchange_analysis_only" in previous.eligibility_reasons:
            reasons.insert(0, "beijing_exchange_analysis_only")
        reasons = list(dict.fromkeys(reasons))
        if strict and reasons:
            friendly = {
                "missing_security_identity": "无法核验证券名称",
                "insufficient_20_day_amount_data": "20日行情或成交额数据不完整",
                "missing_listing_date": "上市日期缺失",
                "listed_less_than_120_days": "上市不足120日",
                "st": "ST股票不允许加入",
                "delisting_period": "退市整理股票不允许加入",
                "price_limit_untradeable": "当前涨跌停状态无法正常成交",
            }
            detail = "、".join(friendly.get(item, item) for item in reasons)
            raise ValueError(f"{symbol}未通过A股股票池准入校验：{detail}")
        latest = bars[-1] if bars else {}
        amount = (
            sum(float(row.get("amount") or 0) for row in bars[-20:]) / 20
            if len(bars) >= 20 else 0.0
        )
        return CurrentUniverseMember(
            rank=previous.rank if previous else 1,
            symbol=symbol,
            name=name,
            exchange=str(profile.get("exchange") or _exchange_for_symbol(symbol)),
            industry=str(profile.get("industry") or (previous.industry if previous else "")),
            theme=previous.theme if previous else "用户关注",
            tier=previous.tier if previous else "自定义",
            board=previous.board if previous else _board_for_symbol(symbol),
            average_amount_20=round(amount, 2),
            latest_price=(float(latest["close"]) if latest.get("close") is not None else None),
            latest_pct_chg=(
                float(latest["pct_chg"])
                if latest.get("pct_chg") is not None else None
            ),
            listing_date=listing_date,
            data_updated_at=str(latest.get("time") or ""),
            authorization_eligible=not reasons,
            eligibility_reasons=reasons,
        )

    def _commit_revision(self, snapshot: UniverseSnapshot) -> None:
        self.store.upsert_universe_snapshot(
            snapshot,
            source_updated_at=snapshot.revision_created_at,
            data_complete=snapshot.data_complete,
        )
        if self.validation_epochs is not None:
            self.validation_epochs.activate(snapshot)
        self._reset_strategy_validation(snapshot)

    def _reset_strategy_validation(self, snapshot: UniverseSnapshot) -> None:
        """Fail closed after a member-set change without deleting old evidence."""
        if snapshot.change_kind not in {"add", "remove"}:
            return
        from pa_agent.trading.quant import StrategyState
        from pa_agent.trading.stability import PerformanceEvidence, StateTransition
        from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID

        current = StrategyState(
            self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        )
        target = (
            StrategyState.PAUSED
            if current in {
                StrategyState.SHADOW,
                StrategyState.ACTIVE,
                StrategyState.REDUCED,
                StrategyState.PAUSED,
            }
            else current
        )
        self.store.record_strategy_transition(
            StateTransition(
                previous=current,
                current=target,
                reasons=[
                    "universe_revision_requires_new_oos_and_shadow_validation",
                    snapshot.version,
                ],
            ),
            PerformanceEvidence(dataset="universe_revision", trade_count=0),
            strategy_id=TOPDOWN_STRATEGY_ID,
        )


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


def _private_member_hash(symbols: list[str]) -> str:
    """Hash the ordered member definition, not mutable quote metadata."""
    payload = json.dumps(
        [str(symbol).strip() for symbol in symbols],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _exchange_for_symbol(symbol: str) -> str:
    code = str(symbol).strip()[-6:]
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    return "BJ"


def _board_for_symbol(symbol: str) -> str:
    code = str(symbol).strip()[-6:]
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "北交所"
    return "主板"
