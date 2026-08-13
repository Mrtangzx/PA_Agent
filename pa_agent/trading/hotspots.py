"""Deterministic hotspot collection from East Money public data.

AI may later summarise these snapshots, but it never classifies a hard block or
changes a score.  Official announcement titles are mapped to stable risk codes
and positive items require both theme relevance and market verification.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pa_agent.trading.topdown import HotspotItem, HotspotSnapshot

MAJOR_NEGATIVE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("regulatory_investigation", ("立案", "调查通知书", "监管措施", "行政处罚")),
    ("fraud_or_audit_risk", ("财务造假", "审计意见", "无法表示意见", "会计差错")),
    ("delisting_or_st_risk", ("退市风险", "终止上市", "实施退市", "被实施ST", "被实施*ST")),
    ("material_performance_deterioration", ("预亏", "首亏", "大幅下降", "业绩修正")),
    ("major_shareholder_reduction", ("减持股份", "减持计划", "被动减持")),
    ("litigation_or_default", ("重大诉讼", "债务违约", "破产重整", "被申请破产")),
    ("suspension_or_operation_risk", ("停产", "临时停牌", "异常交易核查")),
)


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

        now = frozen_at or datetime.now().astimezone().isoformat()
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
        for raw in announcements:
            title = _first(raw, "title", "NOTICE_TITLE", "notice_title", "art_code")
            if not title or title in seen:
                continue
            seen.add(title)
            risk_code = classify_major_negative(title)
            items.append(HotspotItem(
                item_id=_item_id("announcement", title, raw),
                title=title,
                source="交易所/公司公告",
                source_url=_first(raw, "url", "URL", "notice_url"),
                published_at=_first(raw, "notice_date", "NOTICE_DATE", "display_time") or now,
                official=True,
                verified=True,
                positive=False,
                major_negative=bool(risk_code),
                related_themes=_matching_themes(title, theme_names),
                risk_code=risk_code,
            ))
        for raw in news:
            title = _first(raw, "title", "show_title", "content")
            if not title or title in seen:
                continue
            seen.add(title)
            related = _matching_themes(title, theme_names)
            verified = bool(related and positive_board)
            items.append(HotspotItem(
                item_id=_item_id("news", title, raw),
                title=title,
                source=_first(raw, "mediaName", "source", "media_name") or "东方财富财经",
                source_url=_first(raw, "url", "article_url", "unique_url"),
                published_at=_first(raw, "show_time", "publish_time", "date") or now,
                official=False,
                verified=verified,
                positive=verified,
                related_themes=related,
            ))
        verified_positive = sum(1 for item in items if item.positive and item.verified)
        negative_blocks = [
            f"major_negative_{item.risk_code}" for item in items
            if item.official and item.verified and item.major_negative
        ]
        snapshot = HotspotSnapshot(
            symbol=symbol,
            captured_at=datetime.now().astimezone().isoformat(),
            frozen_at=now,
            industries=industries,
            concepts=concepts,
            items=items,
            board_strength={"flows": flows, "market_verified": positive_board},
            positive_score=min(3.0, float(verified_positive)),
            negative_blocks=list(dict.fromkeys(negative_blocks)),
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
        if not flows:
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
    for code, keywords in MAJOR_NEGATIVE_KEYWORDS:
        if any(keyword.lower() in compact.lower() for keyword in keywords):
            return code
    return ""


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
