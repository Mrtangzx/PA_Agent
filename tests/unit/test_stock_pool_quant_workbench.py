from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from pa_agent.config.settings import Settings
from pa_agent.gui.quant_workbench import QuantWorkbenchPage
from pa_agent.trading.stock_sandbox import StockSandboxState, StockTradingSandboxSnapshot
from pa_agent.trading.stock_selection import SelectionCandidate, StockSelectionSnapshot
from pa_agent.trading.store import TradeStore
from pa_agent.trading.universe import CurrentUniverseMember, UniverseSnapshot
from pa_agent.trading.workbench_models import SelectedStockContextController


def _seed(store: TradeStore) -> None:
    store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 14),
            version="cloud_ai_11_v1-2026-08",
            symbols=["600519", "300017"],
            members=[
                CurrentUniverseMember(
                    rank=1,
                    symbol="600519",
                    name="贵州茅台",
                    industry="白酒",
                    latest_price=1418.2,
                    average_amount_20=4_700_000_000,
                ),
                CurrentUniverseMember(
                    rank=2,
                    symbol="300017",
                    name="网宿科技",
                    industry="软件服务",
                    latest_price=13.27,
                    average_amount_20=1_100_000_000,
                ),
            ],
            source_kind="user_fixed_theme_universe",
            source_as_of=date(2026, 8, 14),
            input_member_count=2,
        ),
        source_updated_at="2026-08-14T15:00:00+08:00",
    )
    for symbol, name, state, score, priority in (
        ("600519", "贵州茅台", StockSandboxState.WAIT_CONFIRMATION, 72.0, 20),
        ("300017", "网宿科技", StockSandboxState.DAILY_REJECTED, None, 60),
    ):
        store.upsert_stock_sandbox(
            StockTradingSandboxSnapshot(
                symbol=symbol,
                name=name,
                pool_version="cloud_ai_11_v1-2026-08",
                observed_at="2026-08-14T10:15:01+08:00",
                market_session="trading",
                state=state,
                state_label="等待连续确认" if score is not None else "日线未通过",
                daily_status="通过" if score is not None else "未通过",
                score_status="wait_confirmation" if score is not None else "not_started",
                index_score=30 if score is not None else None,
                sentiment_score=20 if score is not None else None,
                theme_score=14 if score is not None else None,
                stock_score=8 if score is not None else None,
                total_score=score,
                consecutive_pass_count=1 if score is not None else 0,
                hotspot_status="已跟踪",
                action="等待下一根15分钟确认" if score is not None else "等待日线重评",
                action_priority=priority,
                input_hash=f"hash-{symbol}",
                latest_price=1418.2 if symbol == "600519" else 13.27,
            )
        )


def _context(store: TradeStore):
    settings = Settings()
    return SimpleNamespace(
        settings=settings,
        trade_store=store,
        quant_runtime=None,
        universe_service=None,
        broker_adapter=None,
        logger=SimpleNamespace(warning=lambda *_args: None),
    )


def test_personal_watchlist_is_separate_and_idempotent(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)

    store.upsert_watchlist_member(symbol="600519", name="贵州茅台", metadata={"industry": "白酒"})
    store.upsert_watchlist_member(symbol="600519", name="贵州茅台", metadata={"industry": "白酒"})

    assert len(store.list_watchlist_members()) == 1
    assert store.list_universe_snapshots(limit=10)[0]["version"] == ("cloud_ai_11_v1-2026-08")
    assert store.remove_watchlist_member("600519")
    assert store.list_watchlist_members() == []
    assert store.list_watchlist_members(active_only=False)[0]["active"] is False


