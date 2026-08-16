"""Strict, no-future-data import boundary for out-of-sample validation bundles."""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

from pa_agent.trading.hotspots import (
    ANNOUNCEMENT_WINDOW_DAYS,
    HOTSPOT_RULE_VERSION,
    NEWS_WINDOW_DAYS,
)
from pa_agent.trading.topdown import (
    LEGACY_TOPDOWN_STRATEGY_ID,
    TOPDOWN_SCORING_VERSION,
    TOPDOWN_STRATEGY_ID,
)
from pa_agent.trading.universe import (
    CLOUD_AI_STRATEGY_FROZEN_AT,
    CLOUD_AI_SYMBOLS,
    CLOUD_AI_UNIVERSE_ID,
    cloud_ai_definition_hash,
)
from pa_agent.trading.validation_epoch import VALIDATION_EPOCH_SCHEMA

BUNDLE_SCHEMA = "pa_oos_bundle_v2"
LEGACY_BUNDLE_SCHEMA = "pa_oos_bundle_v1"
REQUIRED_KINDS = {
    "historical_constituents",
    "daily_bars",
    "intraday_15m",
    "market_sentiment",
    "hotspots",
}
TRUSTED_SOURCE_KINDS = {
    "historical_constituents": {"csindex_official", "strategy_definition"},
    "daily_bars": {"eastmoney_market"},
    "intraday_15m": {"eastmoney_market"},
    "market_sentiment": {"eastmoney_market"},
    "hotspots": {
        "exchange_announcement",
        "company_announcement",
        "eastmoney_news",
        "eastmoney_board",
        "eastmoney_heat",
    },
}
TRUSTED_SOURCE_HOSTS = {
    "csindex_official": ("csindex.com.cn",),
    "strategy_definition": (),
    "eastmoney_market": ("eastmoney.com",),
    "exchange_announcement": ("sse.com.cn", "szse.cn", "bse.cn"),
    "company_announcement": ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn"),
    "eastmoney_news": ("eastmoney.com",),
    "eastmoney_board": ("eastmoney.com",),
    "eastmoney_heat": ("eastmoney.com",),
}
MAX_ENTRY_COUNT = 1_000
MAX_ENTRY_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024


