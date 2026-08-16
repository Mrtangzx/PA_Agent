"""AI 模型设置对话框 — 只包含 AI 提供商相关字段."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.settings import Settings, save_settings
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import normalize_codex_provider


class AIModelSettingsDialog(QDialog):
    """AI 模型 / 提供商配置对话框."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 模型设置")
        self.setMinimumWidth(520)
        self._settings = settings
        self._setup_ui()
        self._load_values()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        provider_group = QGroupBox("AI 提供商")
        form = QFormLayout(provider_group)

        self._backend_combo = QComboBox()
        self._backend_combo.addItem("Codex SDK（本机登录）", "codex_sdk")
        self._backend_combo.setEnabled(False)
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        form.addRow("运行方式:", self._backend_combo)

        self._model_edit = QLineEdit()
        form.addRow("模型 (model):", self._model_edit)

        self._base_url_edit = QLineEdit()
        form.addRow("Base URL:", self._base_url_edit)

        api_key_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        api_key_row.addWidget(self._api_key_edit)
        self._show_key_btn = QPushButton("隐藏")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(52)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_key_row.addWidget(self._show_key_btn)
        form.addRow("API Key:", api_key_row)

        self._thinking_check = QCheckBox("启用 Thinking")
        form.addRow("Thinking:", self._thinking_check)

        self._reasoning_effort_combo = QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high", "max"])
        form.addRow("Reasoning Effort:", self._reasoning_effort_combo)

        self._codex_help = QLabel(
            "所有模型访问统一通过 Codex SDK。认证使用本机 Codex 登录状态，"
            "PA Agent 不保存 API Key，也不使用第三方模型网关。"
        )
        self._codex_help.setWordWrap(True)
        form.addRow("说明:", self._codex_help)

        root.addWidget(provider_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.setText("保存")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── 加载 / 保存 ────────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        p = self._settings.provider
        self._model_edit.setText(p.model)
        self._base_url_edit.setText(p.base_url)
        self._api_key_edit.setText(p.api_key)
        self._thinking_check.setChecked(p.thinking)
        backend_idx = self._backend_combo.findData("codex_sdk")
        if backend_idx >= 0:
            self._backend_combo.blockSignals(True)
            self._backend_combo.setCurrentIndex(backend_idx)
            self._backend_combo.blockSignals(False)
        self._on_backend_changed()
        idx = self._reasoning_effort_combo.findText(p.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)

    def _on_save(self) -> None:
        p = self._settings.provider
        model = self._model_edit.text().strip()
        p.model = model or "gpt-5.6-terra"
        normalize_codex_provider(p)

        save_settings(self._settings, SETTINGS_JSON_PATH)
        self.accept()

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    def focus_api_key_field(self) -> None:
        if not self._api_key_edit.isEnabled():
            self._model_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            self._model_edit.selectAll()
            return
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def _on_backend_changed(self) -> None:
        if not self._model_edit.text().strip().lower().startswith("gpt-"):
            self._model_edit.setText("gpt-5.6-terra")
        self._base_url_edit.clear()
        self._api_key_edit.clear()
        self._base_url_edit.setEnabled(False)
        self._api_key_edit.setEnabled(False)
        self._show_key_btn.setEnabled(False)
        self._thinking_check.setChecked(True)
        self._thinking_check.setEnabled(False)
        high_index = self._reasoning_effort_combo.findText("high")
        self._reasoning_effort_combo.setCurrentIndex(high_index)
        self._reasoning_effort_combo.setEnabled(False)
        self._base_url_edit.setPlaceholderText("Codex SDK 不使用 Base URL")
        self._api_key_edit.setPlaceholderText("使用本机 Codex 登录状态，无需 API Key")
        self._backend_combo.setToolTip("生产环境只允许 Codex SDK")

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("显示")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("隐藏")
