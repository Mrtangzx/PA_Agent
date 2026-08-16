"""Provider-neutral value types shared by PA Agent model clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AIUsage:
    """Token usage from one model call."""

    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of prompt tokens served from cache (0.0-1.0)."""
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_prompt_tokens / self.prompt_tokens

    @property
    def cache_miss_tokens(self) -> int:
        """Prompt tokens that were not served from cache."""
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)


@dataclass
class AIReply:
    """Structured response returned by the active model client."""

    content: str
    reasoning_content: str
    raw: dict[str, Any]
    usage: AIUsage
    request_id: str
    latency_ms: float


class CancelledError(Exception):
    """Raised when a model call is cancelled before or during execution."""
