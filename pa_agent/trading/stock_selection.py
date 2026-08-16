"""Deterministic A-share stock selection for the quant workbench.

The selector is intentionally separate from the 4:3:2:1 execution gate.  It
discovers stocks worth monitoring; it never creates a trade plan or authorizes
an order.  Every selected stock must have a current, auditable hotspot snapshot
and must pass the major-negative announcement filter.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pa_agent.data.ashare_common import is_a_share_stock_symbol
from pa_agent.trading.hotspots import HOTSPOT_RULE_VERSION
from pa_agent.trading.topdown import HotspotSnapshot

STOCK_SELECTION_VERSION = "a_share_stock_selection_v1"


class SelectionStrategy(StrEnum):
    HOT_THEME = "hot_theme"
    MAIN_FORCE_THEME = "main_force_theme"
    VOLUME_SUFFOCATION = "volume_suffocation"
    TREND_START = "trend_start"


STRATEGY_LABELS = {
    SelectionStrategy.HOT_THEME.value: "近期热点题材",
    SelectionStrategy.MAIN_FORCE_THEME.value: "主力关注题材",
    SelectionStrategy.VOLUME_SUFFOCATION.value: "量能窒息",
    SelectionStrategy.TREND_START.value: "趋势启动",
}


class StockSelectionSettings(BaseModel):
    """Versioned thresholds for discovery, never for order authorization."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    strategy_version: str = STOCK_SELECTION_VERSION
    refresh_seconds: int = Field(default=900, ge=300, le=86_400)
    seed_per_channel: int = Field(default=18, ge=5, le=100)
    max_scan_symbols: int = Field(default=48, ge=10, le=300)
    hotspot_scan_limit: int = Field(default=24, ge=5, le=100)
    candidate_limit: int = Field(default=30, ge=5, le=100)
    daily_bar_count: int = Field(default=90, ge=65, le=240)
    minimum_daily_bars: int = Field(default=65, ge=60, le=240)
    hotspot_max_age_seconds: int = Field(default=1_800, ge=300, le=86_400)

    hot_theme_min_percentile: float = Field(default=75.0, ge=0, le=100)
    hot_theme_min_persistence_days: int = Field(default=2, ge=1, le=10)
    hot_theme_min_positive_score: float = Field(default=1.0, ge=0, le=3)
    main_force_min_net_inflow_pct: float = Field(default=0.5, ge=0, le=20)

    volume_suffocation_max_ratio: float = Field(default=0.65, gt=0, le=1)
    atr_contraction_max_ratio: float = Field(default=0.80, gt=0, le=1)
    range_contraction_max_ratio: float = Field(default=0.80, gt=0, le=1)
    structure_floor_vs_ma20: float = Field(default=0.97, ge=0.8, le=1.1)

    trend_breakout_lookback: int = Field(default=20, ge=10, le=60)
    trend_breakout_tolerance_pct: float = Field(default=0.5, ge=0, le=3)
    trend_min_volume_ratio: float = Field(default=1.20, ge=0.5, le=5)
    trend_ma20_slope_days: int = Field(default=5, ge=2, le=20)
    require_no_major_negative: bool = True


class SelectionCandidate(BaseModel):
    strategy_version: str = STOCK_SELECTION_VERSION
    symbol: str
    name: str
    status: str
    strategy_tags: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    score: float | None = None
    latest_price: float | None = None
    pct_change: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    hard_blocks: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    source_timestamps: dict[str, str] = Field(default_factory=dict)
    input_hash: str

    @property
    def eligible(self) -> bool:
        return self.status == "eligible" and bool(self.strategy_tags)


class StockSelectionSnapshot(BaseModel):
    strategy_version: str = STOCK_SELECTION_VERSION
    generated_at: str
    market_scope: str = "A股"
    status: str
    scanned_count: int = 0
    candidate_count: int = 0
    candidates: list[SelectionCandidate] = Field(default_factory=list)
    results: list[SelectionCandidate] = Field(default_factory=list)
    strategy_counts: dict[str, int] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    source_timestamps: dict[str, str] = Field(default_factory=dict)
    input_hash: str


