from __future__ import annotations

import threading
from datetime import date, datetime
from types import SimpleNamespace

from pa_agent.config.settings import Settings
from pa_agent.trading.broker_models import (
    AuthorizedOrder,
    BrokerConnectionStatus,
    BrokerFill,
    BrokerOrder,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.oos_observations import pool_monitor_strategy_version
from pa_agent.trading.quant import SignalDecision
from pa_agent.trading.quant_runtime import (
    QuantRuntimeCoordinator,
    _signal_is_active_at,
    _topdown_scoring_candidates,
)
from pa_agent.trading.stock_sandbox import (
    StockSandboxState,
    StockTradingSandboxSnapshot,
)
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import (
    MANUAL_EXCEPTION_STRATEGY_ID,
    TOPDOWN_STRATEGY_ID,
    HotspotSnapshot,
)
from pa_agent.trading.universe import (
    CLOUD_AI_AUTHORIZATION_SYMBOLS,
    CLOUD_AI_SYMBOLS,
    CurrentUniverseMember,
    UniverseSnapshot,
)
from pa_agent.trading.validation_epoch import ValidationEpochRegistry

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


class _HotspotService:
    def __init__(self, *, release: threading.Event | None = None) -> None:
        self.calls: list[str] = []
        self.started = threading.Event()
        self.release = release

    def freeze(self, symbol: str) -> HotspotSnapshot:
        self.calls.append(symbol)
        self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=2)
        return HotspotSnapshot(
            symbol=symbol,
            captured_at=NOW,
            frozen_at=NOW,
        ).with_source_hash()


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


def test_runtime_projects_manual_exception_signal_into_its_own_watchlist_sandbox(
    tmp_path,
) -> None:
    ctx = _context(tmp_path)
    ctx.trade_store.upsert_watchlist_member(
        symbol="000001",
        name="平安银行",
        source="ths_watchlist",
        metadata={
            "manual_exception_eligible": True,
            "authorization_eligible": True,
            "ths_categories": ["银行"],
        },
    )
    ctx.trade_store.add_quant_signal(
        SignalDecision(
            status="allow",
            strategy_id=MANUAL_EXCEPTION_STRATEGY_ID,
            parameter_version="1.0.0",
            pool_version="manual-hs300-2026-08-000001",
            symbol="000001",
            signal_time="2026-08-13T15:00:00+08:00",
            condition_snapshot={
                "base_pool_version": "hs300-2026-08",
                "expected_security_name": "平安银行",
                "industry": "银行",
            },
            trigger_price=12.01,
            max_entry_price=12.20,
            initial_stop=11.50,
            valid_until="2026-08-14T15:00:00+08:00",
        )
    )
    runtime = QuantRuntimeCoordinator(
        ctx, now_provider=lambda: datetime.fromisoformat(NOW)
    )

    snapshots = {item.symbol: item for item in runtime.refresh_stock_sandboxes()}

    assert snapshots["000001"].state is StockSandboxState.INTRADAY_OBSERVING
    assert snapshots["000001"].daily_status == "通过"
    assert snapshots["000001"].trigger_price == 12.01


