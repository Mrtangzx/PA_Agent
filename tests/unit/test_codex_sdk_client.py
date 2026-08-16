"""Tests for the OpenAI Codex SDK adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.ai.codex_sdk_client import (
    CodexSdkClient,
    _HiddenConsoleSubprocessProxy,
    _messages_to_prompt,
    _resolve_effort,
    _resolve_model,
)
from pa_agent.ai.deepseek_client import CancelledError
from pa_agent.config.settings import AIProviderSettings


class _FakeTurn:
    id = "turn-1"

    def __init__(self) -> None:
        self.interrupted = False

    def interrupt(self) -> None:
        self.interrupted = True

    def stream(self):
        yield SimpleNamespace(
            method="item/reasoning/summaryTextDelta",
            payload=SimpleNamespace(delta="结构分析完成"),
        )
        yield SimpleNamespace(
            method="item/agentMessage/delta",
            payload=SimpleNamespace(delta='{"ok":'),
        )
        yield SimpleNamespace(
            method="item/agentMessage/delta",
            payload=SimpleNamespace(delta="true}"),
        )
        yield SimpleNamespace(
            method="thread/tokenUsage/updated",
            payload=SimpleNamespace(
                token_usage=SimpleNamespace(
                    last=SimpleNamespace(
                        input_tokens=10,
                        cached_input_tokens=2,
                        output_tokens=4,
                        total_tokens=14,
                    )
                )
            ),
        )
        yield SimpleNamespace(
            method="item/completed",
            payload=SimpleNamespace(item=SimpleNamespace(type="agentMessage", text='{"ok":true}')),
        )
        yield SimpleNamespace(
            method="turn/completed",
            payload=SimpleNamespace(
                turn=SimpleNamespace(
                    status=SimpleNamespace(value="completed"),
                    error=None,
                )
            ),
        )


class _FakeThread:
    id = "thread-1"
    last_turn_kwargs = None
    last_prompt = None

    def turn(self, prompt, **kwargs):
        type(self).last_prompt = prompt
        type(self).last_turn_kwargs = kwargs
        return _FakeTurn()


class _FakeCodex:
    last_start_kwargs = None
    closed = False

    def __init__(self, _config) -> None:
        type(self).closed = False

    def thread_start(self, **kwargs):
        type(self).last_start_kwargs = kwargs
        return _FakeThread()

    def close(self) -> None:
        type(self).closed = True


def test_helper_normalization() -> None:
    assert _resolve_model("codex") == "gpt-5.6-terra"
    assert _resolve_model("gpt-5.6-sol") == "gpt-5.6-sol"
    assert _resolve_effort(thinking=False, effort="max") == "none"
    assert _resolve_effort(thinking=True, effort="max") == "xhigh"
    assert "[system]\nrules" in _messages_to_prompt(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "data"}]
    )


def test_stream_chat_maps_codex_events(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai_codex

    monkeypatch.setattr(openai_codex, "Codex", _FakeCodex)
    settings = AIProviderSettings(
        backend="codex_sdk",
        model="gpt-5.6-terra",
        thinking=True,
        reasoning_effort="max",
    )
    reasoning_chunks: list[str] = []
    content_chunks: list[str] = []

    reply = CodexSdkClient(settings).stream_chat(
        [{"role": "user", "content": "return json"}],
        on_reasoning_token=reasoning_chunks.append,
        on_content_token=content_chunks.append,
        timeout_s=1,
    )

    assert reply.content == '{"ok":true}'
    assert reply.reasoning_content == "结构分析完成"
    assert reply.request_id == "turn-1"
    assert reply.usage.prompt_tokens == 10
    assert reply.usage.cached_prompt_tokens == 2
    assert reply.usage.completion_tokens == 4
    assert reply.usage.total_tokens == 14
    assert reasoning_chunks == ["结构分析完成"]
    assert content_chunks == ['{"ok":', "true}"]
    assert _FakeThread.last_turn_kwargs["effort"] == "xhigh"
    assert _FakeCodex.last_start_kwargs["ephemeral"] is True
    assert _FakeCodex.closed is True


def test_cancelled_before_codex_start() -> None:
    settings = AIProviderSettings(backend="codex_sdk", model="gpt-5.6-terra")
    cancel = SimpleNamespace(is_set=lambda: True)
    with pytest.raises(CancelledError):
        CodexSdkClient(settings).stream_chat([], cancel_token=cancel)


def test_hidden_console_proxy_preserves_existing_creation_flags() -> None:
    calls: list[dict] = []

    class _FakeSubprocess:
        PIPE = object()

        @staticmethod
        def Popen(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return "process"

    proxy = _HiddenConsoleSubprocessProxy(
        _FakeSubprocess,
        create_no_window_flag=0x08000000,
    )

    result = proxy.Popen(["codex.exe"], creationflags=0x00000200)

    assert result == "process"
    assert calls[0]["kwargs"]["creationflags"] == 0x08000200
    assert proxy.PIPE is _FakeSubprocess.PIPE
