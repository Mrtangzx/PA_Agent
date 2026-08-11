"""GUI wiring for the Codex SDK provider backend."""

from __future__ import annotations

from pa_agent.config.settings import Settings
from pa_agent.gui.ai_model_settings_dialog import AIModelSettingsDialog
from pa_agent.gui.settings_dialog import SettingsDialog


def _select_codex(dialog) -> None:
    index = dialog._backend_combo.findData("codex_sdk")
    assert index >= 0
    dialog._backend_combo.setCurrentIndex(index)


def _assert_codex_fields(dialog) -> None:
    assert dialog._model_edit.text() == "gpt-5.6-terra"
    assert not dialog._base_url_edit.isEnabled()
    assert not dialog._api_key_edit.isEnabled()
    assert "无需 API Key" in dialog._api_key_edit.placeholderText()


def test_ai_model_dialog_exposes_codex_backend(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    _select_codex(dialog)
    _assert_codex_fields(dialog)


def test_full_settings_dialog_exposes_codex_backend(qtbot) -> None:
    dialog = SettingsDialog(Settings())
    qtbot.addWidget(dialog)
    _select_codex(dialog)
    _assert_codex_fields(dialog)
