"""OpenAI Codex SDK-backed analysis client for PA Agent.

The adapter keeps the same ``stream_chat`` contract used by the two-stage
orchestrator while running each request in an ephemeral, read-only Codex
thread.  Codex authentication comes from the user's local Codex account.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pa_agent.ai.model_types import AIReply, AIUsage, CancelledError
from pa_agent.config.settings import AIProviderSettings

logger = logging.getLogger(__name__)

_DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
_CODEX_PROCESS_LAUNCH_LOCK = threading.Lock()
_CODEX_AGENT_INSTRUCTIONS = """\
You are the analysis model embedded in PA Agent, not a coding assistant for this turn.
Use only the market data and instructions supplied in the user prompt.
Do not call tools, inspect the workspace, run commands, browse, or modify files.
Return the complete requested answer directly. When the prompt requests JSON,
return only valid JSON with no Markdown fence or surrounding commentary.
"""


def _default_workspace() -> str:
    return os.environ.get("PA_AGENT_ROOT") or str(Path(__file__).resolve().parents[2])


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten OpenAI-style chat messages into one self-contained Codex prompt."""
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            text_chunks: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("text", "input_text"):
                    text_chunks.append(str(block.get("text", "")))
            content = "\n".join(text_chunks)
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts).strip()


def _resolve_model(model: str | None) -> str:
    value = (model or "").strip()
    if value.lower() in ("", "codex", "codex-sdk", "openai-codex"):
        return _DEFAULT_CODEX_MODEL
    return value


def _resolve_effort(*, thinking: bool, effort: str | None) -> str:
    if not thinking:
        return "none"
    value = (effort or "high").strip().lower()
    if value == "max":
        return "xhigh"
    if value in ("minimal", "low", "medium", "high", "xhigh"):
        return value
    return "high"


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status) or "")


def _error_text(error: Any) -> str:
    if error is None:
        return "unknown Codex error"
    message = getattr(error, "message", None)
    if message:
        return str(message)
    return str(error)


class _HiddenConsoleSubprocessProxy:
    """Delegate to subprocess while adding CREATE_NO_WINDOW to Popen calls."""

    def __init__(self, base: Any, *, create_no_window_flag: int) -> None:
        self._base = base
        self._create_no_window_flag = int(create_no_window_flag)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def Popen(self, *args: Any, **kwargs: Any) -> Any:
        existing = int(kwargs.get("creationflags", 0) or 0)
        kwargs["creationflags"] = existing | self._create_no_window_flag
        return self._base.Popen(*args, **kwargs)


@contextmanager
def _codex_process_launch_context(*, hide_console_on_windows: bool):
    """Apply the configured Windows console policy only while Codex starts."""

    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if not hide_console_on_windows or os.name != "nt" or not create_no_window:
        yield
        return

    import openai_codex.client as sdk_client_module

    with _CODEX_PROCESS_LAUNCH_LOCK:
        original_subprocess = sdk_client_module.subprocess
        sdk_client_module.subprocess = _HiddenConsoleSubprocessProxy(
            original_subprocess,
            create_no_window_flag=create_no_window,
        )
        try:
            yield
        finally:
            sdk_client_module.subprocess = original_subprocess


