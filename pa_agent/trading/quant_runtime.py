"""Application-scoped runtime for deterministic quant data collection.

The quant workbench is a view over this coordinator.  Collection must continue
when that window has never been opened or has been closed.  This module never
prefills or submits an order; it only persists read-only broker snapshots,
deterministic inputs/scores, shadow plans, and lifecycle observations.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from pa_agent.trading.quant import SignalDecision
from pa_agent.trading.topdown import TopDownScoreSnapshot


class _UniverseWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: Any) -> None:
        super().__init__()
        self.service = service

    def run(self) -> None:
        try:
            snapshot = self.service.generate(
                progress=lambda current, total, symbol: self.progress.emit(
                    current, total, symbol
                )
            )
            self.finished.emit(snapshot)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _DailyCandidateWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, scanner: Any, pool_snapshot: dict[str, Any]) -> None:
        super().__init__()
        self.scanner = scanner
        self.pool_snapshot = pool_snapshot

    def run(self) -> None:
        try:
            result = self.scanner.scan(
                self.pool_snapshot,
                progress=lambda current, total, symbol: self.progress.emit(
                    current, total, symbol
                ),
            )
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _HotspotBatchWorker(QObject):
    snapshot_ready = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, service: Any, symbols: list[str]) -> None:
        super().__init__()
        self.service = service
        self.symbols = symbols

    def run(self) -> None:
        for symbol in self.symbols:
            if QThread.currentThread().isInterruptionRequested():
                break
            try:
                self.snapshot_ready.emit(self.service.freeze(symbol))
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(symbol, str(exc))
        self.finished.emit()


class _MarketSentimentWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        service: Any,
        store: Any,
        hs300_breadth_pct: float | None,
        captured_at: datetime,
    ) -> None:
        super().__init__()
        self.service = service
        self.store = store
        self.hs300_breadth_pct = hs300_breadth_pct
        self.captured_at = captured_at

    def run(self) -> None:
        try:
            self.finished.emit(
                self.service.capture_for_store(
                    store=self.store,
                    hs300_breadth_pct=self.hs300_breadth_pct,
                    captured_at=self.captured_at,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _TopDownBatchWorker(QObject):
    score_ready = pyqtSignal(object, object)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, service: Any, jobs: list[dict[str, Any]]) -> None:
        super().__init__()
        self.service = service
        self.jobs = jobs

    def run(self) -> None:
        for job in self.jobs:
            if QThread.currentThread().isInterruptionRequested():
                break
            try:
                result = self.service.build_context(**job)
                score = self.service.scoring.evaluate(result.context)
                if result.data_gaps:
                    score = score.model_copy(
                        update={
                            "data_gaps": list(
                                dict.fromkeys([*score.data_gaps, *result.data_gaps])
                            )
                        }
                    )
                self.score_ready.emit(score, result.closed_stock_bar)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(job["symbol"], str(exc))
        self.finished.emit()


class _LifecycleDailySyncWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: Any) -> None:
        super().__init__()
        self.service = service

    def run(self) -> None:
        try:
            self.finished.emit(self.service.sync_open_daily())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class QuantRuntimeCoordinator(QObject):
    """Own long-lived quant collection independently of any workbench window."""

    updated = pyqtSignal()
    broker_snapshot_changed = pyqtSignal(object)
    status_changed = pyqtSignal(str, str)
    task_failed = pyqtSignal(str, str)

    def __init__(
        self,
        ctx: Any,
        parent: QObject | None = None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = ctx.trade_store
        self._now = now_provider or (lambda: datetime.now().astimezone())
        self.broker_snapshot: Any = None
        self.last_lifecycle_sync: dict[str, Any] = {}
        self._last_topdown_slot = ""
        self._active_reconciliation_plan_ids: set[str] = set()
        self._started = False
        self._stopping = False
        self._threads: dict[str, QThread | None] = {
            "universe": None,
            "daily_candidates": None,
            "hotspots": None,
            "sentiment": None,
            "topdown": None,
            "lifecycle": None,
        }
        self._workers: dict[str, QObject | None] = {key: None for key in self._threads}

        self._broker_timer = QTimer(self)
        self._broker_timer.timeout.connect(self.sync_broker)
        self._universe_timer = QTimer(self)
        self._universe_timer.timeout.connect(self.ensure_current_universe)
        self._daily_candidate_timer = QTimer(self)
        self._daily_candidate_timer.timeout.connect(self.ensure_daily_candidates)
        self._hotspot_timer = QTimer(self)
        self._hotspot_timer.timeout.connect(self.refresh_hotspots)
        self._topdown_timer = QTimer(self)
        self._topdown_timer.timeout.connect(self.refresh_topdown_scores)
        self._lifecycle_timer = QTimer(self)
        self._lifecycle_timer.timeout.connect(self.sync_daily_lifecycle)

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        idle_seconds = max(5, int(self.ctx.settings.ths.idle_sync_seconds))
        hotspot_seconds = max(
            60, int(self.ctx.settings.topdown_scoring.hotspot_refresh_seconds)
        )
        self._broker_timer.start(idle_seconds * 1000)
        self._universe_timer.start(6 * 60 * 60 * 1000)
        self._daily_candidate_timer.start(5 * 60 * 1000)
        self._hotspot_timer.start(hotspot_seconds * 1000)
        self._topdown_timer.start(10_000)
        self._lifecycle_timer.start(30 * 60 * 1000)
        self.ctx.logger.info(
            "常驻量化采集已启动: broker=%ss hotspot=%ss topdown=10s "
            "daily_candidates=300s lifecycle=1800s prefill=%s live=%s",
            idle_seconds,
            hotspot_seconds,
            bool(self.ctx.settings.ths.allow_prefill),
            bool(self.ctx.settings.portfolio_risk.live_trading_enabled),
        )
        self.status_changed.emit("runtime", "常驻量化采集已启动")
        QTimer.singleShot(0, self.sync_broker)
        QTimer.singleShot(500, self.ensure_current_universe)
        QTimer.singleShot(1500, self.ensure_daily_candidates)
        QTimer.singleShot(2000, self.refresh_hotspots)
        QTimer.singleShot(2500, self.refresh_topdown_scores)
        QTimer.singleShot(3200, self.sync_daily_lifecycle)

    def stop(self) -> None:
        if not self._started and not any(self._thread_running(k) for k in self._threads):
            return
        self._stopping = True
        for timer in (
            self._broker_timer,
            self._universe_timer,
            self._daily_candidate_timer,
            self._hotspot_timer,
            self._topdown_timer,
            self._lifecycle_timer,
        ):
            timer.stop()
        for key, thread in list(self._threads.items()):
            if thread is None:
                continue
            try:
                if thread.isRunning():
                    thread.requestInterruption()
                    thread.quit()
                    if not thread.wait(5000):
                        self.ctx.logger.warning("量化后台线程退出超时: %s", key)
            except RuntimeError:
                pass
            self._threads[key] = None
            self._workers[key] = None
        self._started = False
        self.ctx.logger.info("常驻量化采集已停止")
        self.status_changed.emit("runtime", "常驻量化采集已停止")

    def _thread_running(self, key: str) -> bool:
        thread = self._threads.get(key)
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            self._threads[key] = None
            self._workers[key] = None
            return False

    def _task_active(self, key: str) -> bool:
        """Deduplicate a task until its queued completion releases ownership."""
        thread = self._threads.get(key)
        worker = self._workers.get(key)
        if thread is None or worker is None:
            return False
        try:
            thread.objectName()
        except RuntimeError:
            self._threads[key] = None
            self._workers[key] = None
            return False
        return True

    def _hold_thread(self, key: str, thread: QThread, worker: QObject) -> None:
        self._threads[key] = thread
        self._workers[key] = worker
        thread.finished.connect(lambda key=key: self._release_thread(key))

    def _release_thread(self, key: str) -> None:
        self._threads[key] = None
        self._workers[key] = None

    def sync_broker(self) -> Any:
        """Persist one read-only broker snapshot; never touches trading controls."""
        adapter = getattr(self.ctx, "broker_adapter", None)
        if adapter is None or self._stopping:
            return None
        try:
            snapshot = adapter.snapshot()
            self.broker_snapshot = snapshot
            if self.store.available:
                self.store.add_broker_snapshot(snapshot)
                if snapshot.complete and snapshot.total_equity is not None:
                    self.store.record_broker_financial_snapshot(snapshot)
                self._refresh_linked_broker_orders(snapshot)
                self._record_external_manual_fills(snapshot)
            self.broker_snapshot_changed.emit(snapshot)
            self.ctx.logger.info(
                "常驻量化只读同步完成: status=%s complete=%s positions=%s "
                "orders=%s fills=%s",
                snapshot.connection.status.value,
                snapshot.complete,
                len(snapshot.positions),
                len(snapshot.orders),
                len(snapshot.fills),
            )
            self.updated.emit()
            return snapshot
        except Exception as exc:  # noqa: BLE001
            self._fail("broker", exc)
            return None

    def set_broker_snapshot(self, snapshot: Any) -> None:
        self.broker_snapshot = snapshot
        self.broker_snapshot_changed.emit(snapshot)
        self.updated.emit()

    def begin_reconciliation(self, plan_id: str) -> None:
        """Prevent a just-prefilled order from being classified as manual."""
        if plan_id:
            self._active_reconciliation_plan_ids.add(plan_id)

    def end_reconciliation(self, plan_id: str) -> None:
        self._active_reconciliation_plan_ids.discard(plan_id)

    def _refresh_linked_broker_orders(self, snapshot: Any) -> None:
        from pa_agent.trading.broker_models import ReconciliationResult

        for link in self.store.list_broker_order_links():
            broker_order = next(
                (
                    item
                    for item in snapshot.orders
                    if item.broker_order_id == link["broker_order_id"]
                ),
                None,
            )
            if broker_order is None:
                continue
            fills = [
                item
                for item in snapshot.fills
                if item.broker_order_id == broker_order.broker_order_id
            ]
            self.store.link_broker_order(
                ReconciliationResult(
                    status="matched",
                    plan_id=link["plan_id"],
                    matched_order_ids=[broker_order.broker_order_id],
                    matched_fill_ids=[
                        item.broker_fill_id for item in fills if item.broker_fill_id
                    ],
                    message="周期同步更新已关联委托成交",
                ),
                details={
                    **link.get("details", {}),
                    "order": broker_order.model_dump(mode="json"),
                    "periodic_sync": True,
                },
            )
            status, event_type = self.ctx.broker_trade_lifecycle.broker_order_status(
                broker_order.status,
                broker_order.filled_quantity,
                broker_order.quantity,
            )
            self.store.upsert_broker_execution(
                plan_id=link["plan_id"],
                fills=fills,
                plan_status="executed_open" if status == "filled" else status,
                event_type=event_type,
                broker_order_id=broker_order.broker_order_id,
            )

    def _record_external_manual_fills(self, snapshot: Any) -> None:
        if self._active_reconciliation_plan_ids:
            return
        linked = self.store.linked_broker_fill_ids()
        pending_matches = {
            (item["symbol"], item["direction"])
            for item in self.store.list_plans(
                statuses=["awaiting_user_confirmation", "reconciliation_required"]
            )
        }
        for fill in snapshot.fills:
            if not fill.broker_fill_id or fill.broker_fill_id in linked:
                continue
            if (fill.symbol, fill.direction) in pending_matches:
                continue
            if self.store.add_external_broker_trade(
                fill, account_fingerprint=snapshot.account_fingerprint
            ):
                self.ctx.logger.info(
                    "EXTERNAL_MANUAL_TRADE %s %s %s",
                    fill.broker_fill_id,
                    fill.symbol,
                    fill.direction,
                )

    def ensure_current_universe(self, *, force: bool = False) -> None:
        if self._stopping or not self.store.available:
            return
        service = getattr(self.ctx, "universe_service", None)
        if service is None or self._thread_running("universe"):
            return
        current_version = (
            service.current_version(self._now())
            if hasattr(service, "current_version")
            else f"hs300-{self._now():%Y-%m}"
        )
        if not force and any(
            item.get("version") == current_version
            and item.get("data_complete")
            for item in self.store.list_universe_snapshots(limit=3)
        ):
            self.ensure_daily_candidates()
            return
        thread = QThread(self)
        worker = _UniverseWorker(service)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda current, total, symbol: self.status_changed.emit(
                "universe", f"正在校验 {current}/{total}：{symbol}"
            )
        )
        worker.finished.connect(self._universe_generated)
        worker.failed.connect(lambda error: self._fail("universe", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("universe", thread, worker)
        self.status_changed.emit("universe", "正在生成本月新云算力 11 股固定池")
        thread.start()

    def _universe_generated(self, snapshot: Any) -> None:
        self.store.upsert_universe_snapshot(
            snapshot,
            source_updated_at=(
                snapshot.source_as_of.isoformat() if snapshot.source_as_of else ""
            ),
            data_complete=snapshot.data_complete,
        )
        if snapshot.data_complete:
            detail = f"{snapshot.version} 已生成：{len(snapshot.symbols)}只"
        else:
            detail = "数据不完整，禁止授权：" + ", ".join(
                snapshot.completeness_reasons
            )
        self.status_changed.emit("universe", detail)
        self.updated.emit()
        QTimer.singleShot(0, self.ensure_daily_candidates)

    def ensure_daily_candidates(self) -> None:
        if self._stopping or not self.store.available:
            return
        scanner = getattr(self.ctx, "daily_candidate_scanner", None)
        if scanner is None or self._task_active("daily_candidates"):
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            return
        pool = universes[0]["snapshot"]
        today = self._now()
        expected_day = today.date()
        if today.hour < 15:
            expected_day = expected_day.fromordinal(expected_day.toordinal() - 1)
        while expected_day.weekday() >= 5:
            expected_day = expected_day.fromordinal(expected_day.toordinal() - 1)
        already_scanned = any(
            item.get("pool_version") == pool.get("version")
            and str(item.get("signal_time") or "")[:10] == expected_day.isoformat()
            for item in self.store.list_quant_signals(
                strategy_id=self.ctx.settings.strategy.strategy_id,
                limit=max(100, len(pool.get("symbols") or []) * 4),
            )
        )
        if already_scanned:
            return
        thread = QThread(self)
        worker = _DailyCandidateWorker(scanner, pool)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda current, total, symbol: self.status_changed.emit(
                "daily_candidates", f"正在扫描 {current}/{total}：{symbol}"
            )
        )
        worker.finished.connect(self._daily_candidates_finished)
        worker.failed.connect(lambda error: self._fail("daily_candidates", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("daily_candidates", thread, worker)
        self.status_changed.emit(
            "daily_candidates",
            f"正在扫描 {pool.get('version', '')} 的收盘后日线候选",
        )
        thread.start()

    def _daily_candidates_finished(self, result: Any) -> None:
        for decision in result.decisions:
            self.store.add_quant_signal(decision)
        if result.data_complete:
            detail = (
                f"{result.pool_version} 扫描{len(result.decisions)}只，"
                f"候选{len(result.allowed)}只，信号日{result.signal_date}"
            )
        else:
            detail = "数据不完整，禁止授权：" + ", ".join(result.data_gaps)
        self.status_changed.emit("daily_candidates", detail)
        self.updated.emit()
        QTimer.singleShot(0, self.refresh_hotspots)
        QTimer.singleShot(0, self.refresh_topdown_scores)

    def refresh_hotspots(self) -> None:
        service = getattr(self.ctx, "hotspot_service", None)
        if (
            self._stopping
            or service is None
            or not self.store.available
            or self._thread_running("hotspots")
        ):
            return
        symbols = {
            item["symbol"]
            for item in self.store.list_plans(lifecycle_open=True)
            if item.get("symbol")
        }
        symbols.update(
            item["symbol"]
            for item in self.store.list_quant_signals(limit=1000)
            if item.get("status") == "allow" and item.get("symbol")
        )
        if self.broker_snapshot is not None:
            symbols.update(item.symbol for item in self.broker_snapshot.positions)
        if not symbols:
            return
        thread = QThread(self)
        worker = _HotspotBatchWorker(service, sorted(symbols))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.snapshot_ready.connect(self._store_hotspot_snapshot)
        worker.failed.connect(lambda symbol, error: self._fail(f"hotspot:{symbol}", error))
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.updated.emit)
        self._hold_thread("hotspots", thread, worker)
        self.status_changed.emit("hotspots", f"正在刷新{len(symbols)}只股票的热点快照")
        thread.start()

    def _store_hotspot_snapshot(self, snapshot: Any) -> None:
        self.store.add_hotspot_snapshot(snapshot)
        if not snapshot.negative_blocks:
            return
        for plan in self.store.list_plans(symbol=snapshot.symbol):
            if plan.get("status") not in {"proposed", "triggered"}:
                continue
            self.store.update_plan(plan["id"], status="invalidated")
            self.store.append_event(
                plan["id"],
                "major_negative_invalidated",
                details={
                    "negative_blocks": snapshot.negative_blocks,
                    "hotspot_source_hash": snapshot.source_hash,
                    "frozen_at": snapshot.frozen_at,
                },
            )

    def refresh_topdown_scores(self, *, now: datetime | None = None) -> None:
        service = getattr(self.ctx, "topdown_market_data_service", None)
        if (
            self._stopping
            or service is None
            or not self.store.available
            or self.broker_snapshot is None
            or self._thread_running("topdown")
            or self._thread_running("sentiment")
        ):
            return
        now = now or self._now()
        if now.weekday() >= 5 or not (
            (9, 30) <= (now.hour, now.minute) <= (11, 30)
            or (13, 0) <= (now.hour, now.minute) <= (15, 0)
        ):
            return
        slot = now.replace(
            minute=now.minute - now.minute % 15, second=0, microsecond=0
        ).isoformat()
        if slot == self._last_topdown_slot or now.minute % 15 > 4:
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            return
        self._last_topdown_slot = slot
        self._capture_market_sentiment(universes[0]["snapshot"], now)

    def _capture_market_sentiment(
        self, universe: dict[str, Any], now: datetime
    ) -> None:
        service = getattr(self.ctx, "market_sentiment_service", None)
        if service is None:
            self._build_topdown_jobs(universe, now, None)
            return
        signals = self.store.list_quant_signals(limit=1000)
        breadth = next(
            (
                float(
                    (item.get("decision") or {})
                    .get("condition_snapshot", {})
                    .get("market_breadth_pct")
                )
                for item in signals
                if (item.get("decision") or {})
                .get("condition_snapshot", {})
                .get("market_breadth_pct")
                is not None
            ),
            None,
        )
        thread = QThread(self)
        worker = _MarketSentimentWorker(service, self.store, breadth, now)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(
            lambda snapshot: self._sentiment_captured(universe, now, snapshot)
        )
        worker.failed.connect(
            lambda error: self._sentiment_failed(universe, now, error)
        )
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("sentiment", thread, worker)
        thread.start()

    def _sentiment_captured(
        self, universe: dict[str, Any], now: datetime, snapshot: Any
    ) -> None:
        self.store.add_market_sentiment_snapshot(snapshot)
        sentiment = snapshot.input if snapshot.data_complete else None
        self._build_topdown_jobs(universe, now, sentiment)

    def _sentiment_failed(
        self, universe: dict[str, Any], now: datetime, error: str
    ) -> None:
        self._fail("sentiment", error)
        self._build_topdown_jobs(universe, now, None)

    def _build_topdown_jobs(
        self, universe: dict[str, Any], now: datetime, sentiment: Any
    ) -> None:
        service = getattr(self.ctx, "topdown_market_data_service", None)
        if service is None or self._stopping:
            return
        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for record in self.store.list_quant_signals(limit=1000):
            if record.get("status") != "allow":
                continue
            latest_by_symbol.setdefault(record["symbol"], record)
        jobs: list[dict[str, Any]] = []
        for symbol in universe.get("symbols") or []:
            record = latest_by_symbol.get(symbol)
            if record is None:
                continue
            try:
                signal = SignalDecision.model_validate(record["decision"])
            except Exception:  # noqa: BLE001
                continue
            hotspot_record = self.store.latest_hotspot_snapshot(symbol)
            hotspot = None
            if hotspot_record:
                from pa_agent.trading.topdown import HotspotSnapshot

                hotspot = HotspotSnapshot.model_validate(hotspot_record["snapshot"])
            theme_metrics = (
                self.ctx.hotspot_service.theme_metrics(hotspot)
                if hotspot is not None and getattr(self.ctx, "hotspot_service", None)
                else None
            )
            previous_record = self.store.latest_topdown_score(symbol)
            previous = (
                TopDownScoreSnapshot.model_validate(previous_record["snapshot"])
                if previous_record
                else None
            )
            jobs.append(
                {
                    "symbol": symbol,
                    "daily_signal": signal,
                    "pool_snapshot": universe,
                    "broker": self.broker_snapshot,
                    "hotspot": hotspot,
                    "previous_score": previous,
                    "sentiment": sentiment,
                    "theme_metrics": theme_metrics,
                    "captured_at": now,
                    "authorization_open": any(
                        item.get("symbol") == symbol
                        and item.get("status")
                        in {
                            "awaiting_user_confirmation",
                            "submitted",
                            "partially_filled",
                        }
                        for item in self.store.list_plans(symbol=symbol)
                    ),
                }
            )
        if not jobs:
            self.updated.emit()
            return
        thread = QThread(self)
        worker = _TopDownBatchWorker(service, jobs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.score_ready.connect(self._store_topdown_score)
        worker.failed.connect(lambda symbol, error: self._fail(f"topdown:{symbol}", error))
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.updated.emit)
        self._hold_thread("topdown", thread, worker)
        self.status_changed.emit("topdown", f"正在计算{len(jobs)}只日线候选的15分钟评分")
        thread.start()

    def _store_topdown_score(self, score: Any, closed_stock_bar: Any = None) -> None:
        if closed_stock_bar is not None:
            self.ctx.trade_lifecycle.process_closed_bar(
                symbol=score.symbol,
                timeframe="15m",
                bar=closed_stock_bar,
            )
        self.store.add_topdown_score(score)
        if score.eligible_for_risk and not score.hard_blocks and not score.data_gaps:
            record = next(
                (
                    item
                    for item in self.store.list_quant_signals(
                        strategy_id=self.ctx.settings.strategy.strategy_id,
                        limit=1000,
                    )
                    if item.get("symbol") == score.symbol
                    and item.get("pool_version") == score.pool_version
                    and item.get("status") == "allow"
                ),
                None,
            )
            if record is not None:
                daily = SignalDecision.model_validate(record["decision"])
                self.ctx.quant_workflow.create_topdown_plan(daily, score)
        if score.status.value != "authorization_revoked":
            return
        for plan in self.store.list_plans(symbol=score.symbol):
            if plan.get("status") not in {
                "awaiting_user_confirmation",
                "submitted",
                "partially_filled",
            }:
                continue
            self.store.update_plan(plan["id"], status="invalidated")
            self.store.append_event(
                plan["id"],
                "topdown_authorization_revoked",
                details={
                    "score": score.total_score,
                    "hard_blocks": score.hard_blocks,
                    "bar_closed_at": score.bar_closed_at,
                },
            )

    def sync_daily_lifecycle(self) -> None:
        lifecycle = getattr(self.ctx, "trade_lifecycle", None)
        if (
            self._stopping
            or not self.store.available
            or lifecycle is None
            or self._thread_running("lifecycle")
        ):
            return
        from pa_agent.trading.lifecycle_sync import LifecycleMarketDataSync

        thread = QThread(self)
        worker = _LifecycleDailySyncWorker(
            LifecycleMarketDataSync(self.store, lifecycle)
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._daily_lifecycle_synced)
        worker.failed.connect(lambda error: self._fail("lifecycle", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("lifecycle", thread, worker)
        thread.start()

    def _daily_lifecycle_synced(self, result: Any) -> None:
        self.last_lifecycle_sync = result if isinstance(result, dict) else {}
        failures = self.last_lifecycle_sync.get("failures") or {}
        if failures:
            self.ctx.logger.warning("开放计划日线同步部分失败: %s", failures)
        self.updated.emit()

    def _fail(self, task: str, error: Any) -> None:
        message = str(error)
        self.ctx.logger.warning("量化常驻任务失败 %s: %s", task, message)
        self.task_failed.emit(task, message)
        self.status_changed.emit(task, f"失败关闭：{message}")
        self.updated.emit()