def test_trade_opportunity_notification_uses_mode_specific_authorization(
    tmp_path,
) -> None:
    ctx = _context(tmp_path)
    runtime = QuantRuntimeCoordinator(ctx)

    def snapshot(state: StockSandboxState, account_risk_status: str) -> StockTradingSandboxSnapshot:
        return StockTradingSandboxSnapshot(
            symbol="600519",
            name="贵州茅台",
            pool_version="hs300-2026-08",
            observed_at=NOW,
            market_session="trading",
            state=state,
            state_label=state.value,
            daily_status="通过",
            score_status="eligible_for_risk",
            hotspot_status="已跟踪",
            plan_id="plan-1",
            account_risk_status=account_risk_status,
            action="等待下一步",
            action_priority=10,
            input_hash=f"hash-{state.value}",
        )

    ctx.trade_store.current_strategy_state = lambda *_args, **_kwargs: "candidate"
    assert not runtime._trade_opportunity_notification_allowed(
        snapshot(StockSandboxState.QUANT_TRADEABLE, "not_evaluated")
    )

    ctx.trade_store.current_strategy_state = lambda *_args, **_kwargs: "shadow"
    assert runtime._trade_opportunity_notification_allowed(
        snapshot(StockSandboxState.QUANT_TRADEABLE, "not_evaluated")
    )

    ctx.settings.portfolio_risk.live_trading_enabled = True
    ctx.trade_store.current_strategy_state = lambda *_args, **_kwargs: "active"
    assert not runtime._trade_opportunity_notification_allowed(
        snapshot(StockSandboxState.QUANT_TRADEABLE, "not_evaluated")
    )
    assert runtime._trade_opportunity_notification_allowed(
        snapshot(StockSandboxState.AUTHORIZED, "authorized")
    )


def test_incomplete_broker_fact_tables_cannot_create_external_manual_trade(
    tmp_path,
) -> None:
    ctx = _context(tmp_path)
    runtime = QuantRuntimeCoordinator(ctx)
    snapshot = BrokerSnapshot(
        connection=ConnectionState(
            status=BrokerConnectionStatus.CONNECTED_READ_ONLY,
            account_fingerprint="untrusted-account",
            checked_at=datetime.now().astimezone().isoformat(),
        ),
        account_fingerprint="untrusted-account",
        fills=[BrokerFill(
            broker_fill_id="F-UNTRUSTED", broker_order_id="O-1",
            symbol="600519", direction="buy", price=100,
            quantity=100, filled_at=datetime.now().astimezone().isoformat(),
        )],
        orders_complete=False,
        fills_complete=False,
        captured_at=datetime.now().astimezone().isoformat(),
    )

    runtime._record_external_manual_fills(snapshot)

    assert ctx.trade_store.list_external_broker_trades() == []


def test_broker_fact_gap_warning_is_emitted_only_on_state_change(tmp_path) -> None:
    ctx = _context(tmp_path)
    ctx.settings.ths.confirmed = True
    ctx.settings.ths.account_fingerprint = "account-1"
    warnings: list[tuple] = []
    infos: list[tuple] = []
    ctx.logger = SimpleNamespace(
        warning=lambda *args: warnings.append(args),
        info=lambda *args: infos.append(args),
    )
    runtime = QuantRuntimeCoordinator(ctx)
    messages: list[tuple[str, str]] = []
    runtime.status_changed.connect(
        lambda task, detail: messages.append((task, detail))
    )

    unavailable = _Broker().snapshot()
    runtime.set_broker_snapshot(unavailable)
    runtime.set_broker_snapshot(unavailable)

    assert len(warnings) == 1
    assert len(messages) == 1
    assert messages[0][0] == "broker"

    captured_at = datetime.now().astimezone().isoformat()
    trusted = BrokerSnapshot(
        connection=ConnectionState(
            status=BrokerConnectionStatus.CONNECTED_READ_ONLY,
            account_fingerprint="account-1",
            checked_at=captured_at,
        ),
        account_fingerprint="account-1",
        total_equity=100_000,
        available_cash=100_000,
        position_value=0,
        orders_complete=True,
        fills_complete=True,
        captured_at=captured_at,
        complete=True,
    )
    runtime.set_broker_snapshot(trusted)
    runtime.set_broker_snapshot(trusted)

    assert len(infos) == 1
    assert len(messages) == 2
    assert messages[-1] == ("broker", "资金、持仓、委托和成交事实已同步")


