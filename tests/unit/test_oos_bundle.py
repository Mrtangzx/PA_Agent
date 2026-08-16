from __future__ import annotations

import hashlib
import json
import zipfile

from pa_agent.trading.oos_bundle import validate_oos_bundle
from pa_agent.trading.topdown import (
    LEGACY_TOPDOWN_STRATEGY_ID,
    TOPDOWN_SCORING_VERSION,
    TOPDOWN_STRATEGY_ID,
)
from pa_agent.trading.universe import (
    CLOUD_AI_STRATEGY_FROZEN_AT,
    CLOUD_AI_SYMBOLS,
    CLOUD_AI_UNIVERSE_ID,
    PRIVATE_A_SHARE_UNIVERSE_ID,
    cloud_ai_definition_hash,
)


def _bundle(tmp_path, *, future_hotspot: bool = False):
    published = "2025-12-31T18:00:00+08:00"
    packaged = "2026-02-01T18:00:00+08:00"
    effective = "2026-01-05T15:00:00+08:00"
    records = {
        "constituents.jsonl": [{
            "effective_at": effective,
            "source_published_at": published,
            "symbols": [f"{index:06d}" for index in range(300)],
        }],
        "daily.jsonl": [{
            "effective_at": effective,
            "source_published_at": published,
            "symbol": "000001",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
        }],
        "intraday.jsonl": [{
            "effective_at": effective,
            "source_published_at": published,
            "symbol": "000001",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
        }],
        "sentiment.jsonl": [{
            "effective_at": effective,
            "source_published_at": published,
            "advancing_pct": 60,
        }],
        "hotspots.jsonl": [{
            "effective_at": effective,
            "frozen_at": effective,
            "source_published_at": published,
            "symbol": "000001",
            "items": [{
                "title": "fixture",
                "published_at": (
                    "2026-01-06T09:00:00+08:00" if future_hotspot else published
                ),
            }],
        }],
    }
    kinds = {
        "constituents.jsonl": "historical_constituents",
        "daily.jsonl": "daily_bars",
        "intraday.jsonl": "intraday_15m",
        "sentiment.jsonl": "market_sentiment",
        "hotspots.jsonl": "hotspots",
    }
    source_kinds = {
        "constituents.jsonl": "csindex_official",
        "daily.jsonl": "eastmoney_market",
        "intraday.jsonl": "eastmoney_market",
        "sentiment.jsonl": "eastmoney_market",
        "hotspots.jsonl": "eastmoney_news",
    }
    source_urls = {
        "constituents.jsonl": "https://www.csindex.com.cn/constituents.jsonl",
        "daily.jsonl": "https://push2his.eastmoney.com/daily.jsonl",
        "intraday.jsonl": "https://push2his.eastmoney.com/intraday.jsonl",
        "sentiment.jsonl": "https://push2.eastmoney.com/sentiment.jsonl",
        "hotspots.jsonl": "https://finance.eastmoney.com/hotspots.jsonl",
    }
    payloads = {
        name: ("\n".join(json.dumps(item) for item in values) + "\n").encode()
        for name, values in records.items()
    }
    manifest = {
        "schema_version": "pa_oos_bundle_v1",
        "strategy_version": "hs300_topdown_4321_intraday_v1",
        "dataset": "out_of_sample",
        "period_start": "2026-01-01T00:00:00+08:00",
        "period_end": "2026-01-31T23:59:59+08:00",
        "artifacts": [{
            "path": name,
            "kind": kinds[name],
            "source_kind": source_kinds[name],
            "source_url": source_urls[name],
            "source_published_at": packaged,
            "sha256": hashlib.sha256(payload).hexdigest(),
        } for name, payload in payloads.items()],
    }
    path = tmp_path / "oos.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return path


