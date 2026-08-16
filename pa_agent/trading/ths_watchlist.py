"""Read, mirror and deterministically scan TongHuaShun self-selected stocks."""

from __future__ import annotations

import hashlib
import json
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.data.ashare_common import is_a_share_stock_symbol
from pa_agent.trading.quant import SignalDecision
from pa_agent.trading.topdown import MANUAL_EXCEPTION_STRATEGY_ID

THS_WATCHLIST_SOURCE = "ths_watchlist"


class ThsWatchlistMember(BaseModel):
    symbol: str
    market: str
    categories: list[str] = Field(default_factory=list)


class ThsWatchlistSnapshot(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    captured_at: str
    source_updated_at: str
    source_hash: str
    source_fingerprint: str
    status: str = "complete"
    categories: list[str] = Field(default_factory=list)
    members: list[ThsWatchlistMember] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)


class ThsWatchlistScanReport(BaseModel):
    scan_id: str
    source_hash: str
    base_pool_version: str
    captured_at: str
    signal_date: str = ""
    data_complete: bool = True
    data_gaps: list[str] = Field(default_factory=list)
    total: int = 0
    next_session_candidates: int = 0
    actionable: int = 0
    results: list[dict[str, Any]] = Field(default_factory=list)


class ThsWatchlistFileReader:
    """Parse the active user's local blockstockV3.xml without account secrets."""

    def read(self, install_root: str | Path) -> ThsWatchlistSnapshot:
        source = self._locate_source(Path(install_root))
        raw = source.read_bytes()
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("同花顺自选文件异常过大，拒绝读取")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError("同花顺自选文件不是可识别的XML") from exc

        categories: list[str] = []
        members: dict[str, ThsWatchlistMember] = {}
        rejected: list[dict[str, str]] = []
        for block in root.findall(".//Block"):
            category = str(block.attrib.get("name") or "未分类").strip() or "未分类"
            if category not in categories:
                categories.append(category)
            for security in block.findall("security"):
                market = str(security.attrib.get("market") or "").strip().upper()
                symbol = str(security.attrib.get("code") or "").strip()
                if market not in {"USHA", "USZA"} or not is_a_share_stock_symbol(symbol):
                    rejected.append({
                        "category": category,
                        "symbol": symbol,
                        "reason": "非沪深A股，已排除",
                    })
                    continue
                existing = members.get(symbol)
                if existing is None:
                    members[symbol] = ThsWatchlistMember(
                        symbol=symbol,
                        market=market,
                        categories=[category],
                    )
                elif category not in existing.categories:
                    existing.categories.append(category)

        stat = source.stat()
        source_hash = hashlib.sha256(raw).hexdigest()
        source_fingerprint = hashlib.sha256(
            str(source.resolve()).casefold().encode("utf-8")
        ).hexdigest()
        captured = datetime.now(UTC).astimezone().isoformat()
        return ThsWatchlistSnapshot(
            captured_at=captured,
            source_updated_at=datetime.fromtimestamp(
                stat.st_mtime, tz=datetime.now().astimezone().tzinfo
            ).isoformat(),
            source_hash=source_hash,
            source_fingerprint=source_fingerprint,
            categories=categories,
            members=sorted(members.values(), key=lambda item: item.symbol),
            rejected=rejected,
        )

    @staticmethod
    def _locate_source(install_root: Path) -> Path:
        users = install_root.resolve() / "bin" / "users"
        if not users.is_dir():
            raise FileNotFoundError("同花顺用户数据目录不存在")
        excluded = {"config", "internal", "public"}
        candidates = sorted(
            path
            for path in users.glob("*/blockstockV3.xml")
            if path.parent.name.casefold() not in excluded and path.is_file()
        )
        if not candidates:
            raise FileNotFoundError("未找到同花顺自选分类文件 blockstockV3.xml")
        if len(candidates) != 1:
            raise RuntimeError("检测到多个同花顺用户自选文件，请先在客户端确认当前用户")
        return candidates[0]