def test_unrelated_same_symbol_fill_is_still_recorded_as_external_manual_trade(
    tmp_path,
) -> None:
    ctx = _context(tmp_path)
    runtime = QuantRuntimeCoordinator(ctx)
    now = datetime.now().astimezone().isoformat()
    account = "trusted-account"
    ctx.settings.ths.confirmed = True
    ctx.settings.ths.account_fingerprint = account
    decision_id = ctx.trade_store.add_decision(
        symbol="600519",
        timeframe="15m",
        asset_class="a_share",
        original_decision={},
        final_decision={},
        meta={},
    )
    plan = TradePlan(
        id="pending-plan",
        decision_event_id=decision_id,
        symbol="600519",
        timeframe="15m",
        asset_class=AssetClass.A_SHARE,
        direction="buy",
        order_type="limit",
        entry_price=100,
        stop_loss_price=95,
        take_profit_price=110,
        status=PlanStatus.RECONCILIATION_REQUIRED,
    )
    ctx.trade_store.add_plan(plan)
    authorized = AuthorizedOrder(
        plan_id=plan.id,
        account_fingerprint=account,
        symbol="600519",
        name="贵州茅台",
        direction="buy",
        price=100,
        quantity=100,
        stop_loss_price=95,
        strategy_id="s",
        authorized_at=now,
        expires_at=now,
    )
    ctx.trade_store.append_event(
        plan.id,
        "awaiting_user_confirmation",
        details={"authorized_order": authorized.model_dump(mode="json")},
    )
    snapshot = BrokerSnapshot(
        connection=ConnectionState(
            status=BrokerConnectionStatus.CONNECTED_READ_ONLY,
            account_fingerprint=account,
            checked_at=now,
        ),
        account_fingerprint=account,
        orders=[
            BrokerOrder(
                broker_order_id="O-PLAN",
                symbol="600519",
                direction="buy",
                price=100,
                quantity=100,
                status="已成",
                submitted_at=now,
            ),
            BrokerOrder(
                broker_order_id="O-MANUAL",
                symbol="600519",
                direction="buy",
                price=101,
                quantity=200,
                status="已成",
                submitted_at=now,
            ),
        ],
        fills=[
            BrokerFill(
                broker_fill_id="F-PLAN",
                broker_order_id="O-PLAN",
                symbol="600519",
                direction="buy",
                price=100,
                quantity=100,
                filled_at=now,
            ),
            BrokerFill(
                broker_fill_id="F-MANUAL",
                broker_order_id="O-MANUAL",
                symbol="600519",
                direction="buy",
                price=101,
                quantity=200,
                filled_at=now,
            ),
        ],
        orders_complete=True,
        fills_complete=True,
        captured_at=now,
    )

    runtime._record_external_manual_fills(snapshot)

    external = [
        item
        for item in ctx.trade_store.list_external_broker_trades()
        if item["account_fingerprint"] == account
    ]
    assert [item["broker_fill_id"] for item in external] == ["F-MANUAL"]


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
    assert runtime._last_topdown_slot == ""
    assert runtime._topdown_inflight_slot == "2026-08-13T10:00:00+08:00"
    assert ctx.trade_store.list_topdown_scores() == []
    assert not ctx.settings.ths.allow_prefill
    assert not ctx.settings.portfolio_risk.live_trading_enabled