def _private_epoch_bundle(tmp_path):
    path = _bundle(tmp_path)
    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}

    epoch_id = "ve-20260814-private-fixture"
    member_hash = "a" * 64
    activated_at = "2026-08-14T16:00:00+08:00"
    effective_at = "2026-08-15T15:00:00+08:00"
    pool_versions = ["pool-v1", "pool-v2"]
    for name in (
        "constituents.jsonl",
        "daily.jsonl",
        "intraday.jsonl",
        "sentiment.jsonl",
        "hotspots.jsonl",
    ):
        rows = [json.loads(line) for line in entries[name].decode().splitlines()]
        for row in rows:
            row.update({
                "effective_at": effective_at,
                "source_published_at": activated_at,
                "validation_epoch_id": epoch_id,
                "pool_version": "pool-v1",
                "member_hash": member_hash,
            })
            if name == "constituents.jsonl":
                row.update({
                    "symbols": ["600519"],
                    "authorization_symbols": ["600519"],
                    "universe_id": PRIVATE_A_SHARE_UNIVERSE_ID,
                    "universe_source_hash": member_hash,
                })
            elif name in {"daily.jsonl", "intraday.jsonl", "hotspots.jsonl"}:
                row["symbol"] = "600519"
            if name in {"daily.jsonl", "intraday.jsonl", "sentiment.jsonl"}:
                row["observed_at"] = effective_at
            if name == "hotspots.jsonl":
                row.update({
                    "frozen_at": effective_at,
                    "observed_at": effective_at,
                    "rule_version": "hotspot_time_window_v2",
                    "effective_windows_days": {"announcement": 30, "news": 3},
                })
                for item in row.get("items") or []:
                    item.update({
                        "published_at": activated_at,
                        "time_valid": True,
                        "time_validation_reason": "within_effective_window",
                    })
        entries[name] = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()

    manifest = json.loads(entries["manifest.json"])
    manifest.update({
        "schema_version": "pa_oos_bundle_v2",
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "period_start": "2026-08-15T00:00:00+08:00",
        "period_end": "2026-08-31T23:59:59+08:00",
        "universe_id": PRIVATE_A_SHARE_UNIVERSE_ID,
        "universe_source_hash": member_hash,
        "strategy_frozen_at": activated_at,
        "scoring_version": TOPDOWN_SCORING_VERSION,
        "hotspot_rule_version": "hotspot_time_window_v2",
        "validation_epoch_id": epoch_id,
        "validation_epoch_schema": "validation_epoch_v1",
        "pool_version": "pool-v1",
        "pool_versions": pool_versions,
        "origin_pool_version": "pool-v1",
        "member_hash": member_hash,
        "symbols": ["600519"],
        "authorization_symbols": ["600519"],
        "activated_at": activated_at,
    })
    for artifact in manifest["artifacts"]:
        artifact["sha256"] = hashlib.sha256(entries[artifact["path"]]).hexdigest()
        artifact["source_published_at"] = activated_at
        if artifact["kind"] == "historical_constituents":
            artifact["source_kind"] = "strategy_definition"
            artifact["source_url"] = f"pa-agent://validation-epoch/{epoch_id}"
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_complete_oos_bundle_is_validated_but_never_directly_promotion_eligible(tmp_path) -> None:
    report = validate_oos_bundle(_bundle(tmp_path))

    assert report.status == "complete"
    assert report.strategy_version == LEGACY_TOPDOWN_STRATEGY_ID
    assert not report.promotion_eligible
    assert report.artifact_counts == {
        "historical_constituents": 1,
        "daily_bars": 1,
        "intraday_15m": 1,
        "market_sentiment": 1,
        "hotspots": 1,
    }
    assert report.data_gaps == []


def test_current_strategy_requires_v2_post_freeze_fixed_universe_bundle(tmp_path) -> None:
    path = _bundle(tmp_path)
    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    effective = "2026-08-15T15:00:00+08:00"
    constituents = {
        "effective_at": effective,
        "source_published_at": CLOUD_AI_STRATEGY_FROZEN_AT,
        "symbols": list(CLOUD_AI_SYMBOLS),
    }
    entries["constituents.jsonl"] = (json.dumps(constituents) + "\n").encode()
    for name in ("daily.jsonl", "intraday.jsonl", "sentiment.jsonl", "hotspots.jsonl"):
        rows = [json.loads(line) for line in entries[name].decode().splitlines()]
        for row in rows:
            row["effective_at"] = effective
            row["source_published_at"] = CLOUD_AI_STRATEGY_FROZEN_AT
            if name in {
                "daily.jsonl", "intraday.jsonl", "sentiment.jsonl", "hotspots.jsonl"
            }:
                row["observed_at"] = effective
            if name == "hotspots.jsonl":
                row["frozen_at"] = effective
                row["rule_version"] = "hotspot_time_window_v2"
                row["effective_windows_days"] = {"announcement": 30, "news": 3}
                for item in row.get("items") or []:
                    item["published_at"] = CLOUD_AI_STRATEGY_FROZEN_AT
                    item["time_valid"] = True
                    item["time_validation_reason"] = "within_effective_window"
        entries[name] = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    manifest = json.loads(entries["manifest.json"])
    manifest.update({
        "schema_version": "pa_oos_bundle_v2",
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "period_start": "2026-08-15T00:00:00+08:00",
        "period_end": "2026-08-31T23:59:59+08:00",
        "universe_id": CLOUD_AI_UNIVERSE_ID,
        "universe_source_hash": cloud_ai_definition_hash(),
        "strategy_frozen_at": CLOUD_AI_STRATEGY_FROZEN_AT,
        "scoring_version": TOPDOWN_SCORING_VERSION,
        "hotspot_rule_version": "hotspot_time_window_v2",
    })
    for artifact in manifest["artifacts"]:
        artifact["sha256"] = hashlib.sha256(entries[artifact["path"]]).hexdigest()
        artifact["source_published_at"] = CLOUD_AI_STRATEGY_FROZEN_AT
        if artifact["kind"] == "historical_constituents":
            artifact["source_kind"] = "strategy_definition"
            artifact["source_url"] = f"pa-agent://strategy/{CLOUD_AI_UNIVERSE_ID}"
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    report = validate_oos_bundle(path)

    assert report.status == "complete"
    assert report.strategy_version == TOPDOWN_STRATEGY_ID
    assert report.schema_version == "pa_oos_bundle_v2"
    assert report.universe_id == CLOUD_AI_UNIVERSE_ID
    assert report.scoring_version == TOPDOWN_SCORING_VERSION
    assert report.hotspot_rule_version == "hotspot_time_window_v2"
    assert report.data_gaps == []


