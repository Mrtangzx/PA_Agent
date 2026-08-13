from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from pa_agent.config.settings import Settings
from pa_agent.trading.broker_models import (
    BrokerConnectionStatus,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.quant_runtime import QuantRuntimeCoordinator
from pa_agent.trading.store import TradeStore
from pa_agent.trading.universe import UniverseSnapshot

NOW = "2026-08-13T10:00:00+08:00"


class _Broker:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> BrokerSnapshot:
        self.calls += 1
        return BrokerSnapshot(
            connection=ConnectionState(
                status=BrokerConnectionStatus.LOGIN_REQUIRED,
                message="登录由用户完成",
                checked_at=NOW,
            ),
            captured_at=NOW,
            complete=False,
            warnings=["login_required"],
        )


class _Scanner:
    def __init__(self) -> None:
        self.calls = 0

    def scan(self, pool_snapshot, *, progress=None):
        self.calls += 1
        if progress:
            progress(1, 1, "600519")
        decision = {
            "strategy_id": "cloud_ai_daily_pullback_v1",
            "parameter_version": "1.0.0",
            "pool_version": pool_snapshot["version"],
            "symbol": "600519",
            "signal_time": "2026-08-12T15:00:00+08:00",
            "status": "reject",
            "reasons": ["daily_recovery_confirmed"],
            "condition_snapshot": {"checks": {"daily_recovery_confirmed": False}},
        }
        return SimpleNamespace(
            decisions=[decision],
            allowed=[],
            data_complete=True,
            data_gaps=[],
            pool_version=pool_snapshot["version"],
            market_breadth_pct=50.0,
            signal_date=date(2026, 8, 12),
        )


def _context(tmp_path, *, scanner=None, broker=None):
    settings = Settings()
    settings.ths.allow_prefill = False
    settings.portfolio_risk.live_trading_enabled = False
    store = TradeStore(tmp_path / "trades.db")
    store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 13),
            version="hs300-2026-08",
            symbols=["600519"],
            source_as_of=date(2026, 8, 12),
            input_member_count=300,
        ),
        source_updated_at="2026-08-12",
    )
    return SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=broker or _Broker(),
        broker_trade_lifecycle=SimpleNamespace(
            broker_order_status=lambda *_args: ("filled", "broker_filled")
        ),
        universe_service=None,
        daily_candidate_scanner=scanner,
        hotspot_service=None,
        topdown_market_data_service=None,
        market_sentiment_service=None,
        trade_lifecycle=None,
        logger=SimpleNamespace(
            warning=lambda *_args: None,
            info=lambda *_args: None,
        ),
    )


def test_runtime_starts_without_workbench_and_keeps_trade_authority_closed(
    qtbot, tmp_path
) -> None:
    scanner = _Scanner()
    broker = _Broker()
    ctx = _context(tmp_path, scanner=scanner, broker=broker)
    runtime = QuantRuntimeCoordinator(
        ctx, now_provider=lambda: datetime.fromisoformat(NOW)
    )

    runtime.start()
    qtbot.waitUntil(lambda: broker.calls >= 1, timeout=2000)
    qtbot.waitUntil(
        lambda: len(ctx.trade_store.list_quant_signals()) == 1,
        timeout=3000,
    )

    assert runtime.started
    assert scanner.calls == 1
    assert runtime.broker_snapshot.connection.status == BrokerConnectionStatus.LOGIN_REQUIRED
    assert not ctx.settings.ths.allow_prefill
    assert not ctx.settings.portfolio_risk.live_trading_enabled

    runtime.stop()
    assert not runtime.started


def test_daily_scan_is_idempotent_across_repeated_calls_and_restart(qtbot, tmp_path) -> None:
    scanner = _Scanner()
    ctx = _context(tmp_path, scanner=scanner)
    first = QuantRuntimeCoordinator(
        ctx, now_provider=lambda: datetime.fromisoformat(NOW)
    )

    first.ensure_daily_candidates()
    qtbot.waitUntil(lambda: len(ctx.trade_store.list_quant_signals()) == 1, timeout=3000)
    first.ensure_daily_candidates()
    qtbot.wait(50)
    assert scanner.calls == 1

    second = QuantRuntimeCoordinator(
        ctx, now_provider=lambda: datetime.fromisoformat(NOW)
    )
    second.ensure_daily_candidates()
    qtbot.wait(50)
    assert scanner.calls == 1
    assert len(ctx.trade_store.list_quant_signals()) == 1


def test_topdown_same_slot_is_not_reprocessed_and_missing_input_fails_closed(
    tmp_path,
) -> None:
    ctx = _context(tmp_path, scanner=None)
    ctx.topdown_market_data_service = object()
    runtime = QuantRuntimeCoordinator(ctx)
    runtime.broker_snapshot = ctx.broker_adapter.snapshot()
    calls = []
    runtime._capture_market_sentiment = lambda universe, now: calls.append((universe, now))
    now = datetime.fromisoformat("2026-08-13T10:00:01+08:00")

    runtime.refresh_topdown_scores(now=now)
    runtime.refresh_topdown_scores(now=now.replace(second=59))

    assert len(calls) == 1
    assert runtime._last_topdown_slot == "2026-08-13T10:00:00+08:00"
    assert ctx.trade_store.list_topdown_scores() == []
    assert not ctx.settings.ths.allow_prefill
    assert not ctx.settings.portfolio_risk.live_trading_enabled


def test_closing_view_cannot_stop_application_runtime(qtbot, tmp_path) -> None:
    from pa_agent.gui.trade_ledger_window import TradeLedgerWindow

    ctx = _context(tmp_path, scanner=None)
    ctx.portfolio_risk = SimpleNamespace()
    ctx.trading_service = SimpleNamespace(risk_settings=ctx.settings.risk)
    runtime = QuantRuntimeCoordinator(ctx)
    ctx.quant_runtime = runtime
    runtime.start()
    window = TradeLedgerWindow(ctx)
    qtbot.addWidget(window)
    window.show()

    window.close()

    assert runtime.started
    assert runtime._broker_timer.isActive()
    runtime.stop()
