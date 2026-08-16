"""Deterministic hotspot collection from East Money public data.

AI may later summarise these snapshots, but it never classifies a hard block or
changes a score.  Official announcement titles are mapped to stable risk codes
and positive items require both theme relevance and market verification.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from pa_agent.trading.topdown import HotspotItem, HotspotSnapshot

MAJOR_NEGATIVE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "regulatory_investigation",
        ("立案告知书", "立案调查", "调查通知书", "行政处罚", "市场禁入"),
    ),
    (
        "fraud_or_audit_risk",
        (
            "财务造假",
            "非标准审计意见",
            "保留意见",
            "否定意见",
            "无法表示意见",
            "重大会计差错",
        ),
    ),
    (
        "delisting_or_st_risk",
        ("退市风险", "终止上市", "实施退市", "被实施ST", "被实施*ST"),
    ),
    (
        "litigation_or_default",
        ("重大诉讼", "债务违约", "破产重整", "被申请破产"),
    ),
    (
        "suspension_or_operation_risk",
        ("重大停产", "全面停产", "临时停牌", "异常交易核查"),
    ),
)

_MAJOR_SHAREHOLDER_SUBJECTS = (
    "控股股东",
    "实际控制人",
    "第一大股东",
    "重要股东",
    "持股5%以上股东",
    "持股百分之五以上股东",
)
_SHARE_REDUCTION_ACTIONS = (
    "减持计划",
    "减持股份计划",
    "拟减持",
    "减持股份预披露",
    "减持股份进展",
    "被动减持",
    "强制减持",
)
_SHARE_REDUCTION_RESOLUTIONS = (
    "减持计划实施完毕",
    "减持计划已实施完毕",
    "减持计划完成",
    "减持完成",
    "终止减持",
    "不再减持",
)

HOTSPOT_RULE_VERSION = "hotspot_time_window_v2"
ANNOUNCEMENT_WINDOW_DAYS = 30
NEWS_WINDOW_DAYS = 3


class HotspotService:
    """Collect and freeze one stock's current hotspot snapshot."""

    def freeze(self, symbol: str, *, frozen_at: str | None = None) -> HotspotSnapshot:
        from pa_agent.data.eastmoney_extended import (
            fetch_operations_required,
            fetch_stock_announcements,
            fetch_stock_board_money_flows,
            fetch_stock_board_tags,
            fetch_stock_news,
        )

        captured = _parse_point(frozen_at) if frozen_at else datetime.now().astimezone()
        now = captured.isoformat()
        tags = fetch_stock_board_tags(symbol) or {}
        operations = fetch_operations_required(symbol) or {}
        boards = list(operations.get("ssbk") or [])
        flows = fetch_stock_board_money_flows(symbol, boards, limit=5)
        announcements = fetch_stock_announcements(symbol, page_size=20)
        news = fetch_stock_news(symbol, page_size=20)
        industries = _as_names(tags.get("industry"))
        concepts = _as_names(tags.get("concepts"))
        if not industries and tags.get("region"):
            industries = _as_names(tags.get("region"))
        theme_names = set(industries + concepts)
        positive_board = any(
            _float(row.get("pct_chg")) > 0 and _float(row.get("main_net_pct")) > 0
            for row in flows
        )
        items: list[HotspotItem] = []
        seen: set[str] = set()
        data_gaps: list[str] = []
        # A listed A-share normally has recent exchange/company announcements.
        # An empty payload cannot prove the absence of major negative events,
        # so every trading/discovery consumer must fail closed.
        if not announcements:
            data_gaps.append("announcement_snapshot_empty")
        for raw in announcements:
            title = _first(raw, "title", "NOTICE_TITLE", "notice_title", "art_code")
            if not title or title in seen:
                continue
            seen.add(title)
            risk_code = classify_major_negative(title)
            published_at = _first(raw, "notice_date", "NOTICE_DATE", "display_time")
            published_at = _normalized_publication_time(published_at, captured)
            time_valid, time_reason = publication_window_status(
                published_at,
                frozen_at=captured,
                # A confirmed major risk does not become harmless merely
                # because the announcement is old. Resolution evidence is a
                # separate requirement; only ordinary announcement recency
                # uses the display window.
                valid_days=None if risk_code else ANNOUNCEMENT_WINDOW_DAYS,
            )
            unverifiable_reasons = {
                "published_at_missing",
                "published_at_invalid",
                "published_at_in_future",
            }
            if risk_code and time_reason in unverifiable_reasons:
                data_gaps.append(f"major_negative_time_unverified:{risk_code}")
            items.append(HotspotItem(
                item_id=_item_id("announcement", title, raw),
                title=title,
                source="交易所/公司公告",
                source_url=_first(raw, "url", "URL", "notice_url"),
                published_at=published_at,
                official=True,
                verified=time_valid,
                positive=False,
                major_negative=bool(risk_code),
                related_themes=_matching_themes(title, theme_names),
                risk_code=risk_code,
                time_valid=time_valid,
                time_validation_reason=time_reason,
            ))
        for raw in news:
            title = _first(raw, "title", "show_title", "content")
            if not title or title in seen:
                continue
            seen.add(title)
            related = _matching_themes(title, theme_names)
            published_at = _first(raw, "show_time", "publish_time", "date")
            published_at = _normalized_publication_time(published_at, captured)
            time_valid, time_reason = publication_window_status(
                published_at,
                frozen_at=captured,
                valid_days=NEWS_WINDOW_DAYS,
            )
            verified = bool(related and positive_board and time_valid)
            items.append(HotspotItem(
                item_id=_item_id("news", title, raw),
                title=title,
                source=_first(raw, "mediaName", "source", "media_name") or "东方财富财经",
                source_url=_first(raw, "url", "article_url", "unique_url"),
                published_at=published_at,
                official=False,
                verified=verified,
                positive=verified,
                related_themes=related,
                time_valid=time_valid,
                time_validation_reason=time_reason,
            ))
        verified_positive = sum(1 for item in items if item.positive and item.verified)
        negative_blocks = [
            f"major_negative_{item.risk_code}" for item in items
            if item.official and item.verified and item.time_valid and item.major_negative
        ]
        negative_blocks.extend(
            item for item in data_gaps if item.startswith("major_negative_time_unverified:")
        )
        snapshot = HotspotSnapshot(
            symbol=symbol,
            captured_at=now,
            frozen_at=now,
            industries=industries,
            concepts=concepts,
            items=items,
            board_strength={"flows": flows, "market_verified": positive_board},
            positive_score=min(3.0, float(verified_positive)),
            negative_blocks=list(dict.fromkeys(negative_blocks)),
            data_gaps=list(dict.fromkeys(data_gaps)),
            rule_version=HOTSPOT_RULE_VERSION,
            effective_windows_days={
                "announcement": ANNOUNCEMENT_WINDOW_DAYS,
                "news": NEWS_WINDOW_DAYS,
            },
        )
        return snapshot.with_source_hash()

    @staticmethod
    def theme_metrics(snapshot: HotspotSnapshot) -> dict[str, float | int] | None:
        """Derive only metrics supported by the frozen board-flow payload.

        A theme score needs five independent inputs.  Current board snapshots
        provide price strength and main-flow percentage, but not board breadth,
        historical turnover comparison, or multi-day persistence.  Returning
        ``None`` here is intentional: it prevents those missing dimensions from
        being silently invented and keeps live authorization closed.
        """
        flows = list(snapshot.board_strength.get("flows") or [])
        if snapshot.data_gaps or not flows:
            return None
        required = {
            "advancing_pct",
            "turnover_vs_recent",
            "persistence_days",
            "relative_strength_percentile",
        }
        complete = [item for item in flows if required.issubset(item)]
        if not complete:
            return None
        best = max(
            complete,
            key=lambda item: float(item.get("relative_strength_percentile") or 0),
        )
        positives = sum(float(item.get("pct_chg") or 0) > 0 for item in flows)
        return {
            "relative_strength_percentile": float(best["relative_strength_percentile"]),
            "advancing_pct": float(best["advancing_pct"]),
            "main_net_inflow_pct": float(best.get("main_net_pct") or 0),
            "turnover_vs_recent": float(best["turnover_vs_recent"]),
            "persistence_days": int(best["persistence_days"]),
            "positive_board_share": positives / len(flows) * 100,
        }


