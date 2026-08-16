"""Codex-only orchestration never falls back to another provider."""
from __future__ import annotations

from unittest.mock import MagicMock

import openai

from pa_agent.config.settings import Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from tests.fixtures.validators import schema_test_validator


def test_stream_chat_does_not_switch_provider_on_connection_error() -> None:
    settings = Settings()
    settings.provider.model = "openclaw"
    settings.provider.base_url = "http://127.0.0.1:53555/v1"

    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="Connection error."
    )

    orchestrator = TwoStageOrchestrator(
        client=client,
        assembler=MagicMock(),
        router=MagicMock(),
        validator=schema_test_validator(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )

    try:
        orchestrator._stream_chat_resilient(
            [{"role": "user", "content": "hi"}],
            on_reasoning_token=None,
            on_content_token=None,
            cancel_token=MagicMock(is_set=MagicMock(return_value=False)),
            thinking=True,
            reasoning_effort="max",
            stage_label="Stage 1",
        )
    except openai.APIConnectionError:
        pass
    assert client.stream_chat.call_count == 1
    assert not hasattr(orchestrator, "_try_qclaw_fallback")
    assert not hasattr(orchestrator, "_try_cursor_fallback")
    assert not hasattr(orchestrator, "_try_workbuddy_fallback")


def test_stream_chat_forwards_codex_reasoning_settings() -> None:
    settings = Settings()
    client = MagicMock()
    client.stream_chat.return_value = MagicMock()

    orchestrator = TwoStageOrchestrator(
        client=client,
        assembler=MagicMock(),
        router=MagicMock(),
        validator=schema_test_validator(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )

    cancel = MagicMock(is_set=MagicMock(return_value=False))
    orchestrator._stream_chat_resilient(
        [{"role": "user", "content": "hi"}],
        on_reasoning_token=None,
        on_content_token=None,
        cancel_token=cancel,
        thinking=True,
        reasoning_effort="high",
        stage_label="Stage 1",
    )
    assert client.stream_chat.call_count == 1
    assert client.stream_chat.call_args.kwargs["reasoning_effort"] == "high"