class BundleArtifact(BaseModel):
    path: str
    kind: Literal[
        "historical_constituents",
        "daily_bars",
        "intraday_15m",
        "market_sentiment",
        "hotspots",
    ]
    source_kind: Literal[
        "csindex_official",
        "eastmoney_market",
        "exchange_announcement",
        "company_announcement",
        "eastmoney_news",
        "eastmoney_board",
        "eastmoney_heat",
        "strategy_definition",
    ]
    source_url: str
    source_published_at: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OosBundleManifest(BaseModel):
    schema_version: Literal["pa_oos_bundle_v1", "pa_oos_bundle_v2"]
    strategy_version: Literal[
        "hs300_topdown_4321_intraday_v1",
        "cloud_ai_topdown_4321_intraday_v1",
    ]
    dataset: Literal["out_of_sample"]
    period_start: str
    period_end: str
    universe_id: str = ""
    universe_source_hash: str = ""
    strategy_frozen_at: str = ""
    scoring_version: str = ""
    hotspot_rule_version: str = ""
    export_policy: str = ""
    excluded_pre_warmup_records: dict[str, int] = Field(default_factory=dict)
    validation_epoch_id: str = ""
    validation_epoch_schema: str = ""
    pool_version: str = ""
    pool_versions: list[str] = Field(default_factory=list)
    origin_pool_version: str = ""
    member_hash: str = ""
    symbols: list[str] = Field(default_factory=list)
    authorization_symbols: list[str] = Field(default_factory=list)
    activated_at: str = ""
    artifacts: list[BundleArtifact] = Field(min_length=5)

    @model_validator(mode="after")
    def validate_strategy_schema(self) -> OosBundleManifest:
        if self.schema_version == LEGACY_BUNDLE_SCHEMA:
            if self.strategy_version != LEGACY_TOPDOWN_STRATEGY_ID:
                raise ValueError("legacy_bundle_requires_legacy_hs300_strategy")
            return self
        if self.strategy_version != TOPDOWN_STRATEGY_ID:
            raise ValueError("current_bundle_requires_current_cloud_ai_strategy")
        if self.validation_epoch_id:
            if self.validation_epoch_schema not in {"", VALIDATION_EPOCH_SCHEMA}:
                raise ValueError("current_bundle_validation_epoch_schema_mismatch")
            if not self.pool_version:
                raise ValueError("current_bundle_pool_version_missing")
            if not self.pool_versions:
                self.pool_versions = [self.pool_version]
            if self.pool_version not in self.pool_versions:
                raise ValueError("current_bundle_pool_version_not_declared")
            if self.origin_pool_version and self.origin_pool_version != self.pool_versions[0]:
                raise ValueError("current_bundle_origin_pool_version_mismatch")
            if len(self.member_hash) != 64 or any(
                value not in "0123456789abcdef" for value in self.member_hash
            ):
                raise ValueError("current_bundle_member_hash_invalid")
            if self.universe_source_hash != self.member_hash:
                raise ValueError("current_bundle_epoch_hash_mismatch")
            if not self.symbols or len(self.symbols) != len(set(self.symbols)):
                raise ValueError("current_bundle_epoch_symbols_invalid")
            if not set(self.authorization_symbols).issubset(self.symbols):
                raise ValueError("current_bundle_epoch_authorization_symbols_invalid")
            if self.strategy_frozen_at != self.activated_at:
                raise ValueError("current_bundle_epoch_activation_mismatch")
        elif self.universe_id != CLOUD_AI_UNIVERSE_ID:
            raise ValueError("current_bundle_universe_id_mismatch")
        if not self.validation_epoch_id and self.universe_source_hash != cloud_ai_definition_hash():
            raise ValueError("current_bundle_universe_hash_mismatch")
        if not self.validation_epoch_id and self.strategy_frozen_at != CLOUD_AI_STRATEGY_FROZEN_AT:
            raise ValueError("current_bundle_strategy_freeze_mismatch")
        if self.scoring_version != TOPDOWN_SCORING_VERSION:
            raise ValueError("current_bundle_scoring_version_mismatch")
        if self.hotspot_rule_version != HOTSPOT_RULE_VERSION:
            raise ValueError("current_bundle_hotspot_rule_version_mismatch")
        if self.export_policy and self.export_policy != "scoreable_observations_v1":
            raise ValueError("current_bundle_export_policy_mismatch")
        start = _time(self.period_start, "period_start")
        frozen = _time(self.strategy_frozen_at, "strategy_frozen_at")
        if start <= frozen:
            raise ValueError("current_bundle_period_must_start_after_strategy_freeze")
        return self


class OosBundleValidationReport(BaseModel):
    strategy_version: str = ""
    schema_version: str = ""
    dataset: str = "out_of_sample_data_bundle"
    status: str
    input_hash: str
    promotion_eligible: bool = False
    bundle_path: str
    period_start: str = ""
    period_end: str = ""
    universe_id: str = ""
    universe_source_hash: str = ""
    scoring_version: str = ""
    hotspot_rule_version: str = ""
    validation_epoch_id: str = ""
    pool_version: str = ""
    member_hash: str = ""
    artifact_counts: dict[str, int] = Field(default_factory=dict)
    record_counts: dict[str, int] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    checked_at: str