class ThsWatchlistScanService:
    """Mirror all A-share categories and scan them with the production strategy."""

    def __init__(
        self,
        store: Any,
        scanner: Any,
        *,
        install_root: str | Path,
        reader: ThsWatchlistFileReader | None = None,
        max_workers: int = 6,
    ) -> None:
        self.store = store
        self.scanner = scanner
        self.install_root = Path(install_root)
        self.reader = reader or ThsWatchlistFileReader()
        self.max_workers = max(1, int(max_workers))

    def synchronize(self) -> ThsWatchlistSnapshot:
        snapshot = self.reader.read(self.install_root)
        self.store.sync_watchlist_source(
            THS_WATCHLIST_SOURCE,
            [
                {
                    "symbol": item.symbol,
                    "name": item.symbol,
                    "metadata": {
                        "ths_categories": item.categories,
                        "ths_market": item.market,
                        "ths_source_hash": snapshot.source_hash,
                        "ths_source_updated_at": snapshot.source_updated_at,
                    },
                }
                for item in snapshot.members
            ],
        )
        self.store.add_ths_watchlist_sync(snapshot)
        return snapshot

    def scan(
        self,
        *,
        force: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ThsWatchlistScanReport:
        snapshot = self.synchronize()
        universe_rows = self.store.list_universe_snapshots(limit=1)
        if not universe_rows or not universe_rows[0].get("data_complete"):
            return self._persist_incomplete(
                snapshot,
                base_pool_version="",
                gaps=["当前系统策略池数据不完整，无法计算可信市场宽度"],
            )
        universe = dict(universe_rows[0].get("snapshot") or {})
        base_pool_version = str(universe.get("version") or "")
        if not force:
            latest_base_day = self._latest_base_signal_date(base_pool_version)
            if latest_base_day:
                cached = self._cached_report(
                    snapshot,
                    universe=universe,
                    base_pool_version=base_pool_version,
                    signal_date=latest_base_day,
                )
                if cached is not None:
                    return cached
        base_scan = self.scanner.scan(universe, progress=None)
        if not base_scan.data_complete or base_scan.market_breadth_pct is None:
            return self._persist_incomplete(
                snapshot,
                base_pool_version=base_pool_version,
                gaps=["系统策略池日线扫描不完整", *base_scan.data_gaps],
            )

        base_decisions = {item.symbol: item for item in base_scan.decisions}
        signal_date = str(base_scan.signal_date or "")
        scan_id = _scan_id(snapshot.source_hash, base_pool_version, signal_date)
        system_symbols = set(str(item) for item in universe.get("symbols") or [])
        system_member_by_symbol = {
            str(item.get("symbol") or ""): dict(item)
            for item in universe.get("members") or []
            if str(item.get("symbol") or "")
        }
        member_by_symbol = {item.symbol: item for item in snapshot.members}
        if not force:
            cached = self._cached_report(
                snapshot,
                universe=universe,
                base_pool_version=base_pool_version,
                signal_date=signal_date,
            )
            if cached is not None:
                return cached
        decisions: dict[str, SignalDecision] = {}
        outside = [item for item in snapshot.members if item.symbol not in system_symbols]
        for item in snapshot.members:
            if item.symbol in base_decisions:
                decisions[item.symbol] = base_decisions[item.symbol]

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self.scanner.evaluate_manual_exception,
                    item.symbol,
                    base_pool_version=base_pool_version,
                    market_breadth_pct=float(base_scan.market_breadth_pct),
                ): item.symbol
                for item in outside
            }
            completed = len(decisions)
            total = len(snapshot.members)
            if progress is not None:
                for current, symbol in enumerate(sorted(decisions), start=1):
                    progress(min(current, total), total, symbol)
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    decisions[symbol] = future.result()
                except Exception as exc:  # noqa: BLE001
                    decisions[symbol] = SignalDecision(
                        status="reject",
                        strategy_id=MANUAL_EXCEPTION_STRATEGY_ID,
                        parameter_version="watchlist-scan-error-1.0.0",
                        pool_version=(
                            f"manual-exception-{base_pool_version}-{signal_date}-{symbol}"
                        ),
                        symbol=symbol,
                        signal_time=datetime.now().astimezone().isoformat(),
                        reasons=[f"watchlist_scan_failed:{type(exc).__name__}"],
                        condition_snapshot={
                            "manual_exception": True,
                            "base_pool_version": base_pool_version,
                        },
                    )
                completed += 1
                if progress is not None:
                    progress(completed, total, symbol)

        results: list[dict[str, Any]] = []
        for symbol in sorted(member_by_symbol):
            decision = decisions.get(symbol)
            member = member_by_symbol[symbol]
            if decision is None:
                result = _missing_result(
                    symbol,
                    member.categories,
                    snapshot.source_hash,
                    base_pool_version,
                    signal_date,
                    ["该股票未产生可回溯的策略结果"],
                )
            else:
                self.store.add_quant_signal(decision)
                result = _decision_result(
                    decision,
                    categories=member.categories,
                    source_hash=snapshot.source_hash,
                    base_pool_version=base_pool_version,
                    signal_date=signal_date,
                    in_system_pool=symbol in system_symbols,
                    fallback_name=str(
                        (system_member_by_symbol.get(symbol) or {}).get("name") or ""
                    ),
                    fallback_industry=str(
                        (system_member_by_symbol.get(symbol) or {}).get("industry") or ""
                    ),
                )
            self.store.upsert_ths_watchlist_scan_result(scan_id, result)
            self._apply_member_result(
                member,
                result,
                scan_id=scan_id,
                source_hash=snapshot.source_hash,
                in_system_pool=symbol in system_symbols,
            )
            results.append(result)

        report = ThsWatchlistScanReport(
            scan_id=scan_id,
            source_hash=snapshot.source_hash,
            base_pool_version=base_pool_version,
            captured_at=datetime.now(UTC).astimezone().isoformat(),
            signal_date=signal_date,
            total=len(results),
            next_session_candidates=sum(
                item["actionable_stage"] == "next_session_candidate"
                for item in results
            ),
            actionable=sum(item["actionable_stage"] == "actionable" for item in results),
            results=results,
        )
        return report

    def _latest_base_signal_date(self, base_pool_version: str) -> str:
        strategy = getattr(self.scanner, "strategy", None)
        settings = getattr(strategy, "settings", None)
        strategy_id = str(getattr(settings, "strategy_id", "") or "")
        if not strategy_id:
            return ""
        records = self.store.list_quant_signals(
            strategy_id=strategy_id,
            limit=2000,
        )
        return max(
            (
                str(item.get("signal_time") or "")[:10]
                for item in records
                if str(item.get("pool_version") or "") == base_pool_version
            ),
            default="",
        )

    def _cached_report(
        self,
        snapshot: ThsWatchlistSnapshot,
        *,
        universe: dict[str, Any],
        base_pool_version: str,
        signal_date: str,
    ) -> ThsWatchlistScanReport | None:
        scan_id = _scan_id(snapshot.source_hash, base_pool_version, signal_date)
        existing = self.store.list_ths_watchlist_scan_results(scan_id=scan_id)
        cached_results = [dict(item.get("result") or {}) for item in existing]
        expected_symbols = {item.symbol for item in snapshot.members}
        actual_symbols = {str(item.get("symbol") or "") for item in cached_results}
        if actual_symbols != expected_symbols:
            return None
        system_symbols = set(str(item) for item in universe.get("symbols") or [])
        member_by_symbol = {item.symbol: item for item in snapshot.members}
        for result in cached_results:
            symbol = str(result.get("symbol") or "")
            self._apply_member_result(
                member_by_symbol[symbol],
                result,
                scan_id=scan_id,
                source_hash=snapshot.source_hash,
                in_system_pool=symbol in system_symbols,
            )
        return ThsWatchlistScanReport(
            scan_id=scan_id,
            source_hash=snapshot.source_hash,
            base_pool_version=base_pool_version,
            captured_at=datetime.now(UTC).astimezone().isoformat(),
            signal_date=signal_date,
            total=len(cached_results),
            next_session_candidates=sum(
                item.get("actionable_stage") == "next_session_candidate"
                for item in cached_results
            ),
            actionable=sum(
                item.get("actionable_stage") == "actionable"
                for item in cached_results
            ),
            results=cached_results,
        )

    def _apply_member_result(
        self,
        member: ThsWatchlistMember,
        result: dict[str, Any],
        *,
        scan_id: str,
        source_hash: str,
        in_system_pool: bool,
    ) -> None:
        """Restore the source projection after both fresh and cached scans."""
        self.store.upsert_watchlist_member(
            symbol=member.symbol,
            name=str(result.get("name") or member.symbol),
            source=THS_WATCHLIST_SOURCE,
            metadata={
                "ths_categories": member.categories,
                "ths_market": member.market,
                "ths_source_hash": source_hash,
                "ths_scan_id": scan_id,
                "ths_scan_status": result["status"],
                "ths_actionable_stage": result["actionable_stage"],
                "ths_reason": result["reason_text"],
                "manual_exception_eligible": bool(
                    result.get("manual_exception_eligible")
                ),
                "authorization_eligible": bool(
                    result.get("manual_exception_eligible") or in_system_pool
                ),
                "industry": str(result.get("industry") or ""),
            },
        )

    def _persist_incomplete(
        self,
        snapshot: ThsWatchlistSnapshot,
        *,
        base_pool_version: str,
        gaps: list[str],
    ) -> ThsWatchlistScanReport:
        scan_id = _scan_id(snapshot.source_hash, base_pool_version, "incomplete")
        results = []
        for member in snapshot.members:
            result = _missing_result(
                member.symbol,
                member.categories,
                snapshot.source_hash,
                base_pool_version,
                "",
                gaps,
            )
            self.store.upsert_ths_watchlist_scan_result(scan_id, result)
            self.store.upsert_watchlist_member(
                symbol=member.symbol,
                name=member.symbol,
                source=THS_WATCHLIST_SOURCE,
                metadata={
                    "ths_categories": member.categories,
                    "ths_market": member.market,
                    "ths_source_hash": snapshot.source_hash,
                    "ths_scan_id": scan_id,
                    "ths_scan_status": "data_incomplete",
                    "ths_actionable_stage": "data_incomplete",
                    "ths_reason": result["reason_text"],
                    "manual_exception_eligible": False,
                    "authorization_eligible": False,
                },
            )
            results.append(result)
        return ThsWatchlistScanReport(
            scan_id=scan_id,
            source_hash=snapshot.source_hash,
            base_pool_version=base_pool_version,
            captured_at=datetime.now(UTC).astimezone().isoformat(),
            data_complete=False,
            data_gaps=list(dict.fromkeys(gaps)),
            total=len(results),
            results=results,
        )