class StockSelectionService:
    """Collect a bounded live cross-section and evaluate deterministic rules."""

    def __init__(
        self,
        settings: StockSelectionSettings | None = None,
        *,
        universe_page_loader: Callable[..., tuple[list[dict[str, Any]], int | None]] | None = None,
        daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
        hotspot_loader: Callable[..., HotspotSnapshot] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or StockSelectionSettings()
        if universe_page_loader is None or daily_loader is None or hotspot_loader is None:
            from pa_agent.data.eastmoney_client import (
                fetch_stock_daily_recent,
                fetch_stock_universe_page,
            )
            from pa_agent.trading.hotspots import HotspotService

            universe_page_loader = universe_page_loader or fetch_stock_universe_page
            daily_loader = daily_loader or fetch_stock_daily_recent
            hotspot_loader = hotspot_loader or HotspotService().freeze
        self.universe_page_loader = universe_page_loader
        self.daily_loader = daily_loader
        self.hotspot_loader = hotspot_loader
        self.clock = clock or (lambda: datetime.now().astimezone())

    def evaluate(
        self,
        *,
        symbol: str,
        name: str,
        daily_bars: list[dict[str, Any]],
        hotspot: HotspotSnapshot | dict[str, Any] | None,
        spot: dict[str, Any] | None = None,
        evaluated_at: datetime | None = None,
    ) -> SelectionCandidate:
        settings = self.settings
        now = (evaluated_at or self.clock()).astimezone()
        code = str(symbol or "").strip()[-6:]
        spot = dict(spot or {})
        rows = [_normal_bar(item) for item in daily_bars]
        rows = [item for item in rows if item is not None]
        hotspot_model = (
            hotspot
            if isinstance(hotspot, HotspotSnapshot)
            else HotspotSnapshot.model_validate(hotspot)
            if hotspot
            else None
        )
        gaps: list[str] = []
        blocks: list[str] = []
        if not is_a_share_stock_symbol(code):
            blocks.append("not_a_share")
        if len(rows) < settings.minimum_daily_bars:
            gaps.append(
                f"daily_bars_insufficient:{len(rows)}/{settings.minimum_daily_bars}"
            )
        if hotspot_model is None:
            gaps.append("major_negative_evidence_missing")
        else:
            if hotspot_model.rule_version != HOTSPOT_RULE_VERSION:
                gaps.append("hotspot_rule_version_mismatch")
            gaps.extend(str(item) for item in hotspot_model.data_gaps)
            age = _age_seconds(hotspot_model.frozen_at, now)
            if age is None or age > settings.hotspot_max_age_seconds:
                gaps.append("hotspot_snapshot_stale")
            if settings.require_no_major_negative:
                blocks.extend(str(item) for item in hotspot_model.negative_blocks)

        evidence: dict[str, Any] = {
            "negative_news_check": (
                "passed"
                if hotspot_model is not None and not hotspot_model.negative_blocks and not gaps
                else "blocked_or_incomplete"
            )
        }
        tags: list[str] = []
        if rows and len(rows) >= settings.minimum_daily_bars:
            technical = _technical_metrics(rows, settings)
            evidence.update(technical)
            if technical["volume_suffocation_passed"]:
                tags.append(SelectionStrategy.VOLUME_SUFFOCATION.value)
            if technical["trend_start_passed"]:
                tags.append(SelectionStrategy.TREND_START.value)
        if hotspot_model is not None:
            theme = _theme_metrics(hotspot_model, settings)
            evidence.update(theme)
            if theme["hot_theme_passed"]:
                tags.append(SelectionStrategy.HOT_THEME.value)
            if theme["main_force_theme_passed"]:
                tags.append(SelectionStrategy.MAIN_FORCE_THEME.value)

        gaps = list(dict.fromkeys(gaps))
        blocks = list(dict.fromkeys(blocks))
        if blocks:
            status = "blocked"
        elif gaps:
            status = "data_incomplete"
        elif tags:
            status = "eligible"
        else:
            status = "not_matched"
        if status != "eligible":
            tags = []
        score = None if gaps else _selection_score(tags, evidence)
        latest_price = _number(spot.get("price"))
        if latest_price is None and rows:
            latest_price = float(rows[-1]["close"])
        payload = {
            "settings": settings.model_dump(mode="json"),
            "symbol": code,
            "bars": rows,
            "hotspot": hotspot_model.model_dump(mode="json") if hotspot_model else None,
            "spot": spot,
        }
        return SelectionCandidate(
            strategy_version=settings.strategy_version,
            symbol=code,
            name=str(name or spot.get("name") or code),
            status=status,
            strategy_tags=tags,
            themes=(
                list(dict.fromkeys([*hotspot_model.industries, *hotspot_model.concepts]))
                if hotspot_model else []
            ),
            score=score,
            latest_price=latest_price,
            pct_change=_number(spot.get("pct_chg")),
            evidence=evidence,
            hard_blocks=blocks,
            data_gaps=gaps,
            source_timestamps={
                "daily_bar": str((daily_bars[-1] if daily_bars else {}).get("time") or ""),
                "hotspot": hotspot_model.frozen_at if hotspot_model else "",
                "evaluated_at": now.isoformat(),
            },
            input_hash=_stable_hash(payload),
        )

    def scan(
        self,
        *,
        extra_members: list[dict[str, Any]] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> StockSelectionSnapshot:
        now = self.clock().astimezone()
        settings = self.settings
        if not settings.enabled:
            return self._snapshot(now, [], ["stock_selection_disabled"], scanned_count=0)
        seed_rows: dict[str, dict[str, Any]] = {}
        source_gaps: list[str] = []
        channels = (("f3", True), ("f6", True), ("f10", False))
        for field, descending in channels:
            try:
                rows, _total = self.universe_page_loader(
                    page=1,
                    page_size=settings.seed_per_channel,
                    sort_field=field,
                    sort_desc=descending,
                )
                for row in rows:
                    self._add_seed(seed_rows, row, source=field)
            except Exception as exc:
                source_gaps.append(f"market_seed_{field}:{type(exc).__name__}")
        for member in extra_members or []:
            self._add_seed(seed_rows, member, source="existing_pool")
        seeds = list(seed_rows.values())[: settings.max_scan_symbols]
        if not seeds:
            return self._snapshot(now, [], [*source_gaps, "a_share_seed_empty"], scanned_count=0)

        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            jobs = {
                executor.submit(
                    self.daily_loader,
                    row["code"],
                    n=settings.daily_bar_count,
                    adjust="qfq",
                ): row
                for row in seeds
            }
            for index, future in enumerate(as_completed(jobs), 1):
                row = jobs[future]
                if cancel_check and cancel_check():
                    break
                try:
                    bars_by_symbol[row["code"]] = list(future.result() or [])
                except Exception:
                    bars_by_symbol[row["code"]] = []
                if progress:
                    progress(index, len(jobs), f"读取日线 {row['code']}")

        prioritized = sorted(
            seeds,
            key=lambda row: (
                -int(_pretechnical_match(bars_by_symbol.get(row["code"], []), settings)),
                -float(row.get("pct_chg") or -999),
                -float(row.get("amount") or 0),
                row["code"],
            ),
        )[: settings.hotspot_scan_limit]
        hotspots: dict[str, HotspotSnapshot | None] = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            jobs = {
                executor.submit(self.hotspot_loader, row["code"], frozen_at=now.isoformat()): row
                for row in prioritized
            }
            for index, future in enumerate(as_completed(jobs), 1):
                row = jobs[future]
                if cancel_check and cancel_check():
                    break
                try:
                    value = future.result()
                    hotspots[row["code"]] = (
                        value if isinstance(value, HotspotSnapshot)
                        else HotspotSnapshot.model_validate(value) if value else None
                    )
                except Exception:
                    hotspots[row["code"]] = None
                if progress:
                    progress(index, len(jobs), f"核验公告与题材 {row['code']}")

        results = [
            self.evaluate(
                symbol=row["code"],
                name=str(row.get("name") or row["code"]),
                daily_bars=bars_by_symbol.get(row["code"], []),
                hotspot=hotspots.get(row["code"]),
                spot=row,
                evaluated_at=now,
            )
            for row in prioritized
        ]
        return self._snapshot(now, results, source_gaps, scanned_count=len(seeds))

    @staticmethod
    def _add_seed(target: dict[str, dict[str, Any]], row: dict[str, Any], *, source: str) -> None:
        code = str(row.get("code") or row.get("symbol") or "").strip()[-6:]
        name = str(row.get("name") or code)
        if not is_a_share_stock_symbol(code) or "ST" in name.upper() or "退" in name:
            return
        current = target.setdefault(code, {"code": code, "name": name, "seed_sources": []})
        current.update({key: value for key, value in row.items() if value is not None})
        current.setdefault("seed_sources", []).append(source)

    def _snapshot(
        self,
        now: datetime,
        results: list[SelectionCandidate],
        gaps: list[str],
        *,
        scanned_count: int,
    ) -> StockSelectionSnapshot:
        eligible = sorted(
            (item for item in results if item.eligible),
            key=lambda item: (-(item.score or 0), item.symbol),
        )[: self.settings.candidate_limit]
        counts = {
            value: sum(value in item.strategy_tags for item in eligible)
            for value in STRATEGY_LABELS
        }
        unique_gaps = list(dict.fromkeys(gaps))
        status = "data_incomplete" if unique_gaps and not results else "complete"
        payload = {
            "settings": self.settings.model_dump(mode="json"),
            "generated_at": now.isoformat(),
            "results": [item.model_dump(mode="json") for item in results],
            "source_gaps": unique_gaps,
        }
        return StockSelectionSnapshot(
            strategy_version=self.settings.strategy_version,
            generated_at=now.isoformat(),
            status=status,
            scanned_count=scanned_count,
            candidate_count=len(eligible),
            candidates=eligible,
            results=results,
            strategy_counts=counts,
            data_gaps=unique_gaps,
            source_timestamps={"scan": now.isoformat()},
            input_hash=_stable_hash(payload),
        )


def _normal_bar(value: dict[str, Any]) -> dict[str, float] | None:
    try:
        row = {
            "open": float(value["open"]),
            "high": float(value["high"]),
            "low": float(value["low"]),
            "close": float(value["close"]),
            "volume": float(value.get("volume") or 0),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if row["high"] < row["low"] or row["close"] <= 0:
        return None
    return row


def _technical_metrics(
    rows: list[dict[str, float]], settings: StockSelectionSettings
) -> dict[str, Any]:
    closes = [item["close"] for item in rows]
    volumes = [item["volume"] for item in rows]
    true_ranges = []
    ranges = []
    for index, item in enumerate(rows):
        previous = closes[index - 1] if index else item["close"]
        true_ranges.append(
            max(
                item["high"] - item["low"],
                abs(item["high"] - previous),
                abs(item["low"] - previous),
            )
        )
        ranges.append((item["high"] - item["low"]) / max(previous, 1e-9))
    ma20 = _mean(closes[-20:])
    ma60 = _mean(closes[-60:])
    slope_days = settings.trend_ma20_slope_days
    previous_ma20 = _mean(closes[-20 - slope_days : -slope_days])
    recent_volume = _mean(volumes[-5:])
    base_volume = _mean(volumes[-25:-5])
    volume_ratio = recent_volume / base_volume if base_volume > 0 else 999.0
    atr_ratio = _mean(true_ranges[-5:]) / max(_mean(true_ranges[-25:-5]), 1e-9)
    range_ratio = _mean(ranges[-5:]) / max(_mean(ranges[-25:-5]), 1e-9)
    structure_ok = closes[-1] >= ma20 * settings.structure_floor_vs_ma20 and closes[-1] > ma60
    volume_suffocation = (
        volume_ratio <= settings.volume_suffocation_max_ratio
        and atr_ratio <= settings.atr_contraction_max_ratio
        and range_ratio <= settings.range_contraction_max_ratio
        and structure_ok
    )
    lookback = settings.trend_breakout_lookback
    prior_high = max(item["high"] for item in rows[-lookback - 1 : -1])
    latest_volume_ratio = volumes[-1] / max(_mean(volumes[-21:-1]), 1e-9)
    breakout_floor = prior_high * (1 - settings.trend_breakout_tolerance_pct / 100)
    trend_start = (
        closes[-1] >= breakout_floor
        and closes[-1] > ma20 > ma60
        and ma20 > previous_ma20
        and latest_volume_ratio >= settings.trend_min_volume_ratio
    )
    return {
        "ma20": round(ma20, 4),
        "ma60": round(ma60, 4),
        "ma20_slope_positive": ma20 > previous_ma20,
        "volume_ratio_5_to_previous20": round(volume_ratio, 4),
        "atr_contraction_ratio": round(atr_ratio, 4),
        "range_contraction_ratio": round(range_ratio, 4),
        "price_structure_intact": structure_ok,
        "volume_suffocation_passed": volume_suffocation,
        "prior_high_20": round(prior_high, 4),
        "latest_volume_ratio_20": round(latest_volume_ratio, 4),
        "trend_start_passed": trend_start,
    }


def _theme_metrics(
    hotspot: HotspotSnapshot, settings: StockSelectionSettings
) -> dict[str, Any]:
    flows = list(hotspot.board_strength.get("flows") or [])
    best_strength = max(
        (_number(item.get("relative_strength_percentile")) or 0 for item in flows),
        default=0,
    )
    persistence = max((int(item.get("persistence_days") or 0) for item in flows), default=0)
    main_inflow = max((_number(item.get("main_net_pct")) or 0 for item in flows), default=0)
    market_verified = bool(hotspot.board_strength.get("market_verified"))
    hot_theme = (
        hotspot.positive_score >= settings.hot_theme_min_positive_score
        and best_strength >= settings.hot_theme_min_percentile
        and persistence >= settings.hot_theme_min_persistence_days
        and market_verified
    )
    main_force = main_inflow >= settings.main_force_min_net_inflow_pct and market_verified
    return {
        "hot_theme_passed": hot_theme,
        "theme_relative_strength_percentile": round(best_strength, 4),
        "theme_persistence_days": persistence,
        "verified_positive_hotspot_score": hotspot.positive_score,
        "main_force_theme_passed": main_force,
        "theme_main_net_inflow_pct": round(main_inflow, 4),
        "theme_market_verified": market_verified,
        "negative_blocks": list(hotspot.negative_blocks),
        "hotspot_source_hash": hotspot.source_hash,
        "official_announcements_checked": sum(item.official for item in hotspot.items),
        "verified_hotspot_titles": [
            item.title for item in hotspot.items if item.verified and item.positive
        ][:5],
    }


def _pretechnical_match(
    rows: list[dict[str, Any]], settings: StockSelectionSettings
) -> bool:
    normalized = [_normal_bar(item) for item in rows]
    clean = [item for item in normalized if item is not None]
    if len(clean) < settings.minimum_daily_bars:
        return False
    metrics = _technical_metrics(clean, settings)
    return bool(metrics["volume_suffocation_passed"] or metrics["trend_start_passed"])


def _selection_score(tags: list[str], evidence: dict[str, Any]) -> float:
    if not tags:
        return 0.0
    score = len(tags) * 20.0
    score += min(10.0, float(evidence.get("theme_relative_strength_percentile") or 0) / 10)
    score += min(10.0, max(0.0, float(evidence.get("theme_main_net_inflow_pct") or 0)) * 2)
    return round(min(100.0, score), 2)


def _age_seconds(value: str, now: datetime) -> float | None:
    try:
        point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if point.tzinfo is None:
        point = point.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - point.astimezone(now.tzinfo)).total_seconds())


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", "-", "--") else None
    except (TypeError, ValueError):
        return None


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