class CodexSdkClient:
    """Run PA Agent prompts through the local OpenAI Codex SDK runtime."""

    def __init__(self, settings: AIProviderSettings, logger_: logging.Logger | None = None) -> None:
        self._settings = settings
        self._log = logger_ or logger

    def update_provider(self, settings: AIProviderSettings) -> None:
        self._settings = settings

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_content_token: Callable[[str], None] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        cancel_token: Any | None = None,
        timeout_s: float = 600.0,
    ) -> AIReply:
        """Run an ephemeral read-only Codex turn and map its events to callbacks."""
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before Codex call")

        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
            from openai_codex.generated.v2_all import ReasoningSummary
        except Exception as exc:
            raise RuntimeError(
                "openai-codex 未安装或导入失败。请先安装依赖: pip install openai-codex"
            ) from exc

        prompt = _messages_to_prompt(messages)
        model = _resolve_model(self._settings.model)
        thinking_on = self._settings.thinking if thinking is None else bool(thinking)
        effort = _resolve_effort(
            thinking=thinking_on,
            effort=reasoning_effort or self._settings.reasoning_effort,
        )
        cwd = _default_workspace()

        self._log.info(
            "CodexSdkClient.stream_chat: model=%s effort=%s chars=%d cwd=%s",
            model,
            effort,
            len(prompt),
            cwd,
        )

        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        completed_item_content = ""
        completed_item_reasoning = ""
        usage = AIUsage()
        turn_status = ""
        turn_error: Any = None
        thread_id = ""
        turn_id = ""
        timed_out = threading.Event()
        watcher_stop = threading.Event()
        timer: threading.Timer | None = None
        watcher: threading.Thread | None = None
        t0 = time.monotonic()

        hide_console = self._settings.codex_process.hide_console_on_windows
        with _codex_process_launch_context(hide_console_on_windows=hide_console):
            codex = Codex(CodexConfig(cwd=cwd))
        turn: Any = None
        try:
            thread = codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=cwd,
                developer_instructions=_CODEX_AGENT_INSTRUCTIONS,
                ephemeral=True,
                model=model,
                sandbox=Sandbox.read_only,
            )
            thread_id = str(thread.id)
            turn = thread.turn(
                prompt,
                approval_mode=ApprovalMode.deny_all,
                effort=effort,
                sandbox=Sandbox.read_only,
                summary=ReasoningSummary("detailed") if thinking_on else ReasoningSummary("none"),
            )
            turn_id = str(turn.id)

            def _interrupt_for_timeout() -> None:
                timed_out.set()
                try:
                    turn.interrupt()
                except Exception:
                    self._log.debug("Codex timeout interrupt failed", exc_info=True)

            if timeout_s > 0:
                timer = threading.Timer(timeout_s, _interrupt_for_timeout)
                timer.daemon = True
                timer.start()

            if cancel_token is not None:

                def _watch_cancel() -> None:
                    while not watcher_stop.wait(0.05):
                        if cancel_token.is_set():
                            try:
                                turn.interrupt()
                            except Exception:
                                self._log.debug(
                                    "Codex cancellation interrupt failed", exc_info=True
                                )
                            return

                watcher = threading.Thread(
                    target=_watch_cancel,
                    name="pa_agent_codex_cancel",
                    daemon=True,
                )
                watcher.start()

            for event in turn.stream():
                method = str(getattr(event, "method", ""))
                payload = getattr(event, "payload", None)

                if method == "item/agentMessage/delta":
                    chunk = str(getattr(payload, "delta", "") or "")
                    if chunk:
                        content_parts.append(chunk)
                        if on_content_token is not None:
                            on_content_token(chunk)
                    continue

                if method == "item/reasoning/summaryTextDelta":
                    chunk = str(getattr(payload, "delta", "") or "")
                    if chunk:
                        reasoning_parts.append(chunk)
                        if on_reasoning_token is not None:
                            on_reasoning_token(chunk)
                    continue

                if method == "item/completed":
                    item = getattr(payload, "item", None)
                    item_type = str(getattr(item, "type", ""))
                    if item_type == "agentMessage":
                        completed_item_content = str(getattr(item, "text", "") or "")
                    elif item_type == "reasoning" and not completed_item_reasoning:
                        summary_parts = getattr(item, "summary", None) or []
                        content_values = getattr(item, "content", None) or []
                        completed_item_reasoning = "\n".join(
                            str(part) for part in (summary_parts or content_values) if part
                        )
                    continue

                if method == "thread/tokenUsage/updated":
                    token_usage = getattr(payload, "token_usage", None)
                    last = getattr(token_usage, "last", None)
                    if last is not None:
                        usage = AIUsage(
                            prompt_tokens=int(getattr(last, "input_tokens", 0) or 0),
                            cached_prompt_tokens=int(getattr(last, "cached_input_tokens", 0) or 0),
                            completion_tokens=int(getattr(last, "output_tokens", 0) or 0),
                            total_tokens=int(getattr(last, "total_tokens", 0) or 0),
                        )
                    continue

                if method == "turn/completed":
                    completed_turn = getattr(payload, "turn", None)
                    turn_status = _status_value(getattr(completed_turn, "status", ""))
                    turn_error = getattr(completed_turn, "error", None)

            if timed_out.is_set():
                raise TimeoutError(f"Codex SDK request timed out after {timeout_s:.0f}s")
            if cancel_token is not None and cancel_token.is_set():
                raise CancelledError("Request cancelled during Codex turn")
            if turn_status and turn_status != "completed":
                raise RuntimeError(
                    f"Codex turn ended with status={turn_status}: {_error_text(turn_error)}"
                )
        except CancelledError:
            raise
        except Exception as exc:
            if timed_out.is_set():
                raise TimeoutError(f"Codex SDK request timed out after {timeout_s:.0f}s") from exc
            if cancel_token is not None and cancel_token.is_set():
                raise CancelledError("Request cancelled during Codex turn") from exc
            self._log.error("CodexSdkClient stream error: %s", exc)
            raise
        finally:
            watcher_stop.set()
            if timer is not None:
                timer.cancel()
            if watcher is not None:
                watcher.join(timeout=0.2)
            codex.close()

        latency_ms = (time.monotonic() - t0) * 1000
        content = "".join(content_parts) or completed_item_content
        reasoning_content = "".join(reasoning_parts) or completed_item_reasoning
        if not content_parts and content and on_content_token is not None:
            on_content_token(content)
        if not reasoning_parts and reasoning_content and on_reasoning_token is not None:
            on_reasoning_token(reasoning_content)

        if not content.strip():
            raise RuntimeError("Codex SDK returned an empty final response")

        self._log.info(
            "CodexSdkClient.stream_chat done: latency=%.0f ms content_chars=%d "
            "reasoning_chars=%d total_tokens=%d",
            latency_ms,
            len(content),
            len(reasoning_content),
            usage.total_tokens,
        )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw={
                "id": turn_id,
                "thread_id": thread_id,
                "status": turn_status,
                "model": model,
                "content": content,
                "reasoning_content": reasoning_content,
                "usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "cached_prompt_tokens": usage.cached_prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
                "latency_ms": latency_ms,
            },
            usage=usage,
            request_id=turn_id,
            latency_ms=latency_ms,
        )
