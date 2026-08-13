"""Application context wiring shared resources without global singletons."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppContext:
    """Carries shared resources to GUI widgets and orchestrators."""

    settings: Any = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pa_agent"))
    event_bus: Any = None

    # Data layer
    data_source: Any = None       # DataSource implementation

    # AI / orchestration layer
    client: Any = None            # DeepSeekClient
    assembler: Any = None         # PromptAssembler
    router: Any = None            # route_strategy_files callable
    validator: Any = None         # JsonValidator
    pending_writer: Any = None    # PendingWriter
    exp_reader: Any = None        # ExperienceReader
    ledger: Any = None            # SessionTokenLedger

    # Local trading workflow. Initialization failure must never block analysis.
    trade_store: Any = None       # TradeStore
    trading_service: Any = None   # TradingService
    trade_lifecycle: Any = None   # TradeLifecycleTracker
    broker_trade_lifecycle: Any = None # Unified TradeLifecycle
    quant_strategy: Any = None    # Hs300DailyPullbackStrategy
    quant_workflow: Any = None    # QuantTradingWorkflow
    topdown_scoring: Any = None   # TopDownScoring
    hotspot_service: Any = None   # HotspotService
    topdown_market_data_service: Any = None # Time-aligned 15m data orchestration
    universe_service: Any = None # User-defined fixed current trading universe
    daily_candidate_scanner: Any = None # Closed-daily deterministic pool scanner
    market_sentiment_service: Any = None # Structured full-market sentiment collector
    portfolio_risk: Any = None    # PortfolioRisk
    strategy_stability: Any = None # StrategyStabilityController
    strategy_promotion: Any = None # Evidence-derived promotion workflow
    broker_adapter: Any = None    # ThsBrokerAdapter
    quant_runtime: Any = None     # Application-scoped QuantRuntimeCoordinator

    @classmethod
    def bootstrap(cls) -> AppContext:
        """Wire all real components and return a fully initialised AppContext."""
        from pa_agent.ai.json_validator import JsonValidator
        from pa_agent.ai.prompt_assembler import PromptAssembler
        from pa_agent.ai.router import route_strategy_files
        from pa_agent.ai.session_ledger import SessionTokenLedger
        from pa_agent.config.paths import (
            EXPERIENCE_DIR,
            PROMPT_DIR,
            RECORDS_PENDING_DIR,
            SETTINGS_JSON_PATH,
        )
        from pa_agent.config.settings import load_settings
        from pa_agent.data.factory import create_data_source, normalize_data_source_kind
        from pa_agent.records.experience_reader import ExperienceReader
        from pa_agent.records.pending_writer import PendingWriter
        from pa_agent.util.event_bus import EventBus
        from pa_agent.util.logging import configure_logging

        # ── Settings ──────────────────────────────────────────────────────────
        settings = load_settings(SETTINGS_JSON_PATH)
        from pa_agent.ai.cursor_connector import sync_cursor_provider_on_load
        from pa_agent.ai.qclaw_connector import sync_qclaw_agent_provider_on_load
        from pa_agent.ai.workbuddy_connector import sync_workbuddy_provider_on_load

        sync_qclaw_agent_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)
        sync_workbuddy_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)
        sync_cursor_provider_on_load(settings, save_path=SETTINGS_JSON_PATH)

        # ── Logging (with API key masking) ────────────────────────────────────
        configure_logging(api_key=settings.provider.api_key)

        app_logger = logging.getLogger("pa_agent")

        # ── Event bus ─────────────────────────────────────────────────────────
        event_bus = EventBus()

        # ── Data layer ────────────────────────────────────────────────────────
        from pa_agent.data.kline_adjust import apply_kline_adjust_from_settings

        apply_kline_adjust_from_settings(settings)
        ds_kind = normalize_data_source_kind(
            getattr(settings.general, "last_data_source", "mt5")
        )
        data_source = create_data_source(ds_kind)

        # Subscribe to the last-used symbol/timeframe from settings
        try:
            data_source.connect()
            if ds_kind == "tradingview":
                from pa_agent.data.tradingview import TradingViewSource

                if isinstance(data_source, TradingViewSource):
                    # Use saved exchange setting, default to auto (empty).
                    saved_exchange = getattr(settings.general, 'last_tradingview_exchange', '') or ''
                    data_source.set_exchange(saved_exchange)
            data_source.subscribe(
                settings.general.last_symbol,
                settings.general.last_timeframe,
            )
            app_logger.info(
                "Data source %s subscribed to %s %s",
                ds_kind,
                settings.general.last_symbol,
                settings.general.last_timeframe,
            )
        except Exception as exc:  # noqa: BLE001
            app_logger.warning("Initial data source subscription failed: %s", exc)

        # ── AI client ─────────────────────────────────────────────────────────
        from pa_agent.ai.client_factory import create_ai_client

        client = create_ai_client(settings.provider, logger_=app_logger)

        # ── Prompt assembler ──────────────────────────────────────────────────
        exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=app_logger)
        assembler = PromptAssembler(
            prompt_dir=PROMPT_DIR,
            experience_reader=exp_reader,
            prompt_settings=settings.prompt,
        )

        # ── Validator & router ────────────────────────────────────────────────
        validator = JsonValidator(settings)
        router = route_strategy_files

        # ── Pending writer ────────────────────────────────────────────────────
        pending_writer = PendingWriter(
            pending_dir=RECORDS_PENDING_DIR,
            event_bus=event_bus,
            api_key=settings.provider.api_key,
        )

        # ── Session ledger ────────────────────────────────────────────────────
        ledger = SessionTokenLedger(
            context_window=settings.provider.context_window,
            warn_pct=settings.general.context_warning_threshold_pct,
        )

        from pa_agent.brokers.ths_adapter import ThsBrokerAdapter
        from pa_agent.config.paths import TRADE_RECORDS_DIR, TRADES_DB_PATH
        from pa_agent.trading.daily_candidates import DailyCandidateScanner
        from pa_agent.trading.hotspots import HotspotService
        from pa_agent.trading.lifecycle import TradeLifecycleTracker
        from pa_agent.trading.market_sentiment import MarketSentimentService
        from pa_agent.trading.portfolio import PortfolioRisk
        from pa_agent.trading.promotion import StrategyPromotionService
        from pa_agent.trading.quant import Hs300DailyPullbackStrategy
        from pa_agent.trading.quant_workflow import QuantTradingWorkflow
        from pa_agent.trading.service import TradingService
        from pa_agent.trading.stability import StrategyStabilityController
        from pa_agent.trading.store import TradeStore
        from pa_agent.trading.strategy_validation import run_fixed_mechanism_validation
        from pa_agent.trading.topdown import TopDownScoring
        from pa_agent.trading.topdown_market_data import TopDownMarketDataService
        from pa_agent.trading.trade_lifecycle import TradeLifecycle
        from pa_agent.trading.universe import FixedCloudAiUniverseService

        trade_store = TradeStore(TRADES_DB_PATH, legacy_dir=TRADE_RECORDS_DIR)
        if trade_store.available:
            try:
                fixed_validation = run_fixed_mechanism_validation()
                trade_store.add_validation_run(
                    fixed_validation,
                    dataset="fixed_replay",
                    promotion_eligible=False,
                )
            except Exception as exc:  # noqa: BLE001
                app_logger.warning("固定机制回放执行或保存失败: %s", exc)
        trading_service = TradingService(trade_store, settings.risk, research_only=True)
        trade_lifecycle = TradeLifecycleTracker(trade_store)
        quant_strategy = Hs300DailyPullbackStrategy(settings.strategy)
        quant_workflow = QuantTradingWorkflow(trade_store, quant_strategy)
        topdown_scoring = TopDownScoring(settings.topdown_scoring)
        hotspot_service = HotspotService()
        topdown_market_data_service = TopDownMarketDataService(topdown_scoring)
        universe_service = FixedCloudAiUniverseService()
        daily_candidate_scanner = DailyCandidateScanner(quant_strategy)
        market_sentiment_service = MarketSentimentService()
        portfolio_risk = PortfolioRisk(settings.risk, settings.portfolio_risk)
        strategy_stability = StrategyStabilityController()
        strategy_promotion = StrategyPromotionService(trade_store)
        broker_adapter = ThsBrokerAdapter(settings.ths)
        broker_trade_lifecycle = TradeLifecycle(trade_lifecycle, broker_adapter)
        broker_state = broker_adapter.connect()
        app_logger.info("同花顺连接状态: %s - %s", broker_state.status, broker_state.message)
        if broker_state.usable:
            try:
                broker_snapshot = broker_adapter.snapshot()
                if trade_store.available:
                    trade_store.add_broker_snapshot(broker_snapshot)
                    if broker_snapshot.complete and broker_snapshot.total_equity is not None:
                        trade_store.record_broker_financial_snapshot(broker_snapshot)
            except Exception as exc:  # noqa: BLE001
                app_logger.warning("启动时同花顺只读同步失败: %s", exc)
        if not trade_store.available:
            app_logger.error("交易数据库不可用（分析功能继续）: %s", trade_store.error)

        ctx = cls(
            settings=settings,
            logger=app_logger,
            event_bus=event_bus,
            data_source=data_source,
            client=client,
            assembler=assembler,
            router=router,
            validator=validator,
            pending_writer=pending_writer,
            exp_reader=exp_reader,
            ledger=ledger,
            trade_store=trade_store,
            trading_service=trading_service,
            trade_lifecycle=trade_lifecycle,
            broker_trade_lifecycle=broker_trade_lifecycle,
            quant_strategy=quant_strategy,
            quant_workflow=quant_workflow,
            topdown_scoring=topdown_scoring,
            hotspot_service=hotspot_service,
            topdown_market_data_service=topdown_market_data_service,
            universe_service=universe_service,
            daily_candidate_scanner=daily_candidate_scanner,
            market_sentiment_service=market_sentiment_service,
            portfolio_risk=portfolio_risk,
            strategy_stability=strategy_stability,
            strategy_promotion=strategy_promotion,
            broker_adapter=broker_adapter,
        )
        # The coordinator is application-scoped but is started by main() only
        # after QApplication and the visible main window exist.  Keeping it on
        # the context makes opening/closing the workbench presentation-neutral.
        from pa_agent.trading.quant_runtime import QuantRuntimeCoordinator

        ctx.quant_runtime = QuantRuntimeCoordinator(ctx)
        return ctx