def test_controller_keeps_one_symbol_context_when_rows_reorder(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    controller = SelectedStockContextController(_context(store))
    controller.select_symbol("300017")
    before = controller.view_model

    controller.reload(scope="sandboxes")
    after = controller.view_model

    assert before is not None and after is not None
    assert before.selected.symbol == "300017"
    assert after.selected.symbol == "300017"
    assert after.selected.sandbox["symbol"] == "300017"
    assert after.selected.sandbox["total_score"] is None


def test_workbench_has_four_workspaces_and_selected_stock_drives_all_panels(
    qtbot, tmp_path
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    page = QuantWorkbenchPage(_context(store))
    qtbot.addWidget(page)
    page.resize(1205, 689)
    page.show()
    qtbot.wait(20)

    assert page.workspace_stack.count() == 4
    assert [button.text() for button in page.nav_buttons] == [
        "实时监控", "交易账户", "智能选股", "系统验证"
    ]
    assert page.controller.selected_symbol == "600519"
    assert "贵州茅台" in page.stock_title.text()
    assert "等待连续确认" in page.stage_badge.text()
    assert page.monitor_splitter.count() == 3
    assert page.plan_panel.isVisible()
    assert not page.primary_action.isVisible()

    page.controller.select_symbol("300017")
    assert "网宿科技" in page.stock_title.text()
    assert "日线未通过" in page.stage_badge.text()
    assert "600519" not in page.stock_title.text()


def test_unavailable_action_is_explained_instead_of_disabled_button(qtbot, tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    page = QuantWorkbenchPage(_context(store))
    qtbot.addWidget(page)
    page.show()

    page.controller.select_symbol("600519")

    assert "15分钟" in page.next_condition.text()
    assert "确认" in page.next_condition.text()
    assert not page.primary_action.isVisible()


def test_compact_window_switches_plan_inside_current_workbench(qtbot, tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    page = QuantWorkbenchPage(_context(store))
    qtbot.addWidget(page)
    page.resize(900, 689)
    page.show()
    qtbot.wait(20)

    assert page.compact_plan_button.isVisible()
    assert page.stock_panel.isVisible()
    assert not page.plan_panel.isVisible()

    page.compact_plan_button.click()

    assert not page.stock_panel.isVisible()
    assert page.plan_panel.isVisible()
    assert "返回股票沙箱" in page.compact_plan_button.text()


def test_reconciliation_window_is_owned_and_stopped_by_new_workbench(
    qtbot, tmp_path
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    runtime = SimpleNamespace(
        started_ids=[],
        ended_ids=[],
        begin_reconciliation=lambda plan_id: runtime.started_ids.append(plan_id),
        end_reconciliation=lambda plan_id: runtime.ended_ids.append(plan_id),
    )
    ctx = _context(store)
    page = QuantWorkbenchPage(ctx)
    qtbot.addWidget(page)
    ctx.quant_runtime = runtime
    order = SimpleNamespace(plan_id="plan-60-second-window")

    page._start_reconciliation(order)

    assert page._reconciliation_timer.isActive()
    assert runtime.started_ids == ["plan-60-second-window"]

    page.shutdown()

    assert not page._reconciliation_timer.isActive()
    assert runtime.ended_ids == ["plan-60-second-window"]


def test_monthly_page_exposes_cash_flow_and_attribution_gaps(qtbot, tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    page = QuantWorkbenchPage(_context(store))
    qtbot.addWidget(page)
    page.show()

    text = page.monthly_text.toPlainText()

    assert "扣除出入金后的月度收益" in text
    assert "资金流水完整性：未核验" in text
    assert "正常策略" in text
    assert "池外例外" in text
    assert "外部手工交易" in text


def test_ths_watchlist_is_a_separate_view_and_personal_removal_keeps_ths_source(
    qtbot, tmp_path
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    store.upsert_watchlist_member(
        symbol="600519",
        name="贵州茅台",
        source="user_watchlist",
        metadata={"industry": "白酒"},
    )
    store.upsert_watchlist_member(
        symbol="600519",
        name="贵州茅台",
        source="ths_watchlist",
        metadata={
            "ths_categories": ["趋势", "主力资金"],
            "ths_reason": "下一个交易日进入15分钟观察",
        },
    )
    page = QuantWorkbenchPage(_context(store))
    qtbot.addWidget(page)
    page.show()

    ths_index = page.pool_view_combo.findData("ths_watchlist")
    page.pool_view_combo.setCurrentIndex(ths_index)
    page.controller.select_symbol("600519")

    assert ths_index >= 0
    assert page.pool_tree.topLevelItemCount() == 1
    assert "趋势/主力资金" in page.pool_tree.topLevelItem(0).text(0)
    assert "同花顺自选分类" in page.score_details.toPlainText()
    assert page.watch_remove_button.isVisible()

    page._remove_watchlist()

    member = store.list_watchlist_members(source="ths_watchlist")[0]
    sources = {item["source"]: item for item in member["sources"]}
    assert sources["user_watchlist"]["active"] is False
    assert sources["ths_watchlist"]["active"] is True


def test_smart_selection_is_single_window_and_candidate_joins_watchlist(
    qtbot, tmp_path
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    candidate = SelectionCandidate(
        symbol="000001",
        name="平安银行",
        status="eligible",
        strategy_tags=["main_force_theme", "trend_start"],
        themes=["银行"],
        score=52,
        latest_price=12.34,
        pct_change=1.28,
        evidence={
            "negative_news_check": "passed",
            "theme_main_net_inflow_pct": 1.4,
            "latest_volume_ratio_20": 1.35,
        },
        source_timestamps={
            "hotspot": "2026-08-15T10:30:00+08:00",
            "daily_bar": "2026-08-14T15:00:00+08:00",
        },
        input_hash="candidate-hash",
    )
    store.add_stock_selection_snapshot(
        StockSelectionSnapshot(
            generated_at="2026-08-15T10:30:00+08:00",
            status="complete",
            scanned_count=48,
            candidate_count=1,
            candidates=[candidate],
            results=[candidate],
            strategy_counts={"main_force_theme": 1, "trend_start": 1},
            input_hash="selection-hash",
        )
    )
    page = QuantWorkbenchPage(_context(store))
    qtbot.addWidget(page)
    page.show()

    page.navigate("selection")

    assert page.workspace_stack.currentWidget() is page.selection_workspace
    assert page.selection_table.rowCount() == 1
    assert page.controller.selected_symbol == "000001"
    assert "重大负面核验：已通过" in page.selection_detail.toPlainText()
    assert page.selection_add_button.isVisible()

    page.selection_add_button.click()

    member = next(
        item for item in store.list_watchlist_members(active_only=False)
        if item["symbol"] == "000001"
    )
    assert member is not None
    assert any(
        source["source"] == "user_watchlist" and source["active"]
        for source in member["sources"]
    )
    assert "监控池" in page.selection_feedback.text()
