"""Model retry, disconnect and billing classification."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Callable

MODEL_RETRY_INITIAL_DELAY = 1.0
MODEL_RETRY_MAX_DELAY = 300.0

_BILLING_MARKERS = (
    "insufficient",
    "余额不足",
    "余额已耗尽",
    "额度不足",
    "额度已用尽",
    "欠费",
    "payment required",
    "credit balance",
    "credits exhausted",
    "quota exceeded",
    "quota_exceeded",
    "billing_hard_limit",
    "exceeded your current quota",
    "openai status 402:",
)

_DISCONNECT_MARKERS = (
    "openai status 5",
    "openai status 408:",
    "openai status 429:",
    "stream ended without response.completed",
    "connection reset",
    "connection refused",
    "broken pipe",
)


def error_text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    return str(exc)


def is_context_length_error(exc: BaseException | None) -> bool:
    text = error_text(exc).lower()
    return (
        "maximum context length" in text
        or "context length" in text
        or "input_tokens" in text
    )


def is_billing_exhausted(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    text = error_text(exc).lower()
    return any(marker in text for marker in _BILLING_MARKERS)


def is_incomplete_generation(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    name = type(exc).__name__
    if name in ("IncompleteGeneration", "IncompleteResponse"):
        return True
    return "model generation incomplete" in error_text(exc).lower()


def is_tool_protocol_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    return type(exc).__name__ in ("ToolProtocolError", "ToolProtocol")


def is_model_disconnect(exc: BaseException | None) -> bool:
    if exc is None or is_context_length_error(exc):
        return False
    name = type(exc).__name__
    if name in ("NetworkError", "TimeoutError", "ConnectError", "ReadError"):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, asyncio.CancelledError):
        return False
    text = error_text(exc).lower()
    if "unexpected_eof" in text or "unexpected eof" in text or "eof" == text.strip():
        return True
    return any(marker in text for marker in _DISCONNECT_MARKERS)


async def wait_model_retry(delay: float) -> None:
    """Sleep for *delay* seconds, cancellable through task cancellation."""

    await asyncio.sleep(delay)


class ModelRequestRunner:
    """Retry wrapper shared by every model request in an agent.

    Transport failures are retried with an exponential, saturating delay until
    the task is cancelled.  Billing exhaustion terminates the whole team.
    Incomplete generations retry immediately at most three times.
    """

    def __init__(self, emit, on_disconnect=None, wait=None) -> None:
        self._emit = emit
        self._on_disconnect = on_disconnect
        self._wait = wait or wait_model_retry

    async def run(self, request: Callable[[], Awaitable[Any]]) -> Any:
        incomplete_retries = 0
        retry_delay = MODEL_RETRY_INITIAL_DELAY
        while True:
            error: BaseException | None = None
            try:
                answer = await request()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - classified below
                error = exc
                answer = None
            if is_billing_exhausted(error):
                if self._on_disconnect is not None:
                    result = self._on_disconnect(error)
                    if asyncio.iscoroutine(result):
                        await result
                raise RuntimeError(
                    "API余额或额度不足，已立即终止全队运行，不再重试: " + error_text(error)
                )
            if error is None:
                return answer
            if is_incomplete_generation(error):
                if incomplete_retries == 3:
                    raise RuntimeError(
                        "生成未完成，回退到上次完整结果后重试3次仍失败；保留进度，输入 go 可继续: "
                        + error_text(error)
                    )
                incomplete_retries += 1
                self._info(
                    "生成未完成：%s；丢弃本次残缺输出，回退到上次完整结果，重试 %d/3"
                    % (error_text(error), incomplete_retries)
                )
                continue
            if not is_model_disconnect(error):
                raise error
            self._emit_wait(
                "模型服务断线；等待 %s 后继续重试（取消任务可停止）" % _format_delay(retry_delay)
            )
            await self._wait(retry_delay)
            if retry_delay < MODEL_RETRY_MAX_DELAY:
                if retry_delay > MODEL_RETRY_MAX_DELAY / 2:
                    retry_delay = MODEL_RETRY_MAX_DELAY
                else:
                    retry_delay *= 2

    def _info(self, text: str) -> None:
        with contextlib.suppress(Exception):
            from .events import Event

            self._emit(Event(kind="info", content=text))

    def _emit_wait(self, text: str) -> None:
        with contextlib.suppress(Exception):
            from .events import Event

            self._emit(Event(kind="waiting", content=text))


def _error_of(answer: Any) -> BaseException | None:
    return answer if isinstance(answer, BaseException) else None


def _format_delay(delay: float) -> str:
    if delay >= 60 and delay % 60 == 0:
        return "%dm0s" % int(delay // 60)
    if delay >= 1:
        return "%gs" % delay
    return "%gms" % (delay * 1000)


class BillingCancelled(RuntimeError):
    """Raised when the account cannot pay for further model requests."""
