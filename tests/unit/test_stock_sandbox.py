from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QPushButton

from pa_agent.config.settings import Settings
from pa_agent.gui.widgets.stock_pool_monitor import StockPoolMonitor
from pa_agent.notify.feishu_notifier import send_quant_tradeable_signal
from pa_agent.trading.stock_sandbox import (
    StockSandboxState,
    project_stock_sandboxes,
)
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID


def _universe() -> dict:
    return {
        "version": "cloud_ai_11_v1-2026-08",
        "data_complete": True,
        "symbols": ["600519", "300017"],
        "members": [
            {
                "symbol": "600519",
                "name": "贵州茅台",
                "authorization_eligible": True,
                "latest_price": 1418.2,
            },
            {
                "symbol": "300017",
                "name": "网宿科技",
                "authorization_eligible": True,
                "latest_price": 13.27,
            },
        ],
    }


def _signals() -> list[dict]:
    return [
        {
            "symbol": "600519",
            "pool_version": "cloud_ai_11_v1-2026-08",
            "signal_time": "2026-08-14T15:00:00+08:00",
            "status": "allow",
            "decision": {
                "status": "allow",
                "trigger_price": 1420.0,
                "max_entry_price": 1432.0,
                "initial_stop": 1390.0,
                "valid_until": "2026-08-17T15:00:00+08:00",
            },
        },
        {
            "symbol": "300017",
            "pool_version": "cloud_ai_11_v1-2026-08",
            "signal_time": "2026-08-14T15:00:00+08:00",
            "status": "reject",
            "decision": {"status": "reject", "reasons": ["daily_ma20_not_rising"]},
        },
    ]


def _eligible_score() -> dict:
    return {
        "symbol": "600519",
        "snapshot": {
            "symbol": "600519",
            "bar_closed_at": "2026-08-17T10:00:00+08:00",
            "status": "eligible_for_risk",
            "index_score": 31.0,
            "sentiment_score": 22.0,
            "theme_score": 14.0,
            "stock_score": 8.0,
            "total_score": 75.0,
            "consecutive_pass_count": 2,
            "input_hash": "score-hash",
        },
    }


def _plan() -> dict:
    return {
        "id": "plan-600519",
        "symbol": "600519",
        "status": "proposed",
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "entry_price": 1420.0,
        "stop_loss_price": 1390.0,
        "valid_until": "2026-08-17T15:00:00+08:00",
        "risk_snapshot": {
            "pool_version": "cloud_ai_11_v1-2026-08",
            "max_entry_price": 1432.0,
            "live_authorized": False,
        },
    }


def _project() -> list:
    return project_stock_sandboxes(
        universe=_universe(),
        signals=_signals(),
        scores=[_eligible_score()],
        plans=[_plan()],
        hotspots={
            "600519": {
                "snapshot": {
                    "symbol": "600519",
                    "source_hash": "hotspot-1",
                    "items": [{"title": "白酒板块资金活跃"}],
                    "negative_blocks": [],
                    "data_gaps": [],
                }
            }
        },
        observed_at="2026-08-17T10:00:05+08:00",
    )


def test_each_pool_stock_has_an_independent_deterministic_sandbox() -> None:
    snapshots = {item.symbol: item for item in _project()}

    assert snapshots["600519"].state is StockSandboxState.QUANT_TRADEABLE
    assert snapshots["600519"].total_score == 75
    assert snapshots["600519"].plan_id == "plan-600519"
    assert snapshots["600519"].action == "进入账户与组合风控"
    assert snapshots["300017"].state is StockSandboxState.DAILY_REJECTED
    assert snapshots["300017"].total_score is None
    assert snapshots["300017"].plan_id is None
    assert snapshots["300017"].input_hash != snapshots["600519"].input_hash


def test_major_negative_without_a_plan_is_labeled_as_a_risk_block() -> None:
    snapshots = project_stock_sandboxes(
        universe=_universe(),
        signals=_signals(),
        scores=[],
        plans=[],
        hotspots={
            "300017": {
                "snapshot": {
                    "symbol": "300017",
                    "source_hash": "negative-1",
                    "negative_blocks": ["major_negative_regulatory_investigation"],
                    "items": [{"title": "公司公告收到监管立案通知"}],
                }
            }
        },
        observed_at="2026-08-17T10:00:05+08:00",
    )
    by_symbol = {item.symbol: item for item in snapshots}

    assert by_symbol["300017"].state is StockSandboxState.MAJOR_RISK_BLOCKED
    assert by_symbol["300017"].state_label == "重大风险阻断"
    assert by_symbol["300017"].plan_id is None


def test_sandbox_never_reports_tradeable_when_score_contains_data_gaps() -> None:
    score = _eligible_score()
    score["snapshot"] = {
        **score["snapshot"],
        "status": "eligible_for_risk",
        "data_gaps": ["bar_time_mismatch:index_399006"],
    }

    snapshots = project_stock_sandboxes(
        universe=_universe(),
        signals=_signals(),
        scores=[score],
        plans=[_plan()],
        hotspots={},
        observed_at="2026-08-17T10:00:05+08:00",
    )
    item = next(value for value in snapshots if value.symbol == "600519")

    assert item.state is StockSandboxState.DATA_INCOMPLETE
    assert not item.tradeable
    assert item.total_score == 75
    assert item.data_gaps == ["bar_time_mismatch:index_399006"]


