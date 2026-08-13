from __future__ import annotations

import hashlib
import json
import zipfile

from pa_agent.trading.oos_bundle import validate_oos_bundle


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


def test_complete_oos_bundle_is_validated_but_never_directly_promotion_eligible(tmp_path) -> None:
    report = validate_oos_bundle(_bundle(tmp_path))

    assert report.status == "complete"
    assert not report.promotion_eligible
    assert report.artifact_counts == {
        "historical_constituents": 1,
        "daily_bars": 1,
        "intraday_15m": 1,
        "market_sentiment": 1,
        "hotspots": 1,
    }
    assert report.data_gaps == []


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
