"""Resilient LLM wrapper: retry-with-backoff + provider fallback.

Retries transient failures (429/5xx/timeout/reset) with exponential
backoff, then falls back to the next provider. If a provider already
streamed visible content before failing, we cannot retry without
duplicating output, so we surface the error instead (rare in practice)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from drx_agent.llm.base import AgentEvent, AgentEventType, LLMProvider

logger = logging.getLogger(__name__)


_TRANSIENT_MARKERS = (
    "429", "rate limit", "ratelimit", "too many requests",
    "500", "502", "503", "504",
    "overloaded", "capacity", "server error", "internal error",
    "timeout", "timed out", "deadline",
    "connection", "econnreset", "connection reset", "broken pipe",
    "temporarily", "try again", "service unavailable", "unavailable",
)


def _is_transient(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _TRANSIENT_MARKERS)


class ResilientProvider(LLMProvider):
    def __init__(
        self,
        providers: list[LLMProvider],
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not providers:
            raise ValueError("ResilientProvider needs at least one provider")
        self.providers = providers
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        # notify: human-readable status (retrying / failing over) surfaced by the UI. Optional.
        self.notify = notify

    @property
    def config(self):
        return self.providers[0].config

    def _emit(self, msg: str) -> None:
        logger.warning(msg)
        if self.notify:
            try:
                self.notify(msg)
            except Exception:
                pass

    async def chat(self, messages, tools=None, stream=True):
        last_error_msg = "all providers failed"
        n_providers = len(self.providers)

        for p_idx, provider in enumerate(self.providers):
            label = f"{type(provider).__name__}/{getattr(provider.config, 'model', '?')}"
            for attempt in range(self.max_retries + 1):
                yielded_content = False
                error_msg: Optional[str] = None
                done_seen = False

                try:
                    async for ev in provider.chat(messages, tools=tools, stream=stream):
                        kind = getattr(ev, "type", None)
                        kind_value = kind.value if hasattr(kind, "value") else kind
                        if kind_value in ("text", "tool_call"):
                            yielded_content = True
                            yield ev
                        elif kind_value == "error":
                            error_msg = ev.content or "unknown error"
                            break
                        elif kind_value == "done":
                            done_seen = True
                            yield ev
                        else:
                            yield ev
                except Exception as exc:
                    logger.exception("Provider %s raised", label)
                    error_msg = str(exc)

                if error_msg is None and done_seen:
                    return

                if yielded_content:
                    # Content already streamed — can't safely retry.
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        content=f"{error_msg} (mid-stream, not retried)",
                    )
                    return

                last_error_msg = error_msg or "unknown error"
                transient = _is_transient(last_error_msg)

                if transient and attempt < self.max_retries:
                    delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                    self._emit(
                        f"LLM {label} transient error: {last_error_msg[:120]} — "
                        f"retry {attempt + 1}/{self.max_retries} in {delay:.0f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                if not transient:
                    self._emit(
                        f"LLM {label} non-transient error: {last_error_msg[:120]}"
                    )
                break

            if p_idx < n_providers - 1:
                self._emit(
                    f"Failing over from {label} to "
                    f"{type(self.providers[p_idx + 1]).__name__}"
                )

        yield AgentEvent(
            type=AgentEventType.ERROR,
            content=f"all LLM providers failed: {last_error_msg}",
        )

    def count_tokens(self, messages):
        return self.providers[0].count_tokens(messages)