def test_private_epoch_bundle_accepts_refresh_alias_and_rejects_foreign_record(
    tmp_path,
) -> None:
    path = _private_epoch_bundle(tmp_path)
    report = validate_oos_bundle(path)
    assert report.status == "complete"
    assert report.validation_epoch_id == "ve-20260814-private-fixture"
    assert report.pool_version == "pool-v1"
    assert report.member_hash == "a" * 64

    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    intraday = json.loads(entries["intraday.jsonl"].decode().splitlines()[0])
    intraday["pool_version"] = "pool-v2"
    entries["intraday.jsonl"] = (json.dumps(intraday) + "\n").encode()
    manifest = json.loads(entries["manifest.json"])
    for artifact in manifest["artifacts"]:
        artifact["sha256"] = hashlib.sha256(entries[artifact["path"]]).hexdigest()
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    assert validate_oos_bundle(path).status == "complete"

    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    intraday = json.loads(entries["intraday.jsonl"].decode().splitlines()[0])
    intraday["validation_epoch_id"] = "old-epoch"
    entries["intraday.jsonl"] = (json.dumps(intraday) + "\n").encode()
    manifest = json.loads(entries["manifest.json"])
    for artifact in manifest["artifacts"]:
        artifact["sha256"] = hashlib.sha256(entries[artifact["path"]]).hexdigest()
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    invalid = validate_oos_bundle(path)
    assert invalid.status == "data_incomplete"
    assert "record_validation_epoch_mismatch:intraday.jsonl:1" in invalid.data_gaps


def test_current_strategy_rejects_legacy_schema_and_pre_freeze_period(tmp_path) -> None:
    path = _bundle(tmp_path)
    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["strategy_version"] = TOPDOWN_STRATEGY_ID
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    report = validate_oos_bundle(path)

    assert report.status == "data_incomplete"
    assert report.strategy_version == TOPDOWN_STRATEGY_ID
    assert any("legacy_bundle_requires_legacy" in gap for gap in report.data_gaps)


def test_oos_bundle_rejects_future_hotspot_information(tmp_path) -> None:
    report = validate_oos_bundle(_bundle(tmp_path, future_hotspot=True))

    assert report.status == "data_incomplete"
    assert any("hotspot_item_from_future" in gap for gap in report.data_gaps)
    assert not report.promotion_eligible


def test_oos_bundle_rejects_https_source_outside_trusted_domains(tmp_path) -> None:
    path = _bundle(tmp_path)
    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["artifacts"][0]["source_url"] = "https://attacker.example/official.jsonl"
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    report = validate_oos_bundle(path)

    assert report.status == "data_incomplete"
    assert any("artifact_source_not_trusted" in gap for gap in report.data_gaps)


def test_current_bundle_rejects_sentiment_observed_after_five_minutes(tmp_path) -> None:
    path = _bundle(tmp_path)
    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    effective = "2026-08-15T15:00:00+08:00"
    sentiment = json.loads(entries["sentiment.jsonl"].decode().splitlines()[0])
    sentiment.update({
        "effective_at": effective,
        "source_published_at": CLOUD_AI_STRATEGY_FROZEN_AT,
        "observed_at": "2026-08-15T15:05:01+08:00",
    })
    entries["sentiment.jsonl"] = (json.dumps(sentiment) + "\n").encode()
    manifest = json.loads(entries["manifest.json"])
    manifest.update({
        "schema_version": "pa_oos_bundle_v2",
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "period_start": "2026-08-15T00:00:00+08:00",
        "period_end": "2026-08-31T23:59:59+08:00",
        "universe_id": CLOUD_AI_UNIVERSE_ID,
        "universe_source_hash": cloud_ai_definition_hash(),
        "strategy_frozen_at": CLOUD_AI_STRATEGY_FROZEN_AT,
        "scoring_version": TOPDOWN_SCORING_VERSION,
        "hotspot_rule_version": "hotspot_time_window_v2",
    })
    for artifact in manifest["artifacts"]:
        artifact["sha256"] = hashlib.sha256(entries[artifact["path"]]).hexdigest()
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    report = validate_oos_bundle(path)

    assert report.status == "data_incomplete"
    assert any("sentiment_observation_delay_invalid" in gap for gap in report.data_gaps)