def _decision_result(
    decision: SignalDecision,
    *,
    categories: list[str],
    source_hash: str,
    base_pool_version: str,
    signal_date: str,
    in_system_pool: bool,
    fallback_name: str = "",
    fallback_industry: str = "",
) -> dict[str, Any]:
    snapshot = dict(decision.condition_snapshot or {})
    name = str(
        snapshot.get("expected_security_name") or fallback_name or decision.symbol
    )
    industry = str(snapshot.get("industry") or fallback_industry or "")
    allowed = str(decision.status.value) == "allow"
    stage = "next_session_candidate" if allowed else "not_ready"
    manual_eligible = bool(
        in_system_pool
        or (
            name != decision.symbol
            and industry
            and not any(_eligibility_reason(reason) for reason in decision.reasons)
        )
    )
    return {
        "symbol": decision.symbol,
        "name": name,
        "industry": industry,
        "categories": categories,
        "source_hash": source_hash,
        "base_pool_version": base_pool_version,
        "signal_date": signal_date or str(decision.signal_time)[:10],
        "status": str(decision.status.value),
        "actionable_stage": stage,
        "reason_text": _decision_reason_text(decision, in_system_pool=in_system_pool),
        "reasons": list(decision.reasons),
        "trigger_price": decision.trigger_price,
        "max_entry_price": decision.max_entry_price,
        "initial_stop": decision.initial_stop,
        "valid_until": decision.valid_until,
        "strategy_id": decision.strategy_id,
        "parameter_version": decision.parameter_version,
        "in_system_pool": in_system_pool,
        "manual_exception_eligible": manual_eligible,
        "decision": decision.model_dump(mode="json"),
    }