def classify_major_negative(title: str) -> str:
    compact = "".join(str(title).split())
    lowered = compact.lower()
    if any(value in compact for value in ("不予立案", "撤销立案")):
        return ""
    if (
        any(subject.lower() in lowered for subject in _MAJOR_SHAREHOLDER_SUBJECTS)
        and any(action.lower() in lowered for action in _SHARE_REDUCTION_ACTIONS)
        and not any(
            resolution.lower() in lowered for resolution in _SHARE_REDUCTION_RESOLUTIONS
        )
    ):
        return "major_shareholder_reduction"
    if (
        "业绩预告" in compact
        and any(value in compact for value in ("下修", "亏损", "下降", "由盈转亏"))
    ) or any(value in compact for value in ("预计亏损", "预亏", "首亏")):
        return "material_performance_deterioration"
    for code, keywords in MAJOR_NEGATIVE_KEYWORDS:
        if (
            code == "fraud_or_audit_risk"
            and "标准无保留意见" in compact
            and "非标准" not in compact
        ):
            continue
        if any(keyword.lower() in lowered for keyword in keywords):
            return code
    return ""


def publication_window_status(
    published_at: str,
    *,
    frozen_at: datetime,
    valid_days: int | None,
) -> tuple[bool, str]:
    """Validate publication time without substituting the collection time."""
    if not str(published_at or "").strip():
        return False, "published_at_missing"
    try:
        published = _parse_point(published_at, default_tz=frozen_at.tzinfo)
    except (TypeError, ValueError):
        return False, "published_at_invalid"
    point = frozen_at
    if point.tzinfo is None:
        point = point.replace(tzinfo=published.tzinfo)
    published = published.astimezone(point.tzinfo)
    if published > point:
        return False, "published_at_in_future"
    if valid_days is not None and point - published > timedelta(days=valid_days):
        return False, "published_at_outside_window"
    return True, "within_effective_window"


