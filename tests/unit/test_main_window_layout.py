from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtWidgets import QMenu

from pa_agent.config.settings import Settings
from pa_agent.gui.main_window import MainWindow, _bounded_initial_window_size


def test_initial_window_uses_desktop_size_on_large_screen() -> None:
    assert _bounded_initial_window_size(1920, 1080) == (1440, 900)


def test_initial_window_stays_inside_dpi_scaled_work_area() -> None:
    width, height = _bounded_initial_window_size(1229, 737)

    assert (width, height) == (1205, 689)
    assert width < 1229
    assert height < 737


def test_home_groups_configuration_actions_under_system_settings(qtbot) -> None:
    settings = Settings()
    ctx = SimpleNamespace(
        settings=settings,
        trade_store=None,
        broker_adapter=None,
        quant_runtime=None,
    )

    with (
        patch.object(MainWindow, "_build_workbench", return_value=QMenu()),
        patch.object(MainWindow, "_connect_event_bus"),
        patch.object(MainWindow, "_connect_quant_runtime"),
        patch.object(MainWindow, "_update_ai_mode_label"),
        patch.object(MainWindow, "_sync_submit_button_state"),
        patch.object(MainWindow, "_refresh_api_key_ui_state"),
    ):
        window = MainWindow(ctx)
    qtbot.addWidget(window)

    top_level_labels = [action.text() for action in window.menuBar().actions()]
    assert "系统设置" in top_level_labels
    assert "AI 模型设置" not in top_level_labels
    assert "飞书发送通知设置" not in top_level_labels
    assert "其他通用设置" not in top_level_labels
    assert "智能选股" in top_level_labels

    settings_actions = [
        action.text()
        for action in window._system_settings_menu.actions()
        if not action.isSeparator()
    ]
    assert settings_actions == [
        "AI 模型设置",
        "飞书发送通知设置",
        "其他通用设置",
    ]