def test_partial_oos_slot_is_retriable_and_complete_slot_survives_restart(
    tmp_path,
) -> None:
    ctx = _context(tmp_path, scanner=None)
    runtime = QuantRuntimeCoordinator(
        ctx, now_provider=lambda: datetime.fromisoformat("2026-08-14T10:00:30+08:00")
    )
    runtime._oos_market_inflight_slot = "2026-08-14T10:00:00+08:00"

    runtime._oos_market_observations_finished({
        "status": "data_incomplete",
        "bar_closed_at": "2026-08-14T10:00:00+08:00",
        "captured": 14,
        "required": 15,
        "failures": ["intraday_399006_expected_bar_missing"],
    })

    assert runtime._last_oos_market_slot == ""
    assert runtime._oos_market_inflight_slot == ""
    assert runtime._oos_market_retry_not_before is not None

    slot = "2026-08-14T10:00:00+08:00"
    all_symbols = [
        *CLOUD_AI_AUTHORIZATION_SYMBOLS,
        "000300",
        "000001",
        "000852",
        "399006",
    ]
    for symbol in all_symbols:
        ctx.trade_store.add_oos_observation(
            strategy_version=TOPDOWN_STRATEGY_ID,
            kind="intraday_15m",
            symbol=symbol,
            effective_at=slot,
            source_published_at=slot,
            source_kind="eastmoney_market",
            source_url="https://push2his.eastmoney.com/",
            payload={"symbol": symbol, "effective_at": slot, "close": 10},
        )
    ctx.trade_store.add_oos_observation(
        strategy_version=pool_monitor_strategy_version("hs300-2026-08"),
        kind="intraday_15m",
        symbol="600519",
        effective_at=slot,
        source_published_at=slot,
        source_kind="eastmoney_market",
        source_url="https://push2his.eastmoney.com/",
        payload={
            "pool_version": "hs300-2026-08",
            "symbol": "600519",
            "effective_at": slot,
            "close": 10,
        },
    )

    class Service:
        INDEXES = ("000300", "000001", "000852", "399006")

        def __init__(self) -> None:
            self.calls = 0

        def capture(self, **_kwargs):
            self.calls += 1
            raise AssertionError("complete persisted slot must not be fetched again")

    service = Service()
    ctx.oos_market_observation_service = service
    restarted = QuantRuntimeCoordinator(ctx)
    restarted.capture_oos_market_observations(
        now=datetime.fromisoformat("2026-08-14T10:00:31+08:00")
    )

    assert service.calls == 0
    assert restarted._last_oos_market_slot == slot


def test_daily_recovery_window_does_not_refetch_completed_daily_close(tmp_path) -> None:
    ctx = _context(tmp_path, scanner=None)
    slot = "2026-08-14T15:00:00+08:00"
    indexes = ("000300", "000001", "000852", "399006")
    for symbol in [*CLOUD_AI_AUTHORIZATION_SYMBOLS, *indexes]:
        ctx.trade_store.add_oos_observation(
            strategy_version=TOPDOWN_STRATEGY_ID,
            kind="daily_bars",
            symbol=symbol,
            effective_at=slot,
            source_published_at=slot,
            source_kind="eastmoney_market",
            source_url="https://push2his.eastmoney.com/",
            payload={"symbol": symbol, "effective_at": slot, "close": 10},
        )

    class Service:
        INDEXES = indexes

        def __init__(self) -> None:
            self.calls = 0

        def capture(self, **_kwargs):
            self.calls += 1
            raise AssertionError("complete daily recovery must not refetch")

    service = Service()
    ctx.oos_market_observation_service = service
    runtime = QuantRuntimeCoordinator(ctx)
    runtime.capture_oos_market_observations(
        now=datetime.fromisoformat("2026-08-14T15:10:00+08:00")
    )

    assert service.calls == 0
    assert runtime._last_oos_market_slot == slot