def validate_oos_bundle(path: Path) -> OosBundleValidationReport:
    bundle_path = Path(path)
    checked_at = datetime.now().astimezone().isoformat()
    bundle_hash = _file_hash(bundle_path) if bundle_path.is_file() else ""
    gaps: list[str] = []
    artifact_counts: dict[str, int] = {}
    record_counts: dict[str, int] = {}
    period_start = ""
    period_end = ""
    strategy_version = ""
    schema_version = ""
    universe_id = ""
    universe_source_hash = ""
    scoring_version = ""
    hotspot_rule_version = ""
    validation_epoch_id = ""
    pool_version = ""
    member_hash = ""
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            infos = archive.infolist()
            _validate_archive_entries(infos)
            names = {info.filename for info in infos if not info.is_dir()}
            if "manifest.json" not in names:
                raise ValueError("bundle_manifest_missing")
            manifest_bytes = archive.read("manifest.json")
            declared = json.loads(manifest_bytes)
            if isinstance(declared, dict):
                strategy_version = str(declared.get("strategy_version") or "")
                schema_version = str(declared.get("schema_version") or "")
                universe_id = str(declared.get("universe_id") or "")
                universe_source_hash = str(
                    declared.get("universe_source_hash") or ""
                )
                scoring_version = str(declared.get("scoring_version") or "")
                hotspot_rule_version = str(
                    declared.get("hotspot_rule_version") or ""
                )
                validation_epoch_id = str(declared.get("validation_epoch_id") or "")
                pool_version = str(declared.get("pool_version") or "")
                member_hash = str(declared.get("member_hash") or "")
            manifest = OosBundleManifest.model_validate_json(manifest_bytes)
            strategy_version = manifest.strategy_version
            schema_version = manifest.schema_version
            universe_id = manifest.universe_id
            universe_source_hash = manifest.universe_source_hash
            scoring_version = manifest.scoring_version
            hotspot_rule_version = manifest.hotspot_rule_version
            validation_epoch_id = manifest.validation_epoch_id
            pool_version = manifest.pool_version
            member_hash = manifest.member_hash
            period_start = manifest.period_start
            period_end = manifest.period_end
            start = _time(manifest.period_start, "period_start")
            end = _time(manifest.period_end, "period_end")
            if end <= start:
                raise ValueError("bundle_period_invalid")
            declared_paths: set[str] = set()
            for artifact in manifest.artifacts:
                _validate_artifact_path(artifact.path)
                if artifact.path in declared_paths:
                    raise ValueError(f"duplicate_artifact_path:{artifact.path}")
                declared_paths.add(artifact.path)
                if artifact.path not in names:
                    gaps.append(f"artifact_missing:{artifact.path}")
                    continue
                if artifact.source_kind not in TRUSTED_SOURCE_KINDS[artifact.kind]:
                    gaps.append(f"artifact_source_kind_mismatch:{artifact.path}")
                if artifact.kind == "historical_constituents":
                    expected_source = (
                        "strategy_definition"
                        if manifest.strategy_version == TOPDOWN_STRATEGY_ID
                        else "csindex_official"
                    )
                    if artifact.source_kind != expected_source:
                        gaps.append(
                            f"universe_source_kind_mismatch:{artifact.path}:"
                            f"expected_{expected_source}"
                        )
                if not _trusted_source_url(artifact.source_url, artifact.source_kind):
                    gaps.append(f"artifact_source_not_trusted:{artifact.path}")
                content = archive.read(artifact.path)
                digest = hashlib.sha256(content).hexdigest()
                if digest != artifact.sha256:
                    gaps.append(f"artifact_hash_mismatch:{artifact.path}")
                    continue
                _time(
                    artifact.source_published_at,
                    f"artifact_source_published_at:{artifact.path}",
                )
                records = _jsonl_records(content, artifact.path)
                artifact_counts[artifact.kind] = artifact_counts.get(artifact.kind, 0) + 1
                record_counts[artifact.kind] = record_counts.get(artifact.kind, 0) + len(records)
                for index, record in enumerate(records, 1):
                    gaps.extend(_validate_record(
                        artifact.kind,
                        record,
                        manifest=manifest,
                        index=index,
                        path=artifact.path,
                        period_start=start,
                        period_end=end,
                    ))
            undeclared = names - declared_paths - {"manifest.json"}
            if undeclared:
                gaps.extend(f"undeclared_archive_entry:{name}" for name in sorted(undeclared))
            for kind in sorted(REQUIRED_KINDS):
                if artifact_counts.get(kind, 0) == 0:
                    gaps.append(f"required_artifact_kind_missing:{kind}")
                elif record_counts.get(kind, 0) == 0:
                    gaps.append(f"required_artifact_records_empty:{kind}")
    except (OSError, zipfile.BadZipFile, ValueError, json.JSONDecodeError) as exc:
        gaps.append(f"bundle_invalid:{type(exc).__name__}:{exc}")

    gaps = list(dict.fromkeys(gaps))
    return OosBundleValidationReport(
        strategy_version=strategy_version,
        schema_version=schema_version,
        status="complete" if not gaps else "data_incomplete",
        input_hash=bundle_hash or hashlib.sha256(str(bundle_path).encode()).hexdigest(),
        bundle_path=str(bundle_path.resolve()),
        period_start=period_start,
        period_end=period_end,
        universe_id=universe_id,
        universe_source_hash=universe_source_hash,
        scoring_version=scoring_version,
        hotspot_rule_version=hotspot_rule_version,
        validation_epoch_id=validation_epoch_id,
        pool_version=pool_version,
        member_hash=member_hash,
        artifact_counts=artifact_counts,
        record_counts=record_counts,
        data_gaps=gaps,
        checked_at=checked_at,
    )


