"""Construct PA Agent's single production model client: Codex SDK."""

from __future__ import annotations

import logging
from typing import Any

from pa_agent.config.settings import AIProviderSettings, normalize_codex_provider


def create_ai_client(
    settings: AIProviderSettings,
    logger_: logging.Logger | None = None,
) -> Any:
    """Normalize legacy settings and always construct ``CodexSdkClient``."""
    log = logger_ or logging.getLogger(__name__)
    if normalize_codex_provider(settings):
        log.warning("Legacy AI provider settings migrated to the Codex SDK route")

    from pa_agent.ai.codex_sdk_client import CodexSdkClient

    log.info("AI client route: Codex SDK only (model=%s)", settings.model)
    return CodexSdkClient(settings=settings, logger_=log)