def test_current_private_pool_epoch_owns_runtime_oos_completeness(tmp_path) -> None:
    ctx = _context(tmp_path, scanner=None)
    pool_version = "ashare_private_pool-current-nine"
    symbols = ["600519", "300750"]
    snapshot = UniverseSnapshot(
        as_of=date(2026, 8, 14),
        version=pool_version,
        symbols=symbols,
        members=[
            CurrentUniverseMember(
                rank=index,
                symbol=symbol,
                name=symbol,
                average_amount_20=1_000_000,
            )
            for index, symbol in enumerate(symbols, 1)
        ],
        source_kind="user_managed_a_share_universe",
        source_hash="9" * 64,
        member_hash="9" * 64,
    )
    ctx.trade_store.upsert_universe_snapshot(snapshot)
    ctx.validation_epochs = ValidationEpochRegistry(ctx.trade_store)
    epoch = ctx.validation_epochs.activate(
        snapshot, activated_at=datetime.fromisoformat("2026-08-14T09:00:00+08:00")
    )
    slot = "2026-08-14T10:00:00+08:00"
    indexes = ("000300", "000001", "000852", "399006")
    for symbol in [*symbols, *indexes]:
        ctx.trade_store.add_oos_observation(
            strategy_version=epoch.observation_strategy_version,
            kind="intraday_15m",
            symbol=symbol,
            effective_at=slot,
            source_published_at=slot,
            source_kind="eastmoney_market",
            source_url="https://push2his.eastmoney.com/",
            payload={"symbol": symbol, "effective_at": slot, "close": 10},
        )
    for symbol in symbols:
        ctx.trade_store.add_oos_observation(
            strategy_version=pool_monitor_strategy_version(pool_version),
            kind="intraday_15m",
            symbol=symbol,
            effective_at=slot,
            source_published_at=slot,
            source_kind="eastmoney_market",
            source_url="https://push2his.eastmoney.com/",
            payload={"symbol": symbol, "effective_at": slot, "close": 10},
        )

    class Service:
        INDEXES = indexes

        def __init__(self) -> None:
            self.calls = 0

        def capture(self, **_kwargs):
            self.calls += 1
            raise AssertionError("current epoch complete slot must not be fetched again")

    service = Service()
    ctx.oos_market_observation_service = service
    runtime = QuantRuntimeCoordinator(ctx)
    runtime.capture_oos_market_observations(
        now=datetime.fromisoformat("2026-08-14T10:00:31+08:00")
    )

    assert service.calls == 0
    assert runtime._last_oos_market_slot == slot
    assert runtime._oos_slot_symbols(
        "intraday_15m", datetime.fromisoformat(slot)
    ) == set([*symbols, *indexes])


def test_hotspot_response_from_previous_pool_batch_is_not_bound_to_new_epoch(
    tmp_path,
) -> None:
    ctx = _context(tmp_path, scanner=None)
    registry = ValidationEpochRegistry(ctx.trade_store)
    first_snapshot = UniverseSnapshot(
        as_of=date(2026, 8, 14),
        version="ashare_private_pool-v1",
        symbols=["600519"],
        members=[CurrentUniverseMember(
            rank=1,
            symbol="600519",
            name="贵州茅台",
            average_amount_20=1_000_000,
        )],
        source_kind="user_managed_a_share_universe",
        source_hash="1" * 64,
        member_hash="1" * 64,
    )
    first = registry.activate(
        first_snapshot,
        activated_at=datetime.fromisoformat("2026-08-14T09:00:00+08:00"),
    )
    second_snapshot = UniverseSnapshot(
        as_of=date(2026, 8, 14),
        version="ashare_private_pool-v2",
        symbols=["600519", "300750"],
        members=[
            CurrentUniverseMember(
                rank=index,
                symbol=symbol,
                name=symbol,
                average_amount_20=1_000_000,
            )
            for index, symbol in enumerate(("600519", "300750"), 1)
        ],
        source_kind="user_managed_a_share_universe",
        source_hash="2" * 64,
        member_hash="2" * 64,
    )
    second = registry.activate(
        second_snapshot,
        activated_at=datetime.fromisoformat("2026-08-14T09:30:00+08:00"),
    )
    ctx.validation_epochs = registry
    runtime = QuantRuntimeCoordinator(ctx)

    stale = HotspotSnapshot(
        symbol="600519",
        captured_at="2026-08-14T10:00:00+08:00",
        frozen_at="2026-08-14T10:00:00+08:00",
    ).with_source_hash()
    runtime._store_hotspot_snapshot(
        stale,
        expected_epoch_id=first.epoch_id,
        expected_pool_version=first.pool_version,
    )
    stale_record = ctx.trade_store.latest_hotspot_snapshot("600519")
    assert stale_record["snapshot"]["validation_epoch_id"] == ""
    assert "hotspot_request_validation_epoch_mismatch" in (
        stale_record["snapshot"]["data_gaps"]
    )

    current = HotspotSnapshot(
        symbol="600519",
        captured_at="2026-08-14T10:01:00+08:00",
        frozen_at="2026-08-14T10:01:00+08:00",
    ).with_source_hash()
    runtime._store_hotspot_snapshot(
        current,
        expected_epoch_id=second.epoch_id,
        expected_pool_version=second.pool_version,
    )
    current_record = ctx.trade_store.latest_hotspot_snapshot("600519")
    assert current_record["snapshot"]["validation_epoch_id"] == second.epoch_id
    assert current_record["snapshot"]["pool_version"] == second.pool_version