def _parse_point(value: str | datetime | None, *, default_tz=None) -> datetime:
    if isinstance(value, datetime):
        point = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        point = datetime.fromisoformat(text)
    if point.tzinfo is None:
        point = point.replace(tzinfo=default_tz or datetime.now().astimezone().tzinfo)
    return point


def _normalized_publication_time(
    value: str | datetime | None,
    frozen_at: datetime,
) -> str:
    """Persist publication time as an explicit point in the frozen market zone."""
    try:
        point = _parse_point(value, default_tz=frozen_at.tzinfo)
    except (TypeError, ValueError):
        return str(value or "")
    target_tz = frozen_at.tzinfo or point.tzinfo
    return point.astimezone(target_tz).isoformat()


def _matching_themes(title: str, themes: set[str]) -> list[str]:
    compact = str(title).lower()
    return sorted(theme for theme in themes if theme and theme.lower() in compact)


def _as_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
            elif isinstance(item, dict):
                name = _first(item, "BOARD_NAME", "name", "industry")
                if name:
                    result.append(name)
        return result
    return []


def _first(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        raw = value.get(key)
        if raw not in (None, ""):
            return str(raw).strip()
    return ""


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _item_id(source: str, title: str, raw: dict[str, Any]) -> str:
    value = json.dumps([source, title, raw], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
