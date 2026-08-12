from __future__ import annotations

from pa_agent.config.settings import Settings
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.orchestrator.two_stage import _build_empty_record, _with_prompt_audit


def test_record_meta_contains_reproducibility_snapshot() -> None:
    frame = KlineFrame(
        symbol="600519", timeframe="1d",
        bars=(KlineBar(seq=1, ts_open=1, open=1, high=2, low=0.5, close=1.5, volume=1, closed=True),),
        indicators=IndicatorBundle(ema20=(1.0,), atr14=(1.0,)), snapshot_ts_local_ms=1,
    )
    record = _build_empty_record(frame, Settings())
    assert record.meta.strategy_version.startswith("pa-baseline-")
    assert record.meta.feature_version
    assert record.meta.model_name
    assert record.meta.app_git_commit
    audited = _with_prompt_audit(record, [])
    assert audited.meta.prompt_snapshot
    assert all(len(item["sha256"]) == 64 for item in audited.meta.prompt_snapshot)