def _validate_record(
    kind: str,
    record: dict[str, Any],
    *,
    manifest: OosBundleManifest,
    index: int,
    path: str,
    period_start: datetime,
    period_end: datetime,
) -> list[str]:
    strategy_version = manifest.strategy_version
    prefix = f"{path}:{index}"
    gaps: list[str] = []
    if manifest.validation_epoch_id:
        if record.get("validation_epoch_id") != manifest.validation_epoch_id:
            gaps.append(f"record_validation_epoch_mismatch:{prefix}")
        if record.get("member_hash") != manifest.member_hash:
            gaps.append(f"record_member_hash_mismatch:{prefix}")
        if str(record.get("pool_version") or "") not in manifest.pool_versions:
            gaps.append(f"record_pool_version_mismatch:{prefix}")
    effective_raw = record.get("effective_at")
    published_raw = record.get("source_published_at")
    if not effective_raw:
        return [f"record_effective_at_missing:{prefix}"]
    if not published_raw:
        return [f"record_source_published_at_missing:{prefix}"]
    try:
        effective = _time(str(effective_raw), f"effective_at:{prefix}")
        published = _time(str(published_raw), f"source_published_at:{prefix}")
    except ValueError as exc:
        return [f"record_time_invalid:{prefix}:{exc}"]
    if not period_start <= effective <= period_end:
        gaps.append(f"record_outside_bundle_period:{prefix}")
    if published > effective:
        gaps.append(f"record_future_source:{prefix}")
    if kind in {"daily_bars", "intraday_15m"} and strategy_version == TOPDOWN_STRATEGY_ID:
        observed_raw = record.get("observed_at")
        if not observed_raw:
            gaps.append(f"market_observed_at_missing:{prefix}")
        else:
            try:
                observed = _time(str(observed_raw), f"observed_at:{prefix}")
                max_delay = timedelta(
                    minutes=15 if kind == "daily_bars" else 5
                )
                if observed < effective or observed - effective > max_delay:
                    gaps.append(f"market_observation_delay_invalid:{prefix}")
            except ValueError as exc:
                gaps.append(f"market_observed_at_invalid:{prefix}:{exc}")
    if kind == "market_sentiment" and strategy_version == TOPDOWN_STRATEGY_ID:
        observed_raw = record.get("observed_at")
        if not observed_raw:
            gaps.append(f"sentiment_observed_at_missing:{prefix}")
        else:
            try:
                observed = _time(str(observed_raw), f"observed_at:{prefix}")
                if observed < effective or observed - effective > timedelta(minutes=5):
                    gaps.append(f"sentiment_observation_delay_invalid:{prefix}")
            except ValueError as exc:
                gaps.append(f"sentiment_observed_at_invalid:{prefix}:{exc}")
    if kind == "historical_constituents":
        symbols = [str(value) for value in record.get("symbols") or []]
        if strategy_version == TOPDOWN_STRATEGY_ID:
            expected_symbols = manifest.symbols or list(CLOUD_AI_SYMBOLS)
            if set(symbols) != set(expected_symbols) or len(symbols) != len(
                expected_symbols
            ):
                gaps.append(f"cloud_ai_universe_definition_mismatch:{prefix}")
            frozen = _time(manifest.strategy_frozen_at, "strategy_frozen_at")
            if effective <= frozen:
                gaps.append(f"cloud_ai_universe_record_not_post_freeze:{prefix}")
        elif len(symbols) != 300 or len(set(symbols)) != 300:
            gaps.append(f"historical_constituents_not_300_unique:{prefix}")
        if any(not value.isdigit() or len(value) != 6 for value in symbols):
            gaps.append(f"historical_constituent_symbol_invalid:{prefix}")
    if kind == "hotspots":
        if strategy_version == TOPDOWN_STRATEGY_ID:
            observed_raw = record.get("observed_at")
            if not observed_raw:
                gaps.append(f"hotspot_observed_at_missing:{prefix}")
            else:
                try:
                    observed = _time(str(observed_raw), f"observed_at:{prefix}")
                    if observed < effective or observed - effective > timedelta(minutes=5):
                        gaps.append(f"hotspot_observation_delay_invalid:{prefix}")
                except ValueError as exc:
                    gaps.append(f"hotspot_observed_at_invalid:{prefix}:{exc}")
            if record.get("rule_version") != HOTSPOT_RULE_VERSION:
                gaps.append(f"hotspot_rule_version_mismatch:{prefix}")
            expected_windows = {
                "announcement": ANNOUNCEMENT_WINDOW_DAYS,
                "news": NEWS_WINDOW_DAYS,
            }
            if record.get("effective_windows_days") != expected_windows:
                gaps.append(f"hotspot_effective_windows_mismatch:{prefix}")
        frozen_raw = record.get("frozen_at")
        if not frozen_raw:
            gaps.append(f"hotspot_frozen_at_missing:{prefix}")
        else:
            try:
                frozen = _time(str(frozen_raw), f"frozen_at:{prefix}")
                if frozen != effective:
                    gaps.append(f"hotspot_effective_time_mismatch:{prefix}")
                for item_index, item in enumerate(record.get("items") or [], 1):
                    item_published = _time(
                        str(item.get("published_at") or ""),
                        f"hotspot_item_published:{prefix}:{item_index}",
                    )
                    if item_published > frozen:
                        gaps.append(
                            f"hotspot_item_from_future:{prefix}:{item_index}"
                        )
                    if strategy_version == TOPDOWN_STRATEGY_ID:
                        if not isinstance(item.get("time_valid"), bool):
                            gaps.append(
                                f"hotspot_item_time_valid_missing:{prefix}:{item_index}"
                            )
                        if not str(item.get("time_validation_reason") or ""):
                            gaps.append(
                                f"hotspot_item_time_reason_missing:{prefix}:{item_index}"
                            )
                        if (
                            (item.get("positive") or item.get("verified"))
                            and item.get("time_valid") is not True
                        ):
                            gaps.append(
                                f"hotspot_scored_item_time_invalid:{prefix}:{item_index}"
                            )
                        if item.get("major_negative") and item.get("time_valid") is not True:
                            gaps.append(
                                f"hotspot_major_negative_time_unverified:{prefix}:{item_index}"
                            )
            except ValueError as exc:
                gaps.append(f"hotspot_time_invalid:{prefix}:{exc}")
    return gaps


