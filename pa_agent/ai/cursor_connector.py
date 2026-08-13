"""Cursor route (model alias ``openclaw_cs``).

This route uses the Cursor SDK directly (requires a Cursor API key).

Model field conventions
-----------------------

- ``openclaw_cs``: use Cursor with server-chosen model (``auto``).
- ``openclaw_cs/<cursorModelId>``: pin a specific Cursor model id, e.g.
  ``openclaw_cs/composer-2.5``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CURSOR_MODEL = "openclaw_cs"
_CURSOR_DEFAULT_MODEL_ID = "auto"


def is_openclaw_cs_model(model: str | None) -> bool:
    """True when the user selected the Cursor SDK route."""
    m = (model or "").strip().lower()
    if not m:
        return False
    return m == _CURSOR_MODEL or m.startswith(f"{_CURSOR_MODEL}/")


def resolve_cursor_sdk_model_id(model: str | None) -> str:
    """Resolve Cursor SDK model id from ``openclaw_cs`` alias.

    ``openclaw_cs/<cursorModelId>`` -> ``<cursorModelId>``
    ``openclaw_cs`` -> ``auto``
    """
    raw = (model or "").strip()
    if not is_openclaw_cs_model(raw):
        return _CURSOR_DEFAULT_MODEL_ID
    suffix = raw[len(_CURSOR_MODEL) :].lstrip("/")
    return suffix or _CURSOR_DEFAULT_MODEL_ID


def should_use_cursor_provider(
    model: str | None,
    base_url: str | None = None,
) -> bool:
    """True when settings Save should treat this as Cursor SDK route."""
    del base_url
    return is_openclaw_cs_model(model)


def is_cursor_agent_route(model: str | None) -> bool:
    """True when API calls should use the Cursor SDK route."""
    return is_openclaw_cs_model(model)


def sync_cursor_provider_on_load(
    settings: Any,
    *,
    save_path: Any | None = None,
) -> None:
    """No-op for Cursor SDK route (no gateway autodetect)."""
    del settings, save_path


def apply_cursor_provider_to_settings(
    settings: Any,
    *,
    preferred_model: str | None = None,
) -> str | None:
    """Validate *settings.provider* for Cursor SDK route.

    Returns None on success, or a user-facing error string.
    """
    model_hint = (preferred_model or getattr(settings.provider, "model", "") or "").strip()
    if not is_openclaw_cs_model(model_hint):
        return "Cursor 路由模型必须为 openclaw_cs 或 openclaw_cs/<modelId>。"
    provider = settings.provider
    key = str(getattr(provider, "api_key", "") or "").strip()
    if key.startswith("crsr_"):
        provider.backend = "cursor_sdk"
        provider.model = model_hint
        provider.base_url = ""
        return None

    # Backward-compatible local QClaw route.  This branch intentionally ignores
    # user-entered OpenAI URLs/keys and uses the detected gateway fingerprint.
    # A blank key remains an explicit configuration error, preventing a bare
    # alias from silently changing provider authority.
    if not key:
        return "Cursor 路由需要 crsr_ API Key，或先配置本地 QClaw Gateway。"
    from pa_agent.ai.qclaw_connector import (
        detect_qclaw,
        qclaw_health_check_base,
        qclaw_provider_settings,
    )

    if not detect_qclaw():
        return "Cursor API Key 必须以 crsr_ 开头，且未检测到可用的 QClaw Gateway。"
    resolved = qclaw_provider_settings(model=model_hint)
    if resolved is None:
        return "QClaw 配置读取失败。"
    provider.backend = "openai_compatible"
    provider.model = resolved.model
    provider.base_url = resolved.base_url
    provider.api_key = resolved.api_key
    provider.context_window = resolved.context_window
    ok, message = qclaw_health_check_base(provider.base_url, provider.api_key)
    if not ok:
        return f"QClaw 连通性检查失败：\n\n{message}"
    return None