def test_stock_sandbox_price_comes_from_current_pool_monitor_ledger(tmp_path) -> None:
    ctx = _context(tmp_path, scanner=None)
    slot = "2026-08-14T10:00:00+08:00"
    for pool_version, close in (
        ("hs300-2026-08", 123.45),
        ("some-other-pool", 999.0),
    ):
        ctx.trade_store.add_oos_observation(
            strategy_version=pool_monitor_strategy_version(pool_version),
            kind="intraday_15m",
            symbol="600519",
            effective_at=slot,
            source_published_at=slot,
            source_kind="eastmoney_market",
            source_url="https://push2his.eastmoney.com/",
            payload={
                "pool_version": pool_version,
                "symbol": "600519",
                "effective_at": slot,
                "close": close,
            },
        )

    snapshots = QuantRuntimeCoordinator(ctx).refresh_stock_sandboxes()

    assert len(snapshots) == 1
    assert snapshots[0].symbol == "600519"
    assert snapshots[0].latest_price == 123.45


def test_hotspots_monitor_complete_pool_even_when_all_daily_signals_reject(
    qtbot, tmp_path
) -> None:
    ctx = _context(tmp_path, scanner=None)
    ctx.trade_store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 13),
            version="cloud_ai_11_v1-2026-08",
            symbols=list(CLOUD_AI_SYMBOLS),
            source_as_of=date(2026, 8, 13),
            input_member_count=len(CLOUD_AI_SYMBOLS),
        ),
        source_updated_at="2026-08-13",
    )
    for symbol in CLOUD_AI_SYMBOLS:
        ctx.trade_store.add_quant_signal({
            "strategy_id": "cloud_ai_daily_pullback_v1",
            "parameter_version": "1.0.0",
            "pool_version": "cloud_ai_11_v1-2026-08",
            "symbol": symbol,
            "signal_time": NOW,
            "status": "reject",
            "reasons": ["daily_shape_not_ready"],
            "condition_snapshot": {},
        })
    service = _HotspotService()
    ctx.hotspot_service = service
    runtime = QuantRuntimeCoordinator(ctx)

    runtime.refresh_hotspots()

    qtbot.waitUntil(
        lambda: all(
            ctx.trade_store.latest_hotspot_snapshot(symbol) is not None
            for symbol in CLOUD_AI_SYMBOLS
        ),
        timeout=3000,
    )
    assert set(service.calls) == set(CLOUD_AI_SYMBOLS)
    assert "839494" in service.calls  # 北交所分析标的仍需监控公告风险


def test_hotspots_fail_closed_for_incomplete_pool(tmp_path) -> None:
    ctx = _context(tmp_path, scanner=None)
    ctx.trade_store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 14),
            version="cloud_ai_11_v1-2026-08-incomplete",
            symbols=list(CLOUD_AI_SYMBOLS),
            data_complete=False,
            completeness_reasons=["source_incomplete"],
        ),
        data_complete=False,
    )
    service = _HotspotService()
    ctx.hotspot_service = service
    runtime = QuantRuntimeCoordinator(ctx)
    messages: list[tuple[str, str]] = []
    runtime.status_changed.connect(lambda task, message: messages.append((task, message)))

    runtime.refresh_hotspots()

    assert service.calls == []
    assert messages == [
        ("hotspots", "股票池数据不完整，热点与重大公告监控未启动；禁止新增交易")
    ]