def _validate_archive_entries(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_ENTRY_COUNT:
        raise ValueError("bundle_entry_count_exceeded")
    total = 0
    seen: set[str] = set()
    for info in infos:
        _validate_artifact_path(info.filename)
        if info.filename in seen:
            raise ValueError(f"duplicate_archive_entry:{info.filename}")
        seen.add(info.filename)
        if info.flag_bits & 0x1:
            raise ValueError(f"encrypted_archive_entry:{info.filename}")
        if info.file_size > MAX_ENTRY_BYTES:
            raise ValueError(f"archive_entry_too_large:{info.filename}")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise ValueError("bundle_uncompressed_size_exceeded")


def _validate_artifact_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"unsafe_bundle_path:{value}")


def _jsonl_records(content: bytes, path: str) -> list[dict[str, Any]]:
    if not path.endswith(".jsonl"):
        raise ValueError(f"artifact_requires_jsonl:{path}")
    records: list[dict[str, Any]] = []
    for index, line in enumerate(content.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"artifact_record_not_object:{path}:{index}")
        records.append(value)
    return records


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label}_timezone_required")
    return parsed


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_source_url(url: str, source_kind: str) -> bool:
    parsed = urlparse(url)
    if source_kind == "strategy_definition":
        legacy = (
            parsed.scheme == "pa-agent"
            and parsed.netloc == "strategy"
            and parsed.path == f"/{CLOUD_AI_UNIVERSE_ID}"
        )
        epoch = (
            parsed.scheme == "pa-agent"
            and parsed.netloc == "validation-epoch"
            and bool(parsed.path.strip("/"))
            and all(
                value.isalnum() or value in {"-", "_"}
                for value in parsed.path.strip("/")
            )
        )
        return (
            (legacy or epoch)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in TRUSTED_SOURCE_HOSTS.get(source_kind, ())
    )