def test_sandbox_current_state_and_notification_claim_are_restart_safe(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    snapshot = _project()[0]

    assert store.upsert_stock_sandbox(snapshot) is None
    previous = store.upsert_stock_sandbox(
        snapshot.model_copy(update={"observed_at": "2026-08-17T10:00:15+08:00"})
    )
    assert previous is not None
    assert previous["state"] == "quant_tradeable"
    rows = store.list_stock_sandboxes(pool_version=snapshot.pool_version)
    assert len(rows) == 1
    assert rows[0]["snapshot"]["observed_at"] == "2026-08-17T10:00:15+08:00"

    assert store.claim_quant_notification(
        event_key="quant_tradeable|600519|10:00|plan-600519",
        symbol="600519",
        event_type="quant_tradeable",
        bar_closed_at="2026-08-17T10:00:00+08:00",
        plan_id="plan-600519",
    )
    assert not store.claim_quant_notification(
        event_key="quant_tradeable|600519|10:00|plan-600519",
        symbol="600519",
        event_type="quant_tradeable",
        bar_closed_at="2026-08-17T10:00:00+08:00",
        plan_id="plan-600519",
    )

    event_key = "quant_tradeable|600519|10:00|plan-600519"
    store.finish_quant_notification(
        event_key, delivered=False, details={"error": "temporary network error"}
    )
    assert store.claim_quant_notification(
        event_key=event_key,
        symbol="600519",
        event_type="quant_tradeable",
        bar_closed_at="2026-08-17T10:00:00+08:00",
        plan_id="plan-600519",
        retry_failed=True,
        max_attempts=3,
        retry_after_seconds=0,
    )
    assert not store.claim_quant_notification(
        event_key=event_key,
        symbol="600519",
        event_type="quant_tradeable",
        retry_failed=True,
        max_attempts=3,
        retry_after_seconds=0,
    )
    store.finish_quant_notification(event_key, delivered=False)
    assert store.claim_quant_notification(
        event_key=event_key,
        symbol="600519",
        event_type="quant_tradeable",
        retry_failed=True,
        max_attempts=3,
        retry_after_seconds=0,
    )
    store.finish_quant_notification(event_key, delivered=False)
    assert not store.claim_quant_notification(
        event_key=event_key,
        symbol="600519",
        event_type="quant_tradeable",
        retry_failed=True,
        max_attempts=3,
        retry_after_seconds=0,
    )
    row = store.list_quant_notifications(limit=1)[0]
    assert row["status"] == "failed"
    assert row["details"]["attempt_count"] == 3
    assert row["details"]["error"] == "temporary network error"


def test_delivered_notification_is_never_reclaimed(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    event_key = "quant_tradeable|600519|10:15|plan-2"
    assert store.claim_quant_notification(
        event_key=event_key,
        symbol="600519",
        event_type="quant_tradeable",
    )
    store.finish_quant_notification(event_key, delivered=True)

    assert not store.claim_quant_notification(
        event_key=event_key,
        symbol="600519",
        event_type="quant_tradeable",
        retry_failed=True,
        max_attempts=3,
        retry_after_seconds=0,
        recover_pending_after_seconds=0,
    )


def test_quant_feishu_card_reports_signal_but_keeps_user_confirmation_boundary() -> None:
    settings = Settings()
    settings.feishu.enabled = True
    settings.feishu.webhook_url = "https://open.feishu.cn/test-hook"
    response = Mock()
    response.json.return_value = {"code": 0}

    with patch("requests.post", return_value=response) as post:
        delivered = send_quant_tradeable_signal(
            sandbox=_project()[0],
            settings=settings,
        )

    assert delivered
    payload = post.call_args.kwargs["json"]
    content = payload["card"]["body"]["elements"][0]["content"]
    assert "贵州茅台 600519" in content
    assert "75.0/100" in content
    assert "影子模式风险门禁已通过" in content
    assert "不会形成真实委托" in content


def test_home_monitor_shows_full_pool_and_selects_a_symbol(qtbot, tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    universe = _universe()
    store.upsert_universe_snapshot(
        {
            **universe,
            "as_of": "2026-08-14",
            "source_updated_at": "2026-08-14T15:00:00+08:00",
        },
        source_updated_at="2026-08-14T15:00:00+08:00",
        data_complete=True,
    )
    for snapshot in _project():
        store.upsert_stock_sandbox(snapshot)
    settings = Settings()
    widget = StockPoolMonitor(
        SimpleNamespace(settings=settings, trade_store=store)
    )
    qtbot.addWidget(widget)
    widget.show()

    assert widget.table.rowCount() == 2
    assert "全池 2只" in widget.summary_label.text()
    assert "可交易 1" in widget.summary_label.text()
    assert widget.findChild(QPushButton, "stockPoolOpenTradingButton") is not None
    with qtbot.waitSignal(widget.symbol_selected) as emitted:
        widget.table.selectRow(0)
    assert emitted.args == ["600519"]