def test_hotspots_fail_closed_for_complete_but_empty_pool(tmp_path) -> None:
    ctx = _context(tmp_path, scanner=None)
    ctx.trade_store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 14),
            version="cloud_ai_11_v1-2026-08-empty",
            symbols=[],
        )
    )
    service = _HotspotService()
    ctx.hotspot_service = service
    runtime = QuantRuntimeCoordinator(ctx)
    messages: list[tuple[str, str]] = []
    runtime.status_changed.connect(lambda task, message: messages.append((task, message)))

    runtime.refresh_hotspots()

    assert service.calls == []
    assert messages == [
        ("hotspots", "股票池没有有效成员，热点与重大公告监控未启动；禁止新增交易")
    ]


def test_hotspot_refresh_deduplicates_while_batch_is_running(qtbot, tmp_path) -> None:
    ctx = _context(tmp_path, scanner=None)
    release = threading.Event()
    service = _HotspotService(release=release)
    ctx.hotspot_service = service
    runtime = QuantRuntimeCoordinator(ctx)

    runtime.refresh_hotspots()
    assert service.started.wait(timeout=1)
    runtime.refresh_hotspots()
    release.set()

    qtbot.waitUntil(
        lambda: ctx.trade_store.latest_hotspot_snapshot("600519") is not None,
        timeout=3000,
    )
    assert service.calls == ["600519"]


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


def test_manual_exception_is_added_to_scoring_without_mutating_base_pool() -> None:
    now = datetime.fromisoformat(NOW)
    universe = {
        "version": "hs300-2026-08",
        "as_of": "2026-08-13",
        "symbols": ["600519"],
        "data_complete": True,
    }
    original_symbols = list(universe["symbols"])

    def record(
        symbol: str,
        strategy_id: str,
        pool_version: str,
        *,
        valid_until: str = "2026-08-13T15:00:00+08:00",
        condition_snapshot: dict | None = None,
    ) -> dict:
        decision = {
            "strategy_id": strategy_id,
            "parameter_version": "1.0.0",
            "pool_version": pool_version,
            "symbol": symbol,
            "signal_time": "2026-08-12T15:00:00+08:00",
            "status": "allow",
            "reasons": [],
            "condition_snapshot": condition_snapshot or {},
            "trigger_price": 100,
            "max_entry_price": 101,
            "initial_stop": 95,
            "valid_until": valid_until,
        }
        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "pool_version": pool_version,
            "status": "allow",
            "decision": decision,
        }

    manual_pool = "manual-exception-hs300-2026-08-2026-08-13-000858"
    candidates = _topdown_scoring_candidates(
        [
            record("600519", "cloud_ai_daily_pullback_v1", "hs300-2026-08"),
            record(
                "000858",
                MANUAL_EXCEPTION_STRATEGY_ID,
                manual_pool,
                condition_snapshot={
                    "base_pool_version": "hs300-2026-08",
                    "expected_security_name": "五粮液",
                    "industry": "白酒",
                },
            ),
            record(
                "000333",
                MANUAL_EXCEPTION_STRATEGY_ID,
                "manual-exception-hs300-2026-08-stale-000333",
                valid_until="2026-08-12T15:00:00+08:00",
                condition_snapshot={"base_pool_version": "hs300-2026-08"},
            ),
        ],
        universe=universe,
        baseline_strategy_id="cloud_ai_daily_pullback_v1",
        now=now,
    )

    assert universe["symbols"] == original_symbols
    assert [signal.symbol for signal, _pool in candidates] == ["600519", "000858"]
    manual_signal, manual_context = candidates[1]
    assert manual_signal.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID
    assert manual_context["version"] == manual_pool
    assert manual_context["symbols"] == ["000858"]
    assert manual_context["base_pool_version"] == "hs300-2026-08"
    assert _signal_is_active_at(manual_signal, now)
