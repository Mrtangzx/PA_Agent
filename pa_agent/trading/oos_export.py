"""Audit and safely export the append-only production OOS ledger."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.trading.hotspots import (
    ANNOUNCEMENT_WINDOW_DAYS,
    HOTSPOT_RULE_VERSION,
    NEWS_WINDOW_DAYS,
    HotspotService,
)
from pa_agent.trading.oos_bundle import BUNDLE_SCHEMA, validate_oos_bundle
from pa_agent.trading.topdown import (
    TOPDOWN_SCORING_VERSION,
    TOPDOWN_STRATEGY_ID,
    HotspotSnapshot,
)
from pa_agent.trading.universe import (
    CLOUD_AI_AUTHORIZATION_SYMBOLS,
    CLOUD_AI_UNIVERSE_ID,
)
from pa_agent.trading.validation_epoch import ValidationEpochRegistry

INDEX_SYMBOLS = ("000300", "000001", "000852", "399006")
# Bar completeness follows the executable A-share universe. The fixed pool
# definition and hotspot monitoring still retain every requested member,
# including analysis-only 839494.
ALL_MARKET_SYMBOLS = frozenset(
    (*CLOUD_AI_AUTHORIZATION_SYMBOLS, *INDEX_SYMBOLS)
)
SCORING_SLOT_TIMES = (
    time(9, 45), time(10, 0), time(10, 15), time(10, 30),
    time(10, 45), time(11, 0), time(11, 15), time(11, 30),
    time(13, 15), time(13, 30), time(13, 45), time(14, 0),
    time(14, 15), time(14, 30), time(14, 45), time(15, 0),
)
MIN_DAILY_WARMUP_SESSIONS = 65
SENTIMENT_FIELDS = frozenset({
    "advancing_pct",
    "hs300_above_ma20_pct",
    "limit_up_count",
    "limit_down_count",
    "seal_success_pct",
    "blast_board_pct",
    "new_high_count",
    "new_low_count",
    "turnover_vs_ma20",
    "broad_index_positive",
})
THEME_METRIC_FIELDS = frozenset({
    "relative_strength_percentile",
    "advancing_pct",
    "main_net_inflow_pct",
    "turnover_vs_recent",
    "persistence_days",
    "positive_board_share",
})
KIND_PATHS = {
    "historical_constituents": "constituents.jsonl",
    "daily_bars": "daily.jsonl",
    "intraday_15m": "intraday.jsonl",
    "market_sentiment": "sentiment.jsonl",
    "hotspots": "hotspots.jsonl",
}
ARTIFACT_SOURCES = {
    "historical_constituents": (
        "strategy_definition", f"pa-agent://strategy/{CLOUD_AI_UNIVERSE_ID}"
    ),
    "daily_bars": (
        "eastmoney_market", "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    ),
    "intraday_15m": (
        "eastmoney_market", "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    ),
    "market_sentiment": (
        "eastmoney_market", "https://push2.eastmoney.com/api/qt/clist/get"
    ),
    "hotspots": ("eastmoney_board", "https://finance.eastmoney.com/"),
}


class OosCoverageAudit(BaseModel):
    strategy_version: str = TOPDOWN_STRATEGY_ID
    validation_epoch_id: str = ""
    pool_version: str = ""
    member_hash: str = ""
    input_hash: str
    status: str
    export_ready: bool = False
    period_start: str = ""
    period_end: str = ""
    record_counts: dict[str, int] = Field(default_factory=dict)
    session_count: int = 0
    complete_intraday_slots: int = 0
    non_scoreable_setup_records: dict[str, int] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    checked_at: str


class OosObservationExporter:
    """Build a v2 bundle only after strict point-in-time coverage checks."""

    def __init__(
        self, store: Any, *, validation_epochs: ValidationEpochRegistry | None = None
    ) -> None:
        self.store = store
        self.validation_epochs = validation_epochs or ValidationEpochRegistry(store)

    @property
    def epoch(self):
        return self.validation_epochs.require_current()

    @property
    def market_symbols(self) -> frozenset[str]:
        return frozenset((*self.epoch.authorization_symbols, *INDEX_SYMBOLS))

    def audit(self) -> OosCoverageAudit:
        rows = self._rows()
        observation_ids = sorted(
            str(row.get("id") or row.get("payload_hash") or "")
            for values in rows.values()
            for row in values
        )
        gaps: list[str] = []
        counts = {kind: len(values) for kind, values in rows.items()}
        gaps.extend(
            f"observation_kind_missing:{kind}"
            for kind, count in counts.items()
            if count == 0
        )
        self._audit_epoch_bindings(rows, gaps)
        all_points = [
            _point(row["effective_at"])
            for values in rows.values()
            for row in values
        ]
        self._audit_constituents(rows["historical_constituents"], gaps)
        daily_sessions = self._audit_daily(rows["daily_bars"], gaps)
        warmup_cutoff = (
            daily_sessions[MIN_DAILY_WARMUP_SESSIONS - 1]
            if len(daily_sessions) >= MIN_DAILY_WARMUP_SESSIONS
            else date.max
        )
        setup_exclusions = {
            kind: sum(
                _point(row["effective_at"]).date() <= warmup_cutoff
                and not _observation_time_valid(row, max_delay=timedelta(minutes=5))
                for row in rows[kind]
            )
            for kind in ("intraday_15m", "market_sentiment", "hotspots")
        }
        setup_exclusions = {
            kind: count for kind, count in setup_exclusions.items() if count
        }
        complete_slots, evaluation_slots = self._audit_intraday(
            rows["intraday_15m"], daily_sessions, gaps
        )
        self._audit_sentiment(rows["market_sentiment"], evaluation_slots, gaps)
        self._audit_hotspots(rows["hotspots"], evaluation_slots, gaps)
        gaps = list(dict.fromkeys(gaps))
        return OosCoverageAudit(
            validation_epoch_id=self.epoch.epoch_id,
            pool_version=self.epoch.pool_version,
            member_hash=self.epoch.member_hash,
            input_hash=hashlib.sha256("|".join(observation_ids).encode()).hexdigest(),
            status="complete" if not gaps else "data_incomplete",
            export_ready=not gaps,
            period_start=min(all_points).isoformat() if all_points else "",
            period_end=max(all_points).isoformat() if all_points else "",
            record_counts=counts,
            session_count=len(daily_sessions),
            complete_intraday_slots=len(complete_slots),
            non_scoreable_setup_records=setup_exclusions,
            data_gaps=gaps,
            checked_at=datetime.now().astimezone().isoformat(),
        )

    def _audit_epoch_bindings(
        self,
        rows: dict[str, list[dict[str, Any]]],
        gaps: list[str],
    ) -> None:
        """Fail closed when a private-pool ledger contains foreign evidence."""
        epoch = self.epoch
        if not epoch.is_private_pool:
            return
        for kind, values in rows.items():
            for row in values:
                payload = row.get("payload") or {}
                observation_id = str(row.get("id") or row.get("payload_hash") or "")
                if payload.get("validation_epoch_id") != epoch.epoch_id:
                    gaps.append(
                        f"observation_validation_epoch_mismatch:{kind}:{observation_id}"
                    )
                if payload.get("member_hash") != epoch.member_hash:
                    gaps.append(f"observation_member_hash_mismatch:{kind}:{observation_id}")
                if str(payload.get("pool_version") or "") not in epoch.pool_versions:
                    gaps.append(f"observation_pool_version_mismatch:{kind}:{observation_id}")

    def export(self, destination: Path) -> Path:
        audit = self.audit()
        if not audit.export_ready:
            raise ValueError("production_oos_export_blocked:" + ",".join(audit.data_gaps))
        destination = Path(destination).resolve()
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw_rows = self._rows()
        rows, excluded = self._exportable_rows(raw_rows)
        payloads: dict[str, bytes] = {}
        for kind, path in KIND_PATHS.items():
            records = [self._export_record(kind, row) for row in rows[kind]]
            records.sort(key=lambda item: (
                str(item.get("effective_at") or ""), str(item.get("symbol") or "")
            ))
            payloads[path] = (
                "\n".join(
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for item in records
                ) + "\n"
            ).encode("utf-8")
        manifest = self._manifest(audit, rows, payloads, excluded)
        handle, temporary_name = tempfile.mkstemp(
            prefix="pa-oos-", suffix=".zip", dir=str(destination.parent)
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                )
                for path, content in payloads.items():
                    archive.writestr(path, content)
            validation = validate_oos_bundle(temporary)
            if validation.status != "complete":
                raise ValueError(
                    "exported_oos_bundle_invalid:" + ",".join(validation.data_gaps)
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _rows(self) -> dict[str, list[dict[str, Any]]]:
        return {
            kind: self.store.list_oos_observations(
                strategy_version=self.epoch.observation_strategy_version,
                kind=kind,
                limit=1_000_000,
            )
            for kind in KIND_PATHS
        }

    def _exportable_rows(
        self, rows: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
        """Exclude only non-scoreable warm-up observations with no valid availability time.

        The append-only SQLite ledger remains untouched.  Once the 65th complete
        daily session has closed, an invalid ``observed_at`` is never filtered:
        it blocks export so a real scoring-period data gap cannot be hidden.
        """
        daily_sessions = self._audit_daily(
            rows["daily_bars"], []
        )
        if len(daily_sessions) < MIN_DAILY_WARMUP_SESSIONS:
            raise ValueError("production_oos_export_requires_daily_warmup")
        warmup_completed_on = daily_sessions[MIN_DAILY_WARMUP_SESSIONS - 1]
        delays = {
            "intraday_15m": timedelta(minutes=5),
            "market_sentiment": timedelta(minutes=5),
            "hotspots": timedelta(minutes=5),
        }
        result = {kind: list(values) for kind, values in rows.items()}
        excluded: dict[str, int] = {}
        for kind, max_delay in delays.items():
            kept: list[dict[str, Any]] = []
            for row in rows[kind]:
                if _observation_time_valid(row, max_delay=max_delay):
                    kept.append(row)
                    continue
                effective = _point(row["effective_at"])
                if effective.date() <= warmup_completed_on:
                    excluded[kind] = excluded.get(kind, 0) + 1
                    continue
                raise ValueError(
                    "post_warmup_observation_time_invalid:"
                    f"{kind}:{effective.isoformat()}:{row.get('symbol') or ''}"
                )
            result[kind] = kept
        return result, excluded

    def _audit_constituents(self, rows: list[dict[str, Any]], gaps: list[str]) -> None:
        epoch = self.epoch
        frozen = _point(epoch.activated_at)
        valid = [
            row for row in rows
            if _point(row["effective_at"]) > frozen
            and list(row["payload"].get("symbols") or []) == list(epoch.symbols)
            and row["payload"].get("universe_id") == epoch.universe_id
            and row["payload"].get("universe_source_hash") == epoch.member_hash
            and row["payload"].get("validation_epoch_id") == epoch.epoch_id
        ]
        if len(valid) != 1 or len(rows) != 1:
            gaps.append("constituents_fixed_definition_missing_or_ambiguous")

    def _audit_daily(
        self, rows: list[dict[str, Any]], gaps: list[str]
    ) -> list[date]:
        by_session: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_session[_point(row["effective_at"]).date()].append(row)
        complete: list[date] = []
        for session, values in sorted(by_session.items()):
            if (
                _has_exact_market_symbols(values, self.market_symbols)
                and _has_daily_stock_metadata(
                    values, frozenset(self.epoch.authorization_symbols)
                )
                and _market_observation_times_valid(values, max_delay=timedelta(minutes=15))
            ):
                complete.append(session)
            else:
                gaps.append(f"daily_session_incomplete:{session.isoformat()}")
        if len(complete) < MIN_DAILY_WARMUP_SESSIONS:
            gaps.append(
                f"daily_warmup_sessions_insufficient:{len(complete)}/{MIN_DAILY_WARMUP_SESSIONS}"
            )
        return complete

    def _audit_intraday(
        self, rows: list[dict[str, Any]], daily_sessions: list[date], gaps: list[str]
    ) -> tuple[list[datetime], list[datetime]]:
        by_slot: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            point = _point(row["effective_at"])
            by_slot[point].append(row)
        if not by_slot:
            gaps.append("intraday_observations_missing")
            return [], []

        # Keep reporting every complete raw slot so operators can see that the
        # append-only collector is alive during the 65-session indicator
        # warm-up.  Those setup observations are not scoreable OOS frames and
        # therefore must not permanently poison a future export merely because
        # sentiment or hotspot collection started a few minutes later.
        complete = [
            slot
            for slot, values in sorted(by_slot.items())
            if _has_exact_market_symbols(values, self.market_symbols)
        ]
        if not complete:
            gaps.append("complete_intraday_slots_missing")

        if len(daily_sessions) < MIN_DAILY_WARMUP_SESSIONS:
            return complete, []

        # A daily bar captured at 15:00 cannot be used by intraday decisions
        # from that same day.  The first scoreable session is strictly after
        # the 65th complete daily session.
        warmup_completed_on = daily_sessions[MIN_DAILY_WARMUP_SESSIONS - 1]
        by_session: dict[date, list[datetime]] = defaultdict(list)
        for point in by_slot:
            if point.date() > warmup_completed_on:
                by_session[point.date()].append(point)
        if not by_session:
            gaps.append("post_warmup_intraday_slots_missing")
            return complete, []

        # If collection first becomes available part-way through the first
        # post-warm-up day, treat that date as an explicit setup/calibration
        # day.  Every later observed day is audited from 09:45 onward, so a
        # recurring late start or an in-session outage cannot be hidden.
        first_session = min(by_session)
        first_observed = min(by_session[first_session])
        sessions = sorted(by_session)
        if first_observed.time() > SCORING_SLOT_TIMES[0]:
            sessions = [session for session in sessions if session != first_session]
        if not sessions:
            gaps.append("post_warmup_intraday_slots_missing")
            return complete, []

        expected: list[datetime] = []
        for session in sessions:
            observed = by_session[session]
            last = max(observed)
            expected.extend(
                datetime.combine(session, slot, tzinfo=last.tzinfo)
                for slot in SCORING_SLOT_TIMES
                if datetime.combine(session, slot, tzinfo=last.tzinfo) <= last
            )

        evaluation_complete: list[datetime] = []
        for slot in expected:
            values = by_slot.get(slot, [])
            if (
                _has_exact_market_symbols(values, self.market_symbols)
                and _has_intraday_stock_metadata(
                    values, frozenset(self.epoch.authorization_symbols)
                )
                and _market_observation_times_valid(values, max_delay=timedelta(minutes=5))
            ):
                evaluation_complete.append(slot)
            elif _has_exact_market_symbols(values, self.market_symbols):
                gaps.append(f"intraday_metadata_incomplete:{slot.isoformat()}")
            else:
                gaps.append(f"intraday_slot_incomplete:{slot.isoformat()}")
        session_set = set(sessions)
        evaluation_points = {
            point for point in by_slot if point.date() in session_set
        }
        unexpected = sorted(evaluation_points - set(expected))
        if unexpected:
            gaps.append(f"intraday_unexpected_slot_count:{len(unexpected)}")
        if not evaluation_complete:
            gaps.append("post_warmup_intraday_slots_missing")
        return complete, evaluation_complete

    @staticmethod
    def _audit_sentiment(
        rows: list[dict[str, Any]], slots: list[datetime], gaps: list[str]
    ) -> None:
        by_slot: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_slot[_point(row["effective_at"])].append(row)
        for slot in slots:
            values = by_slot.get(slot, [])
            if len(values) != 1:
                gaps.append(f"sentiment_slot_missing_or_ambiguous:{slot.isoformat()}")
                continue
            payload = values[0]["payload"]
            try:
                observed = _point(payload.get("observed_at") or "")
            except (TypeError, ValueError):
                gaps.append(f"sentiment_observed_at_missing:{slot.isoformat()}")
                continue
            if observed < slot or observed - slot > timedelta(minutes=5):
                gaps.append(f"sentiment_observation_delay_invalid:{slot.isoformat()}")
            missing = sorted(
                key for key in SENTIMENT_FIELDS
                if key not in payload or payload[key] is None
            )
            if missing:
                gaps.append(
                    f"sentiment_fields_missing:{slot.isoformat()}:{','.join(missing)}"
                )

    def _audit_hotspots(
        self, rows: list[dict[str, Any]], slots: list[datetime], gaps: list[str]
    ) -> None:
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_symbol[str(row.get("symbol") or row["payload"].get("symbol") or "")].append(row)
        for symbol in self.epoch.symbols:
            values = sorted(by_symbol.get(symbol, []), key=lambda row: _point(row["effective_at"]))
            for slot in slots:
                candidates = [
                    row for row in values
                    if timedelta(0) <= slot - _point(row["effective_at"]) <= timedelta(minutes=15)
                ]
                if not candidates:
                    gaps.append(f"hotspot_slot_missing:{symbol}:{slot.isoformat()}")
                    continue
                row = candidates[-1]
                payload = row["payload"]
                try:
                    observed = _point(payload.get("observed_at"))
                except (TypeError, ValueError):
                    gaps.append(f"hotspot_observed_at_missing:{symbol}:{slot.isoformat()}")
                    continue
                frozen = _point(payload.get("frozen_at") or row["effective_at"])
                if observed < frozen or observed - frozen > timedelta(minutes=5):
                    gaps.append(
                        f"hotspot_observation_delay_invalid:{symbol}:{slot.isoformat()}"
                    )
                if payload.get("rule_version") != HOTSPOT_RULE_VERSION:
                    gaps.append(f"hotspot_rule_version_mismatch:{symbol}:{slot.isoformat()}")
                if payload.get("effective_windows_days") != {
                    "announcement": ANNOUNCEMENT_WINDOW_DAYS, "news": NEWS_WINDOW_DAYS,
                }:
                    gaps.append(f"hotspot_windows_mismatch:{symbol}:{slot.isoformat()}")
                for item in payload.get("items") or []:
                    published_raw = item.get("published_at")
                    if not published_raw:
                        continue
                    try:
                        published = _point_with_default_tz(
                            published_raw, frozen.tzinfo
                        )
                    except (TypeError, ValueError):
                        gaps.append(
                            f"hotspot_item_time_invalid:{symbol}:{slot.isoformat()}"
                        )
                        continue
                    if published > frozen:
                        gaps.append(
                            f"hotspot_future_item:{symbol}:{slot.isoformat()}"
                        )
                if symbol in self.epoch.authorization_symbols:
                    metrics = _theme_metrics(payload)
                    missing = sorted(
                        key for key in THEME_METRIC_FIELDS
                        if metrics is None or key not in metrics or metrics[key] is None
                    )
                    if missing:
                        gaps.append(
                            f"theme_fields_missing:{symbol}:{slot.isoformat()}:{','.join(missing)}"
                        )

    @staticmethod
    def _export_record(kind: str, row: dict[str, Any]) -> dict[str, Any]:
        record = dict(row["payload"])
        record["effective_at"] = row["effective_at"]
        record["source_published_at"] = row["source_published_at"]
        if row.get("symbol"):
            record.setdefault("symbol", row["symbol"])
        if kind == "hotspots":
            metrics = _theme_metrics(record)
            if metrics is not None:
                record["theme_metrics"] = metrics
                # Keep compatibility with the deterministic backtester input.
                record.update(metrics)
        return record

    def _manifest(
        self,
        audit: OosCoverageAudit,
        rows: dict[str, list[dict[str, Any]]],
        payloads: dict[str, bytes],
        excluded: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        epoch = self.epoch
        artifacts = []
        for kind, path in KIND_PATHS.items():
            source_kind, source_url = ARTIFACT_SOURCES[kind]
            if kind == "historical_constituents":
                source_url = f"pa-agent://validation-epoch/{epoch.epoch_id}"
            published = max(
                (_point(row["source_published_at"]) for row in rows[kind]),
                default=_point(epoch.activated_at),
            )
            artifacts.append({
                "path": path,
                "kind": kind,
                "source_kind": source_kind,
                "source_url": source_url,
                "source_published_at": published.isoformat(),
                "sha256": hashlib.sha256(payloads[path]).hexdigest(),
            })
        epoch_fields = (
            {
                "validation_epoch_id": epoch.epoch_id,
                "validation_epoch_schema": epoch.schema_version,
                "pool_version": epoch.origin_pool_version,
                "pool_versions": list(epoch.pool_versions),
                "origin_pool_version": epoch.origin_pool_version,
                "member_hash": epoch.member_hash,
                "symbols": list(epoch.symbols),
                "authorization_symbols": list(epoch.authorization_symbols),
                "activated_at": epoch.activated_at,
            }
            if epoch.is_private_pool
            else {}
        )
        return {
            "schema_version": BUNDLE_SCHEMA,
            "strategy_version": TOPDOWN_STRATEGY_ID,
            "dataset": "out_of_sample",
            "period_start": audit.period_start,
            "period_end": audit.period_end,
            "universe_id": epoch.universe_id,
            "universe_source_hash": epoch.member_hash,
            "strategy_frozen_at": epoch.activated_at,
            **epoch_fields,
            "scoring_version": TOPDOWN_SCORING_VERSION,
            "hotspot_rule_version": HOTSPOT_RULE_VERSION,
            "export_policy": "scoreable_observations_v1",
            "excluded_pre_warmup_records": dict(excluded or {}),
            "artifacts": artifacts,
        }


def _theme_metrics(payload: dict[str, Any]) -> dict[str, Any] | None:
    existing = payload.get("theme_metrics")
    if isinstance(existing, dict) and THEME_METRIC_FIELDS.issubset(existing):
        return dict(existing)
    try:
        return HotspotService.theme_metrics(HotspotSnapshot.model_validate(payload))
    except (TypeError, ValueError):
        return None


def _has_exact_market_symbols(
    values: list[dict[str, Any]], expected: frozenset[str] = ALL_MARKET_SYMBOLS
) -> bool:
    symbols = [
        str(row.get("symbol") or row["payload"].get("symbol") or "")
        for row in values
    ]
    return (
        set(symbols) == expected
        and len(symbols) == len(expected)
    )


def _has_daily_stock_metadata(
    values: list[dict[str, Any]],
    authorization_symbols: frozenset[str] = frozenset(CLOUD_AI_AUTHORIZATION_SYMBOLS),
) -> bool:
    required = (
        "suspended", "limit_locked", "is_st", "delisting",
        "listed_days", "industry",
    )
    return all(
        all(row["payload"].get(key) is not None for key in required)
        for row in values
        if str(row.get("symbol") or row["payload"].get("symbol") or "")
        in authorization_symbols
    )


def _has_intraday_stock_metadata(
    values: list[dict[str, Any]],
    authorization_symbols: frozenset[str] = frozenset(CLOUD_AI_AUTHORIZATION_SYMBOLS),
) -> bool:
    required = ("suspended", "limit_locked")
    return all(
        all(row["payload"].get(key) is not None for key in required)
        for row in values
        if str(row.get("symbol") or row["payload"].get("symbol") or "")
        in authorization_symbols
    )


def _market_observation_times_valid(
    values: list[dict[str, Any]], *, max_delay: timedelta
) -> bool:
    for row in values:
        payload = row["payload"]
        try:
            effective = _point(row.get("effective_at") or payload.get("effective_at"))
            observed = _point(payload.get("observed_at"))
        except (TypeError, ValueError):
            return False
        if observed < effective or observed - effective > max_delay:
            return False
    return True


def _observation_time_valid(
    row: dict[str, Any], *, max_delay: timedelta
) -> bool:
    try:
        effective = _point(row.get("effective_at") or row["payload"].get("effective_at"))
        observed = _point(row["payload"].get("observed_at"))
    except (KeyError, TypeError, ValueError):
        return False
    return effective <= observed <= effective + max_delay


def _point(value: Any) -> datetime:
    if isinstance(value, datetime):
        point = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            raise ValueError("timestamp_required")
        point = datetime.fromisoformat(text)
    if point.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return point


def _point_with_default_tz(value: Any, default_tz: Any) -> datetime:
    """Parse source timestamps that omit a zone using the frozen market zone."""
    if isinstance(value, datetime):
        point = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            raise ValueError("timestamp_required")
        point = datetime.fromisoformat(text)
    if point.tzinfo is None:
        if default_tz is None:
            raise ValueError("timestamp_timezone_required")
        point = point.replace(tzinfo=default_tz)
    return point
