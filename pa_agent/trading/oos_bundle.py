"""Strict, no-future-data import boundary for out-of-sample validation bundles."""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID

BUNDLE_SCHEMA = "pa_oos_bundle_v1"
REQUIRED_KINDS = {
    "historical_constituents",
    "daily_bars",
    "intraday_15m",
    "market_sentiment",
    "hotspots",
}
TRUSTED_SOURCE_KINDS = {
    "historical_constituents": {"csindex_official"},
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
    ]
    source_url: str
    source_published_at: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OosBundleManifest(BaseModel):
    schema_version: Literal["pa_oos_bundle_v1"]
    strategy_version: Literal["hs300_topdown_4321_intraday_v1"]
    dataset: Literal["out_of_sample"]
    period_start: str
    period_end: str
    artifacts: list[BundleArtifact] = Field(min_length=5)


class OosBundleValidationReport(BaseModel):
    # The existing bundle schema contains HS300 historical membership and must
    # remain attributed to the frozen legacy strategy.
    strategy_version: str = "hs300_topdown_4321_intraday_v1"
    dataset: str = "out_of_sample_data_bundle"
    status: str
    input_hash: str
    promotion_eligible: bool = False
    bundle_path: str
    period_start: str = ""
    period_end: str = ""
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
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            infos = archive.infolist()
            _validate_archive_entries(infos)
            names = {info.filename for info in infos if not info.is_dir()}
            if "manifest.json" not in names:
                raise ValueError("bundle_manifest_missing")
            manifest_bytes = archive.read("manifest.json")
            manifest = OosBundleManifest.model_validate_json(manifest_bytes)
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
        status="complete" if not gaps else "data_incomplete",
        input_hash=bundle_hash or hashlib.sha256(str(bundle_path).encode()).hexdigest(),
        bundle_path=str(bundle_path.resolve()),
        period_start=period_start,
        period_end=period_end,
        artifact_counts=artifact_counts,
        record_counts=record_counts,
        data_gaps=gaps,
        checked_at=checked_at,
    )


def _validate_record(
    kind: str,
    record: dict[str, Any],
    *,
    index: int,
    path: str,
    period_start: datetime,
    period_end: datetime,
) -> list[str]:
    prefix = f"{path}:{index}"
    gaps: list[str] = []
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
    if kind == "historical_constituents":
        symbols = [str(value) for value in record.get("symbols") or []]
        if len(symbols) != 300 or len(set(symbols)) != 300:
            gaps.append(f"historical_constituents_not_300_unique:{prefix}")
        if any(not value.isdigit() or len(value) != 6 for value in symbols):
            gaps.append(f"historical_constituent_symbol_invalid:{prefix}")
    if kind == "hotspots":
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
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in TRUSTED_SOURCE_HOSTS.get(source_kind, ())
    )