def _decision_reason_text(decision: SignalDecision, *, in_system_pool: bool) -> str:
    values = decision.condition_snapshot or {}
    if str(decision.status.value) == "allow":
        channel = "系统策略池正常通道" if in_system_pool else "池外例外半风险通道"
        return (
            f"日线趋势、回调支撑、重新站回MA20、量价和收盘位置均通过；"
            f"20日高点回调{_fmt(values.get('pullback_atr'))} ATR，"
            f"量比{_fmt(values.get('volume_ratio'))}，市场宽度"
            f"{_fmt(values.get('market_breadth_pct'))}%。下个交易日仅在"
            f"{decision.trigger_price}触发且不高于{decision.max_entry_price}时进入"
            f"15分钟4:3:2:1连续确认，初始止损{decision.initial_stop}；{channel}，不追价。"
        )
    translated = [_reason_label(reason) for reason in decision.reasons]
    measurements = (
        f"当前回调{_fmt(values.get('pullback_atr'))} ATR，"
        f"量比{_fmt(values.get('volume_ratio'))}，市场宽度"
        f"{_fmt(values.get('market_breadth_pct'))}%"
    )
    return f"本次不能进入下个交易日候选：{'；'.join(translated)}。{measurements}。"


def _missing_result(
    symbol: str,
    categories: list[str],
    source_hash: str,
    base_pool_version: str,
    signal_date: str,
    gaps: list[str],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": symbol,
        "industry": "",
        "categories": categories,
        "source_hash": source_hash,
        "base_pool_version": base_pool_version,
        "signal_date": signal_date,
        "status": "data_incomplete",
        "actionable_stage": "data_incomplete",
        "reason_text": "数据不完整，未给出交易结论：" + "；".join(gaps),
        "reasons": gaps,
        "manual_exception_eligible": False,
    }


def _eligibility_reason(reason: str) -> bool:
    return reason in {
        "missing_listing_date",
        "listed_less_than_120_days",
        "st",
        "delisting_period",
        "missing_industry",
        "insufficient_20_day_amount_data",
    } or "fetch_failed" in reason


_REASON_LABELS = {
    "market_close_above_ma60": "沪深300仍在MA60下方",
    "market_ma20_above_ma60": "沪深300MA20未高于MA60",
    "market_ma20_slope_positive": "沪深300MA20近5日斜率未转正",
    "market_breadth_ok": "系统池站上MA20比例不足55%",
    "close_above_ma60": "个股收盘仍在MA60下方",
    "ma20_above_ma60": "个股MA20未高于MA60",
    "ma20_slope_positive": "个股MA20近5日斜率未转正",
    "pullback_depth_ok": "距20日高点的回调深度不在0.8至2.5 ATR",
    "pullback_touched_support": "回调尚未触及MA20或有效支撑",
    "daily_recovery_confirmed": "收盘未同时站回MA20并突破前一日高点",
    "close_location_ok": "收盘位置不在当日振幅上方35%区域",
    "volume_ratio_ok": "成交量不在20日均量的0.8至1.8倍",
    "stop_distance_outside_allowed_atr_range": "止损距离不在1至3 ATR",
    "st": "股票带ST标识",
    "delisting_period": "股票处于退市风险范围",
    "listed_less_than_120_days": "上市不足120日",
    "missing_listing_date": "上市日期无法核验",
    "missing_industry": "行业信息无法核验",
    "insufficient_20_day_amount_data": "近20日成交额数据不完整",
}


def _reason_label(reason: str) -> str:
    return _REASON_LABELS.get(reason, reason.replace("_", " "))


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "不可用"


def _scan_id(source_hash: str, pool_version: str, signal_date: str) -> str:
    raw = json.dumps(
        [source_hash, pool_version, signal_date],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
