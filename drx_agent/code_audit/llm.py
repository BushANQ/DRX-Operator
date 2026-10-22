"""Asynchronous model client for the code-audit subsystem.

The client speaks both the Responses and the Chat Completions interfaces,
supports server-sent-event streaming, native function calls, usage
observation and generation progress.  Blocking HTTP work runs in a worker
thread so that callers on the event loop never block; parsed deltas are
delivered through the ``emit`` callback.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

import requests

from drx_agent.code_audit.config import OpenAIConfig
from drx_agent.code_audit.text import sanitize_text

__all__ = [
    "Role",
    "Message",
    "ToolDefinition",
    "FunctionCall",
    "ToolResponse",
    "GenerationProgress",
    "Delta",
    "UsageUpdate",
    "LLMError",
    "APIError",
    "NetworkError",
    "NetworkTimeoutError",
    "StreamTimeoutError",
    "IncompleteGeneration",
    "ToolProtocolError",
    "Client",
    "ToolClient",
    "OpenAIClient",
    "validate_tool_history",
    "estimate_tokens",
    "estimate_text_tokens",
]


# ---------------------------------------------------------------------------
# Wire limits
# ---------------------------------------------------------------------------

# Decoded text and tool arguments are capped at one mebibyte.
RESPONSES_MAX_BYTES = 1024 * 1024
# Number of tracked content parts (text/arguments) per response.
_MAX_PARTS = RESPONSES_MAX_BYTES // 64
# Minimum interval between progress callbacks.
_PROGRESS_INTERVAL = 0.25


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for model client failures."""


class APIError(LLMError):
    """The provider answered with a non-success HTTP status."""

    def __init__(self, status_code: int, body: str):
        self.status_code = int(status_code)
        self.body = body or ""
        self.retryable = self.status_code in (408, 429) or 500 <= self.status_code < 600
        super().__init__(f"openai status {self.status_code}: {self.body}")

    @property
    def status(self) -> int:
        return self.status_code


class NetworkError(LLMError):
    """Transport failure.  The agent layer retries these."""

    def __init__(self, message: str):
        super().__init__(message)
        self.retryable = True


class IncompleteGeneration(LLMError):
    """The provider truncated the generation before it finished."""

    def __init__(self, message: str):
        super().__init__(message)
        self.retryable = True


class NetworkTimeoutError(NetworkError, TimeoutError):
    """No response headers arrived before the request timeout.

    Also a builtin :class:`TimeoutError`, so retry classifiers treat it as a
    transport failure rather than a permanent error.
    """


class StreamTimeoutError(NetworkError, IncompleteGeneration):
    """The stream produced no model output within the idle timeout.

    Retryable both as a network error and, like the incomplete-generation
    signal, without advancing the retry backoff.  The message carries the
    incomplete-generation marker for classifiers that inspect text.
    """

    def __init__(self, seconds: float):
        self.timeout = seconds
        self.generating = True
        NetworkError.__init__(
            self,
            "model generation incomplete: openai stream: no model output for "
            f"{_format_duration(seconds)} (SSE heartbeats do not count)",
        )


class ToolProtocolError(LLMError):
    """The provider's native tool protocol stream was malformed."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        if detail:
            super().__init__(f"openai tool protocol {kind}: {detail}")
        else:
            super().__init__(f"openai tool protocol: {kind}")


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    def __str__(self) -> str:
        return self.value


@dataclass
class Message:
    role: Role | str = Role.USER
    content: str = ""
    type: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    id: str = ""


@dataclass
class ToolDefinition:
    type: str = "function"
    name: str = ""
    description: str = ""
    parameters: Any = field(default_factory=dict)
    strict: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": self.strict,
        }


@dataclass
class FunctionCall:
    id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class ToolResponse:
    content: str = ""
    thinking: str = ""
    calls: list[FunctionCall] = field(default_factory=list)
    response_id: str = ""
    # True when the provider cannot be forced to emit exactly one tool call.
    multi_call_fallback: bool = False


@dataclass
class GenerationProgress:
    output_tokens: int = 0
    reasoning_tokens: int = 0
    tool_tokens: int = 0
    estimated: bool = False
    received_at: str = ""


@dataclass
class Delta:
    content: str = ""
    thinking: str = ""
    progress: GenerationProgress | None = None


@dataclass
class UsageUpdate:
    request_id: int = 0
    total_tokens: int = 0
    estimated: bool = False
    final: bool = False


@runtime_checkable
class Client(Protocol):
    """Plain text client interface."""

    async def chat(
        self,
        messages: list[Message],
        *,
        emit: Callable[[Delta], None] | None = ...,
        observer: Callable[[UsageUpdate], None] | None = ...,
        previous_response_id: str = ...,
        continuation: list[Message] | None = ...,
    ) -> str: ...

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        emit: Callable[[Delta], None] | None = ...,
        observer: Callable[[UsageUpdate], None] | None = ...,
        previous_response_id: str = ...,
        continuation: list[Message] | None = ...,
    ) -> str: ...


@runtime_checkable
class ToolClient(Protocol):
    """Native tool-call client interface."""

    async def chat_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        emit: Callable[[Delta], None] | None = ...,
        observer: Callable[[UsageUpdate], None] | None = ...,
        previous_response_id: str = ...,
        continuation: list[Message] | None = ...,
    ) -> ToolResponse: ...


# ---------------------------------------------------------------------------
# Token estimation (project-wide estimator)
# ---------------------------------------------------------------------------


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for one string: UTF-8-agnostic, CJK-aware."""
    if not text:
        return 0
    total = 0
    ascii_run = 0
    for char in text:
        codepoint = ord(char)
        if codepoint <= 0x7F:
            ascii_run += 1
            continue
        if ascii_run:
            total += (ascii_run + 3) // 4
            ascii_run = 0
        if 0x4E00 <= codepoint <= 0x9FFF:
            total += 1
        else:
            total += 2
    if ascii_run:
        total += (ascii_run + 3) // 4
    return total or 1


def estimate_tokens(messages: list[Message]) -> int:
    """Estimate the input tokens of a message list."""
    total = 0
    for message in messages:
        total += estimate_text_tokens(message.content)
        total += estimate_text_tokens(message.arguments)
        total += estimate_text_tokens(message.name)
        total += estimate_text_tokens(message.call_id)
        total += 4
    return total


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utf8_len(value: str) -> int:
    return len(sanitize_text(value).encode("utf-8"))


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def _role_value(role: Role | str) -> str:
    if isinstance(role, Role):
        return role.value
    return str(role)


def _message_json(message: Message) -> dict[str, Any]:
    role = _role_value(message.role)
    data: dict[str, Any] = {}
    if role:
        data["role"] = role
    if message.content:
        data["content"] = message.content
    if message.type:
        data["type"] = message.type
    if message.call_id:
        data["call_id"] = message.call_id
    if message.name:
        data["name"] = message.name
    if message.arguments:
        data["arguments"] = message.arguments
    if message.id:
        data["id"] = message.id
    return data


def _join_assistant_parts(thinking: str, content: str) -> str:
    if not thinking:
        return content
    return f"<think>{thinking}</think>{content}"


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 1:
        return f"{int(round(seconds * 1000))}ms"
    if float(seconds).is_integer():
        return f"{int(seconds)}s"
    return f"{seconds:.3f}s"


def _format_received_at(epoch: float) -> str:
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{moment.microsecond:06d}Z"


def _json_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        raise ValueError("missing string value")
    raise ValueError(f"expected a JSON string, got {type(value).__name__}")


def _int_field(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text_field(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return "" if value is None else sanitize_text(value)


def _native_text_limit(tokens: int) -> int:
    max_int = sys.maxsize
    if tokens > max_int // 16:
        return max_int
    if tokens <= RESPONSES_MAX_BYTES // 16:
        return RESPONSES_MAX_BYTES
    return tokens * 16


def _response_wire_limit(text_limit: int) -> int:
    # JSON may escape every byte as six characters; keep wire overhead bounded.
    if text_limit < RESPONSES_MAX_BYTES:
        text_limit = RESPONSES_MAX_BYTES
    max_int = sys.maxsize
    limit = max_int - 1
    if text_limit <= (max_int - 1 - RESPONSES_MAX_BYTES) // 6:
        limit = text_limit * 6 + RESPONSES_MAX_BYTES
    return limit


def _close_response(response: Any) -> None:
    try:
        response.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class _RequestsTransport:
    """Blocking HTTP transport backed by a shared requests session."""

    def __init__(self) -> None:
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def post(
        self,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any],
        stream: bool,
        timeout: float | None,
    ) -> Any:
        kwargs: dict[str, Any] = {"headers": headers, "json": json_body, "stream": stream}
        if timeout is not None and timeout > 0:
            kwargs["timeout"] = timeout
        return self._get_session().post(url, **kwargs)

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


_TRANSPORT: Any = _RequestsTransport()


# ---------------------------------------------------------------------------
# Internal response model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Part:
    output: int = 0
    index: int = 0
    kind: str = ""


@dataclass
class _APIErrorPayload:
    code: str = ""
    message: str = ""

    @classmethod
    def from_json(cls, data: Any) -> "_APIErrorPayload":
        if not isinstance(data, dict):
            return cls()
        return cls(code=_text_field(data, "code"), message=_text_field(data, "message"))


@dataclass
class _GenerationUsage:
    total_tokens: int | None = None
    input_tokens: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int = 0

    @classmethod
    def from_json(cls, data: Any) -> "_GenerationUsage | None":
        if not isinstance(data, dict):
            return None
        output_details = data.get("output_tokens_details")
        completion_details = data.get("completion_tokens_details")
        reasoning = 0
        if isinstance(output_details, dict):
            reasoning = _int_field(output_details, "reasoning_tokens")
        elif isinstance(completion_details, dict):
            reasoning = _int_field(completion_details, "reasoning_tokens")
        if not isinstance(output_details, dict) and isinstance(completion_details, dict):
            reasoning = _int_field(completion_details, "reasoning_tokens")

        def optional_int(key: str) -> int | None:
            value = data.get(key)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            total_tokens=optional_int("total_tokens"),
            input_tokens=optional_int("input_tokens"),
            prompt_tokens=optional_int("prompt_tokens"),
            output_tokens=optional_int("output_tokens"),
            completion_tokens=optional_int("completion_tokens"),
            reasoning_tokens=max(reasoning, 0),
        )


@dataclass
class _ResponsesContentItem:
    type: str = ""
    text: str = ""

    @classmethod
    def from_json(cls, data: Any) -> "_ResponsesContentItem":
        if not isinstance(data, dict):
            return cls()
        return cls(type=_text_field(data, "type"), text=_text_field(data, "text"))


@dataclass
class _ResponsesOutputItem:
    type: str = ""
    role: str = ""
    id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    status: str = ""
    content: list[_ResponsesContentItem] = field(default_factory=list)
    summary: list[_ResponsesContentItem] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: Any) -> "_ResponsesOutputItem":
        if not isinstance(data, dict):
            return cls()
        return cls(
            type=_text_field(data, "type"),
            role=_text_field(data, "role"),
            id=_text_field(data, "id"),
            call_id=_text_field(data, "call_id"),
            name=_text_field(data, "name"),
            arguments=_text_field(data, "arguments"),
            status=_text_field(data, "status"),
            content=[_ResponsesContentItem.from_json(item) for item in data.get("content") or []],
            summary=[_ResponsesContentItem.from_json(item) for item in data.get("summary") or []],
        )


@dataclass
class _ResponsesResponse:
    id: str = ""
    status: str = ""
    error: _APIErrorPayload = field(default_factory=_APIErrorPayload)
    incomplete_reason: str = ""
    output: list[_ResponsesOutputItem] = field(default_factory=list)
    usage: _GenerationUsage | None = None

    @classmethod
    def from_json(cls, data: Any) -> "_ResponsesResponse":
        if not isinstance(data, dict):
            return cls()
        details = data.get("incomplete_details")
        reason = ""
        if isinstance(details, dict):
            reason = _text_field(details, "reason")
        return cls(
            id=_text_field(data, "id"),
            status=_text_field(data, "status"),
            error=_APIErrorPayload.from_json(data.get("error")),
            incomplete_reason=reason,
            output=[
                _ResponsesOutputItem.from_json(item) for item in data.get("output") or []
            ],
            usage=_GenerationUsage.from_json(data.get("usage")),
        )

    def failure_detail(self) -> str:
        detail = _first_non_empty(
            self.error.message,
            self.incomplete_reason,
            self.status,
            "missing response status",
        )
        return (self.error.code + " " + detail).strip()

    def completed(self) -> None:
        if self.status == "incomplete":
            _raise_incomplete(self)
        if self.status != "completed" or self.error.message or self.error.code:
            raise LLMError(f"openai responses did not complete: {self.failure_detail()}")


def _iter_text_parts(response: _ResponsesResponse) -> Iterator[tuple[_Part, Delta]]:
    for output_index, item in enumerate(response.output):
        for content_index, content in enumerate(item.content):
            if (
                item.type == "message"
                and item.role == "assistant"
                and content.type == "output_text"
            ):
                delta = Delta(content=content.text)
            elif item.type == "reasoning" and content.type == "reasoning_text":
                delta = Delta(thinking=content.text)
            else:
                continue
            if delta.content or delta.thinking:
                yield _Part(output_index, content_index, content.type), delta
        if item.type == "reasoning":
            for summary_index, summary in enumerate(item.summary):
                if summary.type == "summary_text" and summary.text:
                    yield (
                        _Part(output_index, summary_index, summary.type),
                        Delta(thinking=summary.text),
                    )


@dataclass
class _ResponsesStreamEvent:
    type: str = ""
    delta: Any = None
    output_index: int = 0
    content_index: int = 0
    summary_index: int = 0
    response: _ResponsesResponse = field(default_factory=_ResponsesResponse)
    code: str = ""
    message: str = ""
    error: _APIErrorPayload = field(default_factory=_APIErrorPayload)
    item: _ResponsesOutputItem = field(default_factory=_ResponsesOutputItem)
    item_id: str = ""
    arguments: str = ""
    text: str = ""

    @classmethod
    def from_json(cls, data: Any) -> "_ResponsesStreamEvent":
        if not isinstance(data, dict):
            raise ValueError("event must be a JSON object")
        return cls(
            type=_text_field(data, "type"),
            delta=data.get("delta"),
            output_index=_int_field(data, "output_index"),
            content_index=_int_field(data, "content_index"),
            summary_index=_int_field(data, "summary_index"),
            response=_ResponsesResponse.from_json(data.get("response")),
            code=_text_field(data, "code"),
            message=_text_field(data, "message"),
            error=_APIErrorPayload.from_json(data.get("error")),
            item=_ResponsesOutputItem.from_json(data.get("item")),
            item_id=_text_field(data, "item_id"),
            arguments=_text_field(data, "arguments"),
            text=_text_field(data, "text"),
        )


def _raise_incomplete(response: _ResponsesResponse) -> None:
    reason = response.failure_detail()
    if (
        not response.error.message
        and not response.error.code
        and (not response.incomplete_reason or response.incomplete_reason == "max_output_tokens")
    ):
        raise IncompleteGeneration(f"model generation incomplete: {reason}")
    raise LLMError(f"openai response incomplete: {reason}")


def _raise_unfinished_chat(reason: str) -> None:
    if reason == "length":
        raise IncompleteGeneration("model generation incomplete: finish_reason=length")
    raise LLMError(f"openai native chat did not complete: {reason}")


# ---------------------------------------------------------------------------
# Native tool protocol validation
# ---------------------------------------------------------------------------


def _valid_call(call: FunctionCall) -> None:
    arguments_len = _utf8_len(call.arguments)
    if arguments_len > RESPONSES_MAX_BYTES or _utf8_len(call.name) > RESPONSES_MAX_BYTES - arguments_len:
        raise ToolProtocolError("function arguments too large")
    if not call.call_id.strip():
        raise ToolProtocolError("missing call_id")
    if not call.name.strip():
        raise ToolProtocolError("missing function name")
    try:
        parsed = json.loads(call.arguments)
    except (TypeError, ValueError) as exc:
        raise ToolProtocolError("arguments must be a complete JSON object", str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ToolProtocolError("arguments must be a complete JSON object")


def _validate_calls(calls: list[FunctionCall]) -> None:
    seen: set[str] = set()
    for call in calls:
        _valid_call(call)
        if call.call_id in seen:
            raise ToolProtocolError("duplicate call_id")
        seen.add(call.call_id)


def validate_tool_history(messages: list[Message]) -> None:
    """Fail closed when a retained history breaks native call pairing."""
    pending: dict[str, bool] = {}
    seen: set[str] = set()
    for message in messages:
        if message.type == "function_call":
            try:
                _valid_call(
                    FunctionCall(
                        call_id=message.call_id,
                        name=message.name,
                        arguments=message.arguments,
                    )
                )
            except ToolProtocolError as exc:
                raise ToolProtocolError(
                    f"invalid native history call: {exc}"
                ) from exc
            if message.call_id in seen:
                raise ToolProtocolError(
                    f'native history duplicates call_id "{message.call_id}"'
                )
            seen.add(message.call_id)
            pending[message.call_id] = True
        elif message.type == "function_call_output":
            if message.call_id not in pending:
                raise ToolProtocolError(
                    f'native history has orphan output "{message.call_id}"'
                )
            del pending[message.call_id]
        elif message.type == "":
            if pending:
                raise ToolProtocolError("native history interrupts an unresolved function call")
        else:
            raise ToolProtocolError(
                f'unsupported native history item type "{message.type}"'
            )
    if pending:
        raise ToolProtocolError("native history has unresolved function calls")


# ---------------------------------------------------------------------------
# Request payload helpers
# ---------------------------------------------------------------------------


def _responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = _role_value(message.role)
        if role == Role.TOOL.value:
            role = Role.USER.value
        items.append({"role": role, "content": message.content})
    return items


def _native_responses_input(messages: list[Message]) -> list[Any]:
    items: list[Any] = []
    for message in messages:
        if message.type == "function_call":
            item: dict[str, Any] = {
                "type": "function_call",
                "call_id": message.call_id,
                "name": message.name,
                "arguments": message.arguments,
            }
            if message.id:
                item["id"] = message.id
            items.append(item)
        elif message.type == "function_call_output":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.call_id,
                    "output": message.content,
                }
            )
        else:
            role = _role_value(message.role)
            if role == Role.TOOL.value:
                role = Role.USER.value
            items.append({"role": role, "content": message.content})
    return items


def _native_chat_input(messages: list[Message]) -> list[Any]:
    items: list[Any] = []
    for message in messages:
        if message.type == "function_call":
            call = {
                "id": message.call_id,
                "type": "function",
                "function": {"name": message.name, "arguments": message.arguments},
            }
            if (
                items
                and isinstance(items[-1], dict)
                and items[-1].get("role") == "assistant"
                and items[-1].get("tool_calls") is not None
            ):
                items[-1]["tool_calls"].append(call)
                continue
            items.append({"role": "assistant", "tool_calls": [call]})
        elif message.type == "function_call_output":
            items.append(
                {
                    "role": "tool",
                    "tool_call_id": message.call_id,
                    "content": message.content,
                }
            )
        else:
            role = _role_value(message.role)
            if role == Role.TOOL.value:
                role = Role.USER.value
            items.append({"role": role, "content": message.content})
    return items


def _native_chat_tools(tools: list[ToolDefinition]) -> list[Any]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": tool.strict,
            },
        }
        for tool in tools
    ]


def _thinking_json(thinking: Any) -> dict[str, Any] | None:
    if thinking is None:
        return None
    return {"type": getattr(thinking, "type", "")}


# ---------------------------------------------------------------------------
# SSE reader
# ---------------------------------------------------------------------------


class _SSEReader:
    """Reads ``iter_lines`` in a worker thread and feeds an asyncio queue."""

    def __init__(self, response: Any, wire_limit: int, timeout: float):
        self._response = response
        self._wire_limit = wire_limit
        self._timeout = float(timeout) if timeout and timeout > 0 else 0.0
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._closed = False
        self._deadline: float | None = None
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._read, daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._timeout > 0:
            self._deadline = time.monotonic() + self._timeout
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_response(self._response)

    def activity(self) -> None:
        if self._timeout > 0:
            self._deadline = time.monotonic() + self._timeout

    # -- worker ------------------------------------------------------------

    def _deliver(self, item: tuple[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            pass

    def _read(self) -> None:
        try:
            for raw in self._response.iter_lines():
                if self._closed:
                    break
                if isinstance(raw, bytes):
                    raw_bytes = raw
                else:
                    raw_bytes = str(raw).encode("utf-8")
                if len(raw_bytes) > self._wire_limit:
                    raise LLMError(
                        f"openai stream: SSE frame exceeds {self._wire_limit} bytes"
                    )
                try:
                    line = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    line = raw_bytes.decode("utf-8", errors="replace")
                self._deliver(("line", line))
        except BaseException as exc:  # noqa: BLE001 - delivered to the consumer
            self._deliver(("error", exc))
        finally:
            self._deliver(("eof", None))

    # -- consumer ----------------------------------------------------------

    async def next_line(self) -> str | None:
        timeout: float | None = None
        if self._timeout > 0 and self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise StreamTimeoutError(self._timeout)
            timeout = remaining
        try:
            kind, value = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            raise StreamTimeoutError(self._timeout) from None
        if kind == "eof":
            return None
        if kind == "error":
            if isinstance(value, LLMError):
                raise value
            raise NetworkError(f"openai stream read failed: {value}") from value
        return str(value)


# ---------------------------------------------------------------------------
# Generation progress tracker
# ---------------------------------------------------------------------------


class _GenerationTracker:
    def __init__(self, emit: Callable[[Delta], None] | None):
        self.emit = emit
        self.progress = GenerationProgress(estimated=True)
        self.text_bytes = 0
        self.reasoning_bytes = 0
        self.tool_bytes = 0
        self.last_emit = 0.0
        self.received_at: float | None = None
        self.dirty = False
        self.text_limit = RESPONSES_MAX_BYTES
        self.activity: Callable[[], None] | None = None
        self._flush()

    def _flush(self) -> None:
        if self.emit is not None:
            progress = GenerationProgress(
                output_tokens=self.progress.output_tokens,
                reasoning_tokens=self.progress.reasoning_tokens,
                tool_tokens=self.progress.tool_tokens,
                estimated=self.progress.estimated,
                received_at=self.progress.received_at,
            )
            self.emit(Delta(progress=progress))
        self.last_emit = time.monotonic()
        self.dirty = False

    def add(self, text_bytes: int, reasoning_bytes: int, tool_bytes: int) -> None:
        if text_bytes + reasoning_bytes + tool_bytes == 0:
            return
        if self.activity is not None:
            self.activity()
        self.text_bytes += text_bytes
        self.reasoning_bytes += reasoning_bytes
        self.tool_bytes += tool_bytes
        first = self.received_at is None
        now = time.monotonic()
        self.received_at = time.time()
        total = self.text_bytes + self.reasoning_bytes + self.tool_bytes
        self.progress.output_tokens = (total + 3) // 4
        self.progress.reasoning_tokens = (self.reasoning_bytes + 3) // 4
        self.progress.tool_tokens = (self.tool_bytes + 3) // 4
        if self.received_at is not None:
            self.progress.received_at = _format_received_at(self.received_at)
        self.dirty = True
        if first or now - self.last_emit >= _PROGRESS_INTERVAL:
            self._flush()

    def text(self, delta: Delta) -> None:
        self.add(_utf8_len(delta.content), _utf8_len(delta.thinking), 0)
        if self.emit is not None:
            self.emit(delta)

    def finish(self, usage: _GenerationUsage | None) -> None:
        if usage is not None:
            total = usage.output_tokens if usage.output_tokens is not None else usage.completion_tokens
            reasoning = usage.reasoning_tokens
            if total is not None and total >= 0 and 0 <= reasoning <= total:
                self.progress.output_tokens = total
                self.progress.reasoning_tokens = reasoning
                self.progress.tool_tokens = 0
                self.progress.estimated = False
                self._flush()
                return
        self._flush()

    def flush_pending(self) -> None:
        if self.dirty:
            try:
                self._flush()
            except Exception:
                pass


class _ToolText:
    def __init__(self, emit: Callable[[Delta], None] | None, limit: int):
        self.content = ""
        self.thinking = ""
        self.emit = emit
        self.limit = limit

    def add(self, delta: Delta) -> None:
        limit = max(self.limit, RESPONSES_MAX_BYTES)
        remaining = limit - _utf8_len(self.content) - _utf8_len(self.thinking)
        content_len = _utf8_len(delta.content)
        thinking_len = _utf8_len(delta.thinking)
        if content_len > remaining or thinking_len > remaining - content_len:
            raise LLMError(f"openai native text exceeds {limit} bytes")
        self.content += delta.content
        self.thinking += delta.thinking
        if self.emit is not None and (delta.content or delta.thinking):
            self.emit(delta)

    def result(
        self, calls: list[FunctionCall], *, multi_call_fallback: bool = False
    ) -> ToolResponse:
        return ToolResponse(
            content=self.content,
            thinking=self.thinking,
            calls=list(calls),
            multi_call_fallback=multi_call_fallback,
        )


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------

_USAGE_SEQUENCE = itertools.count(1)


class _RequestUsage:
    """Cumulative input plus output usage for a single network request."""

    def __init__(self, observer: Callable[[UsageUpdate], None], input_tokens: int):
        self.observer = observer
        self.input = input_tokens
        self.output_bytes = 0
        self.parts: dict[_Part, int] = {}
        self.provider: _GenerationUsage | None = None
        self.err: LLMError | None = None
        self.update = UsageUpdate(
            request_id=next(_USAGE_SEQUENCE),
            total_tokens=input_tokens,
            estimated=True,
            final=False,
        )
        self.observer(self.update)

    # -- publishing --------------------------------------------------------

    def publish(self, final: bool) -> None:
        total = self.input + (self.output_bytes + 3) // 4
        estimated = True
        provider = self.provider
        if provider is not None:
            provider_input = (
                provider.input_tokens
                if provider.input_tokens is not None
                else provider.prompt_tokens
            )
            provider_output = (
                provider.output_tokens
                if provider.output_tokens is not None
                else provider.completion_tokens
            )
            if provider_input is not None and provider_input >= 0:
                total = provider_input + (self.output_bytes + 3) // 4
            if provider_output is not None and provider_output >= 0:
                total = self.input + provider_output
                if provider_input is not None and provider_input >= 0:
                    total, estimated = provider_input + provider_output, False
            if provider.total_tokens is not None and provider.total_tokens >= 0:
                total, estimated = provider.total_tokens, False
        if (
            final
            or total != self.update.total_tokens
            or estimated != self.update.estimated
        ):
            self.update = UsageUpdate(
                request_id=self.update.request_id,
                total_tokens=total,
                estimated=estimated,
                final=final,
            )
            self.observer(self.update)

    def finish(self) -> None:
        self.publish(True)

    # -- byte accounting ---------------------------------------------------

    def record_part(self, part: _Part, size: int, snapshot: bool) -> None:
        previous = self.parts.get(part)
        if previous is None and len(self.parts) >= _MAX_PARTS:
            self.err = LLMError("openai usage: too many streamed content parts")
            self.output_bytes += size
            return
        if snapshot:
            if size <= (previous or 0):
                return
            self.parts[part] = size
            self.output_bytes += size - (previous or 0)
        else:
            self.parts[part] = (previous or 0) + size
            self.output_bytes += size

    def chat(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        content = _text_field(message, "content")
        thinking = _first_non_empty(
            _text_field(message, "reasoning_content"),
            _text_field(message, "reasoning"),
            _text_field(message, "reasoning_text"),
        )
        size = _utf8_len(content) + _utf8_len(thinking)
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict):
                size += _utf8_len(_text_field(function, "name"))
                size += _utf8_len(_text_field(function, "arguments"))
        self.add(size)

    def add(self, size: int) -> None:
        self.output_bytes += size
        self.publish(False)
        if self.err is not None:
            raise self.err

    def safe_add(self, size: int) -> None:
        try:
            self.add(size)
        except LLMError:
            pass

    def supplied(self, usage: _GenerationUsage | None) -> None:
        if usage is not None:
            self.provider = usage
            self.publish(False)

    def response_item(self, index: int, item: _ResponsesOutputItem) -> None:
        if item.type == "function_call":
            self.record_part(
                _Part(index, 0, "function_name"), _utf8_len(item.name), True
            )
            self.record_part(
                _Part(index, 0, "function_arguments"),
                _utf8_len(item.arguments),
                True,
            )
        for part, delta in _iter_text_parts(_ResponsesResponse(output=[item])):
            self.record_part(
                _Part(index, part.index, part.kind),
                _utf8_len(delta.content) + _utf8_len(delta.thinking),
                True,
            )

    def response(self, snapshot: _ResponsesResponse) -> None:
        self.supplied(snapshot.usage)
        for index, item in enumerate(snapshot.output):
            self.response_item(index, item)
        self.publish(False)
        if self.err is not None:
            raise self.err

    def event(self, event: _ResponsesStreamEvent) -> None:
        part = _Part(event.output_index, event.content_index, "")
        snapshot = False
        value = ""
        event_type = event.type
        if event_type in ("response.output_text.delta", "response.output_text.done"):
            part = _Part(event.output_index, event.content_index, "output_text")
        elif event_type in (
            "response.reasoning_text.delta",
            "response.reasoning_text.done",
        ):
            part = _Part(event.output_index, event.content_index, "reasoning_text")
        elif event_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
        ):
            part = _Part(event.output_index, event.summary_index, "summary_text")
        elif event_type in (
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        ):
            part = _Part(event.output_index, 0, "function_arguments")
        elif event_type in ("response.output_item.added", "response.output_item.done"):
            self.response_item(event.output_index, event.item)
        elif event_type in (
            "response.completed",
            "response.failed",
            "response.incomplete",
            "response.cancelled",
        ):
            self.response(event.response)
            return
        if part.kind:
            if event_type in (
                "response.output_text.done",
                "response.reasoning_text.done",
                "response.reasoning_summary_text.done",
            ):
                value, snapshot = event.text, True
            elif event_type == "response.function_call_arguments.done":
                value, snapshot = event.arguments, True
            else:
                try:
                    value = _json_string(event.delta)
                except ValueError:
                    value = ""
            self.record_part(part, _utf8_len(value), snapshot)
        self.publish(False)
        if self.err is not None:
            raise self.err


def _begin_usage(
    observer: Callable[[UsageUpdate], None] | None,
    messages: list[Message],
    tools: list[ToolDefinition] | None,
) -> _RequestUsage | None:
    if observer is None:
        return None
    input_bytes = 12
    for message in messages:
        input_bytes += (
            _utf8_len(_role_value(message.role))
            + _utf8_len(message.content)
            + _utf8_len(message.type)
            + _utf8_len(message.call_id)
            + _utf8_len(message.name)
            + _utf8_len(message.arguments)
            + _utf8_len(message.id)
            + 16
        )
    for tool in tools or []:
        try:
            parameter_bytes = _utf8_len(
                json.dumps(tool.parameters, ensure_ascii=False, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            parameter_bytes = _utf8_len(str(tool.parameters))
        input_bytes += (
            _utf8_len(tool.type)
            + _utf8_len(tool.name)
            + _utf8_len(tool.description)
            + parameter_bytes
            + 32
        )
    return _RequestUsage(observer, (input_bytes + 3) // 4)


def _account_unreadable_body(usage: _RequestUsage | None, body: str, invalid: bool) -> None:
    if usage is not None and invalid:
        usage.safe_add(_utf8_len(body))


def _json_valid(text: str) -> bool:
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Native stream state helpers
# ---------------------------------------------------------------------------


class _ResponseCallState:
    def __init__(self) -> None:
        self.id = ""
        self.call_id = ""
        self.name = ""
        self.arguments = ""
        self.snapshot = False

    def merge(self, item: _ResponsesOutputItem, final: bool) -> None:
        for current, incoming in (
            (self.id, item.id),
            (self.call_id, item.call_id),
            (self.name, item.name),
        ):
            if current and incoming and current != incoming:
                raise ToolProtocolError("conflicting function identity in stream")
        if item.id:
            self.id = item.id
        if item.call_id:
            self.call_id = item.call_id
        if item.name:
            self.name = item.name
        if final or item.arguments:
            if not item.arguments.startswith(self.arguments) or (
                self.snapshot and self.arguments != item.arguments
            ):
                raise ToolProtocolError("conflicting function arguments in stream")
            self.arguments += item.arguments[len(self.arguments) :]
            self.snapshot = final
        if final and item.status and item.status != "completed":
            raise ToolProtocolError("incomplete function item")


class _ChatCallState:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments = ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_LONG_API_KEY_ERROR = (
    "missing API key; set openai.api_key directly, or set openai.api_key_env "
    "to an environment variable name"
)


def _unsupported_parameter(error: APIError, *names: str) -> bool:
    """Whether a provider rejected one of the named request parameters."""
    if error.status_code not in (400, 404, 422):
        return False
    text = (error.body or "").lower()
    return any(name.lower() in text for name in names)


def _unsupported_continuation(error: APIError) -> bool:
    """Whether a provider cannot resolve a tool output under previous_response_id."""
    if error.status_code not in (400, 409, 422):
        return False
    text = (error.body or "").lower()
    markers = (
        "previous_response_id",
        "tool output with call_id",
        "no tool call found for tool output",
        "unknown tool call",
    )
    return any(marker in text for marker in markers)


# Provider capabilities are endpoint-scoped: one incompatible request on a
# thinking-mode endpoint should not be repeated by every audit worker.
_UNSUPPORTED_TOOL_CHOICE: set[tuple[str, str]] = set()
_UNSUPPORTED_PARALLEL_CALLS: set[tuple[str, str]] = set()
_UNSUPPORTED_RESPONSES_CONTINUATION: set[tuple[str, str]] = set()
_CONTINUATION_NEEDS_CALLS: set[tuple[str, str]] = set()


_TEXT_DELTA_EVENT_TYPES = (
    "response.output_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta",
)

_TEXT_EVENT_TYPES = _TEXT_DELTA_EVENT_TYPES + (
    "response.output_text.done",
    "response.reasoning_text.done",
    "response.reasoning_summary_text.done",
)


class OpenAIClient:
    """Async client for one provider configuration."""

    def __init__(self, cfg: OpenAIConfig):
        self.cfg = cfg
        # Exposed as a float so tests and callers may use sub-second values.
        self.timeout_seconds: float = float(cfg.timeout_seconds or 0)
        # Some thinking-mode endpoints reject tool_choice.  Remember the
        # capability per endpoint/model so later requests skip the parameter.
        self._capability_key = ((cfg.base_url or "").rstrip("/").lower(), (cfg.model or "").lower())
        self._tool_choice_supported = self._capability_key not in _UNSUPPORTED_TOOL_CHOICE
        self._parallel_calls_supported = self._capability_key not in _UNSUPPORTED_PARALLEL_CALLS
        self._continuation_supported = (
            self._capability_key not in _UNSUPPORTED_RESPONSES_CONTINUATION
        )
        self._continuation_needs_calls = (
            self._capability_key in _CONTINUATION_NEEDS_CALLS
        )
    # -- public API --------------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        *,
        emit: Callable[[Delta], None] | None = None,
        observer: Callable[[UsageUpdate], None] | None = None,
        previous_response_id: str = "",
        continuation: list[Message] | None = None,
    ) -> str:
        """Return the assistant answer for ``messages``.

        Streaming configuration selects the SSE transport; the answer is
        returned as one string with reasoning wrapped in ``<think>`` tags.
        """
        messages = list(messages or [])
        if self.cfg.stream:
            return await self.chat_stream(
                messages,
                emit=emit,
                observer=observer,
                previous_response_id=previous_response_id,
                continuation=continuation,
            )
        self._require_api_key(_LONG_API_KEY_ERROR)
        usage = _begin_usage(observer, messages, None)
        try:
            # Let a cancellation raised by the usage observer take effect before
            # any network request is admitted.
            await asyncio.sleep(0)
            if self._use_responses_api():
                return await self._responses_chat(
                    messages, usage, previous_response_id, continuation
                )
            return await self._chat_completions_chat(messages, usage)
        finally:
            if usage is not None:
                usage.finish()

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        emit: Callable[[Delta], None] | None = None,
        observer: Callable[[UsageUpdate], None] | None = None,
        previous_response_id: str = "",
        continuation: list[Message] | None = None,
    ) -> str:
        """Stream one answer, forwarding deltas to ``emit``.

        Returns the joined answer (reasoning wrapped in ``<think>`` tags) after
        the terminal stream event; partial output is never returned on error.
        """
        messages = list(messages or [])
        self._require_api_key(_LONG_API_KEY_ERROR)
        usage = _begin_usage(observer, messages, None)
        try:
            # Let a cancellation raised by the usage observer take effect before
            # any network request is admitted.
            await asyncio.sleep(0)
            if self._use_responses_api():
                return await self._responses_stream(
                    messages, emit, usage, previous_response_id, continuation
                )
            return await self._chat_completions_stream(messages, emit, usage)
        finally:
            if usage is not None:
                usage.finish()

    async def chat_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        emit: Callable[[Delta], None] | None = None,
        observer: Callable[[UsageUpdate], None] | None = None,
        previous_response_id: str = "",
        continuation: list[Message] | None = None,
    ) -> ToolResponse:
        """Run one native tool-call turn.

        Every parsed call is exposed; malformed protocol history or stream
        payloads raise :class:`ToolProtocolError` and release no calls.
        """
        messages = list(messages or [])
        tools = list(tools or [])
        self._require_api_key("missing API key")
        validate_tool_history(messages)
        usage = _begin_usage(observer, messages, tools)
        try:
            # Let a cancellation raised by the usage observer take effect before
            # any network request is admitted.
            await asyncio.sleep(0)
            if self._use_responses_api():
                return await self._responses_tools(
                    messages,
                    tools,
                    emit,
                    usage,
                    previous_response_id,
                    continuation,
                )
            return await self._chat_completions_tools(
                messages,
                tools,
                emit,
                usage,
                previous_response_id,
                continuation,
            )
        finally:
            if usage is not None:
                usage.finish()

    async def close(self) -> None:
        """Release the shared transport session (best effort)."""
        close = getattr(_TRANSPORT, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    # -- request plumbing --------------------------------------------------

    def _require_api_key(self, message: str) -> None:
        if not self.cfg.api_key:
            raise LLMError(message)

    def _use_responses_api(self) -> bool:
        api = (self.cfg.api_interface or "").strip()
        return api == "" or api.lower() == "responses"

    def _endpoint(self, path: str) -> str:
        return (self.cfg.base_url or "").rstrip("/") + path

    def _headers(self, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        return headers

    async def _post(
        self, url: str, payload: dict[str, Any], stream: bool
    ) -> Any:
        timeout = self.timeout_seconds if self.timeout_seconds > 0 else None
        call = asyncio.to_thread(
            _TRANSPORT.post, url, self._headers(stream), payload, stream, timeout
        )
        try:
            if timeout is None:
                return await call
            return await asyncio.wait_for(call, timeout)
        except asyncio.TimeoutError:
            raise self._timeout_error(stream, timeout) from None
        except requests.RequestException as exc:
            raise _classify_transport_error(exc, stream, timeout) from exc

    def _timeout_error(self, stream: bool, timeout: float | None) -> NetworkError:
        seconds = timeout or 0.0
        if stream:
            return NetworkTimeoutError(
                f"openai stream: no network progress for {_format_duration(seconds)}"
            )
        return NetworkTimeoutError(
            f"openai request: no network progress for {_format_duration(seconds)}"
        )

    async def _open_stream(self, url: str, payload: dict[str, Any]) -> Any:
        response = await self._post(url, payload, True)
        try:
            status = int(getattr(response, "status_code", 0))
            if status < 200 or status >= 300:
                body = _read_response_text(
                    response,
                    RESPONSES_MAX_BYTES,
                    f"openai responses: response body exceeds {RESPONSES_MAX_BYTES} bytes",
                )
                raise APIError(status, body)
        except BaseException:
            _close_response(response)
            raise
        return response

    def _chat_stream_payload(self, messages: list[Message], include_usage: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [_message_json(message) for message in messages],
            "temperature": self.cfg.temperature,
            "top_p": self.cfg.top_p,
        }
        if self.cfg.max_output_tokens:
            payload["max_tokens"] = self.cfg.max_output_tokens
        payload["stream"] = True
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        thinking = _thinking_json(self.cfg.thinking)
        if thinking is not None:
            payload["thinking"] = thinking
        if self.cfg.reasoning_effort:
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        return payload

    def _responses_payload(
        self,
        request_messages: list[Message],
        previous_response_id: str,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "input": _responses_input(request_messages),
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if self.cfg.temperature:
            payload["temperature"] = self.cfg.temperature
        if self.cfg.top_p:
            payload["top_p"] = self.cfg.top_p
        if self.cfg.max_output_tokens:
            payload["max_output_tokens"] = self.cfg.max_output_tokens
        if stream:
            payload["stream"] = True
        thinking = _thinking_json(self.cfg.thinking)
        if thinking is not None:
            payload["thinking"] = thinking
        if self.cfg.reasoning_effort:
            payload["reasoning"] = {"effort": self.cfg.reasoning_effort}
        return payload

    @staticmethod
    def _request_messages(
        messages: list[Message],
        previous_response_id: str,
        continuation: list[Message] | None,
    ) -> tuple[str, list[Message]]:
        previous = (previous_response_id or "").strip()
        if not previous:
            return "", messages
        return previous, list(continuation or [])

    def _new_reader(self, response: Any) -> _SSEReader:
        wire_limit = _response_wire_limit(_native_text_limit(self.cfg.max_output_tokens))
        reader = _SSEReader(response, wire_limit, self.timeout_seconds)
        reader.start()
        return reader

    # -- plain streaming ---------------------------------------------------

    async def _responses_stream(
        self,
        messages: list[Message],
        emit: Callable[[Delta], None] | None,
        usage: _RequestUsage | None,
        previous_response_id: str,
        continuation: list[Message] | None,
    ) -> str:
        previous, request_messages = self._request_messages(
            messages, previous_response_id, continuation
        )
        payload = self._responses_payload(request_messages, previous, True)
        response = await self._open_stream(self._endpoint("/responses"), payload)
        reader = self._new_reader(response)
        seen: set[_Part] = set()
        thinking: list[str] = []
        content: list[str] = []

        def emit_delta(delta: Delta) -> None:
            if delta.thinking:
                thinking.append(delta.thinking)
            if delta.content:
                content.append(delta.content)
            if emit is not None:
                emit(delta)

        try:
            while True:
                line = await reader.next_line()
                if line is None:
                    raise LLMError(
                        "openai responses: stream ended without response.completed: "
                        "unexpected EOF"
                    )
                trimmed = line.strip()
                if not trimmed.startswith("data:"):
                    continue
                value = trimmed[len("data:") :].strip()
                if value == "[DONE]":
                    raise LLMError(
                        "openai responses: stream ended without response.completed: "
                        "unexpected EOF"
                    )
                try:
                    data = json.loads(value)
                except (TypeError, ValueError) as exc:
                    _account_unreadable_body(usage, value, True)
                    raise LLMError(f"openai responses event: {exc}") from exc
                event = _ResponsesStreamEvent.from_json(data)
                if usage is not None:
                    usage.event(event)
                event_type = event.type
                if event_type in _TEXT_DELTA_EVENT_TYPES:
                    try:
                        text = _json_string(event.delta)
                    except ValueError as exc:
                        raise LLMError(f"openai responses delta: {exc}") from exc
                    if not text:
                        continue
                    if event_type == "response.output_text.delta":
                        part = _Part(event.output_index, event.content_index, "output_text")
                        delta = Delta(content=text)
                    elif event_type == "response.reasoning_text.delta":
                        part = _Part(event.output_index, event.content_index, "reasoning_text")
                        delta = Delta(thinking=text)
                    else:
                        part = _Part(event.output_index, event.summary_index, "summary_text")
                        delta = Delta(thinking=text)
                    if part not in seen:
                        if len(seen) >= _MAX_PARTS:
                            raise LLMError(
                                "openai responses: too many streamed content parts"
                            )
                        seen.add(part)
                    reader.activity()
                    emit_delta(delta)
                elif event_type == "response.completed":
                    event.response.completed()
                    for part, delta in _iter_text_parts(event.response):
                        if part in seen:
                            continue
                        seen.add(part)
                        emit_delta(delta)
                    return _join_assistant_parts("".join(thinking), "".join(content))
                elif event_type == "response.incomplete":
                    _raise_incomplete(event.response)
                elif event_type in ("response.failed", "response.cancelled"):
                    raise LLMError(
                        f"openai responses {event_type}: "
                        f"{event.response.failure_detail()}"
                    )
                elif event_type in ("error", "response.error"):
                    raise LLMError(
                        "openai responses error: "
                        f"{event.code} {event.error.code} "
                        f"{_first_non_empty(event.message, event.error.message, 'unspecified API error')}"
                    )
        finally:
            reader.close()

    async def _chat_completions_stream(
        self,
        messages: list[Message],
        emit: Callable[[Delta], None] | None,
        usage: _RequestUsage | None,
    ) -> str:
        payload = self._chat_stream_payload(messages, usage is not None)
        response = await self._open_stream(self._endpoint("/chat/completions"), payload)
        reader = self._new_reader(response)
        thinking: list[str] = []
        content: list[str] = []
        try:
            while True:
                line = await reader.next_line()
                if line is None:
                    raise LLMError(
                        "openai chat completions: stream ended without [DONE]: "
                        "unexpected EOF"
                    )
                trimmed = line.strip()
                if not trimmed.startswith("data:"):
                    continue
                value = trimmed[len("data:") :].strip()
                if value == "[DONE]":
                    return _join_assistant_parts("".join(thinking), "".join(content))
                try:
                    chunk = json.loads(value)
                except (TypeError, ValueError) as exc:
                    _account_unreadable_body(usage, value, True)
                    raise LLMError(f"invalid chat completions event: {exc}") from exc
                if not isinstance(chunk, dict):
                    raise LLMError("invalid chat completions event: not an object")
                if usage is not None:
                    usage.supplied(_GenerationUsage.from_json(chunk.get("usage")))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    if not isinstance(delta, dict):
                        delta = {}
                    if usage is not None:
                        usage.chat(delta)
                    delta_content = _text_field(delta, "content")
                    delta_thinking = _first_non_empty(
                        _text_field(delta, "reasoning_content"),
                        _text_field(delta, "reasoning"),
                        _text_field(delta, "reasoning_text"),
                    )
                    if not delta_content and not delta_thinking:
                        continue
                    reader.activity()
                    delta_obj = Delta(content=delta_content, thinking=delta_thinking)
                    if delta_content:
                        content.append(delta_content)
                    if delta_thinking:
                        thinking.append(delta_thinking)
                    if emit is not None:
                        emit(delta_obj)
        finally:
            reader.close()

    # -- plain non-stream --------------------------------------------------

    async def _chat_completions_chat(
        self, messages: list[Message], usage: _RequestUsage | None
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [_message_json(message) for message in messages],
            "temperature": self.cfg.temperature,
            "top_p": self.cfg.top_p,
        }
        if self.cfg.max_output_tokens:
            payload["max_tokens"] = self.cfg.max_output_tokens
        thinking = _thinking_json(self.cfg.thinking)
        if thinking is not None:
            payload["thinking"] = thinking
        if self.cfg.reasoning_effort:
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        response = await self._post(self._endpoint("/chat/completions"), payload, False)
        try:
            body = _read_response_text(response, None, "")
            _account_unreadable_body(usage, body, not _json_valid(body))
            status = int(getattr(response, "status_code", 0))
            if status < 200 or status >= 300:
                raise APIError(status, body)
            try:
                parsed = json.loads(body)
            except (TypeError, ValueError) as exc:
                raise LLMError(f"openai chat completions: invalid response: {exc}") from exc
            if not isinstance(parsed, dict):
                raise LLMError("openai chat completions: invalid response")
            if usage is not None:
                usage.supplied(_GenerationUsage.from_json(parsed.get("usage")))
                for choice in parsed.get("choices") or []:
                    if isinstance(choice, dict):
                        usage.chat(choice.get("message") or {})
            choices = parsed.get("choices") or []
            if not choices:
                raise LLMError("openai returned no choices")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if not isinstance(message, dict):
                message = {}
            return _join_assistant_parts(
                _first_non_empty(
                    _text_field(message, "reasoning_content"),
                    _text_field(message, "reasoning"),
                    _text_field(message, "reasoning_text"),
                ),
                _text_field(message, "content"),
            )
        finally:
            _close_response(response)

    async def _responses_chat(
        self,
        messages: list[Message],
        usage: _RequestUsage | None,
        previous_response_id: str,
        continuation: list[Message] | None,
    ) -> str:
        previous, request_messages = self._request_messages(
            messages, previous_response_id, continuation
        )
        payload = self._responses_payload(request_messages, previous, False)
        response = await self._post(self._endpoint("/responses"), payload, False)
        try:
            body = _read_response_text(
                response,
                RESPONSES_MAX_BYTES,
                f"openai responses: response body exceeds {RESPONSES_MAX_BYTES} bytes",
            )
            _account_unreadable_body(usage, body, not _json_valid(body))
            status = int(getattr(response, "status_code", 0))
            if status < 200 or status >= 300:
                raise APIError(status, body)
            try:
                parsed_json = json.loads(body)
            except (TypeError, ValueError) as exc:
                raise LLMError(f"openai responses: invalid response: {exc}") from exc
            parsed = _ResponsesResponse.from_json(parsed_json)
            if usage is not None:
                usage.response(parsed)
            parsed.completed()
            thinking: list[str] = []
            content: list[str] = []
            for _part, delta in _iter_text_parts(parsed):
                if delta.thinking:
                    thinking.append(delta.thinking)
                if delta.content:
                    content.append(delta.content)
            return _join_assistant_parts("".join(thinking), "".join(content))
        finally:
            _close_response(response)

    # -- native tools: Responses ------------------------------------------

    def _tool_response(self, text: "_ToolText", calls: list[FunctionCall]) -> ToolResponse:
        return text.result(calls, multi_call_fallback=not self._tool_choice_supported)

    @staticmethod
    def _continuation_with_calls(
        messages: list[Message], continuation: list[Message]
    ) -> list[Message]:
        """Prepend matching function_call items before their outputs.

        Some compatible endpoints cannot resolve an output under
        previous_response_id unless the original call item is repeated.
        """
        calls = {
            message.call_id: message
            for message in messages
            if message.type == "function_call" and message.call_id
        }
        augmented: list[Message] = []
        for message in continuation:
            if message.type == "function_call_output":
                call = calls.get(message.call_id)
                if call is not None:
                    augmented.append(call)
            augmented.append(message)
        return augmented

    async def _responses_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        emit: Callable[[Delta], None] | None,
        usage: _RequestUsage | None,
        previous_response_id: str,
        continuation: list[Message] | None,
    ) -> ToolResponse:
        progress = _GenerationTracker(emit)
        progress.text_limit = _native_text_limit(self.cfg.max_output_tokens)
        try:
            last_error: APIError | None = None
            for _attempt in range(6):
                if self._continuation_supported:
                    previous, request_messages = self._request_messages(
                        messages, previous_response_id, continuation
                    )
                    if previous and self._continuation_needs_calls:
                        request_messages = self._continuation_with_calls(
                            messages, request_messages
                        )
                else:
                    previous, request_messages = "", list(messages)
                payload = self._responses_tools_payload(
                    request_messages, previous, tools
                )
                try:
                    return await self._responses_tools_request(
                        payload, usage, progress
                    )
                except APIError as exc:
                    last_error = exc
                    if _unsupported_continuation(exc):
                        if not self._continuation_needs_calls:
                            # Retry with the original call items included.
                            _CONTINUATION_NEEDS_CALLS.add(self._capability_key)
                            self._continuation_needs_calls = True
                            continue
                        # The endpoint cannot resume this history at all;
                        # send the full history instead.
                        _UNSUPPORTED_RESPONSES_CONTINUATION.add(self._capability_key)
                        self._continuation_supported = False
                        continue
                    rejected_choice = _unsupported_parameter(exc, "tool_choice")
                    rejected_parallel = _unsupported_parameter(
                        exc, "parallel_tool_calls"
                    )
                    if not (rejected_choice or rejected_parallel):
                        raise
                    if rejected_choice:
                        _UNSUPPORTED_TOOL_CHOICE.add(self._capability_key)
                        self._tool_choice_supported = False
                    if rejected_parallel:
                        _UNSUPPORTED_PARALLEL_CALLS.add(self._capability_key)
                        self._parallel_calls_supported = False
            assert last_error is not None
            raise last_error
        finally:
            progress.flush_pending()

    def _responses_tools_payload(
        self,
        request_messages: list[Message],
        previous: str,
        tools: list[ToolDefinition],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "input": _native_responses_input(request_messages),
        }
        if previous:
            payload["previous_response_id"] = previous
        payload["tools"] = [tool.to_json() for tool in tools]
        if self._tool_choice_supported:
            payload["tool_choice"] = "required"
        if self._parallel_calls_supported:
            payload["parallel_tool_calls"] = False
        payload["temperature"] = self.cfg.temperature
        payload["top_p"] = self.cfg.top_p
        if self.cfg.max_output_tokens:
            payload["max_output_tokens"] = self.cfg.max_output_tokens
        payload["stream"] = bool(self.cfg.stream)
        thinking = _thinking_json(self.cfg.thinking)
        if thinking is not None:
            payload["thinking"] = thinking
        if self.cfg.reasoning_effort:
            payload["reasoning"] = {"effort": self.cfg.reasoning_effort}
        return payload

    async def _responses_tools_request(
        self,
        payload: dict[str, Any],
        usage: _RequestUsage | None,
        progress: "_GenerationTracker",
    ) -> ToolResponse:
        if self.cfg.stream:
            response = await self._open_stream(self._endpoint("/responses"), payload)
            reader = self._new_reader(response)
            progress.activity = reader.activity
            try:
                return await self._consume_native_responses(reader, usage, progress)
            finally:
                reader.close()
        return await self._native_responses_nonstream(payload, usage, progress)

    async def _consume_native_responses(
        self,
        reader: _SSEReader,
        usage: _RequestUsage | None,
        progress: _GenerationTracker,
    ) -> ToolResponse:
        text = _ToolText(progress.text, progress.text_limit)
        seen: dict[_Part, str] = {}
        states: dict[int, _ResponseCallState] = {}

        def add_part(part: _Part, delta: Delta, snapshot: bool) -> None:
            value = delta.content if part.kind == "output_text" else delta.thinking
            previous = seen.get(part)
            if previous is None:
                if len(seen) >= 1024:
                    raise LLMError("too many native text parts")
                previous = ""
                seen[part] = ""
            if snapshot:
                if not value.startswith(previous):
                    raise ToolProtocolError("conflicting text snapshot")
                value = value[len(previous) :]
            seen[part] = previous + value
            if part.kind == "output_text":
                text.add(Delta(content=value))
            else:
                text.add(Delta(thinking=value))

        def state_for(index: int) -> _ResponseCallState:
            if index < 0 or index > 1024:
                raise ToolProtocolError("invalid output_index")
            state = states.get(index)
            if state is None:
                state = _ResponseCallState()
                states[index] = state
            return state

        while True:
            line = await reader.next_line()
            if line is None:
                raise LLMError(
                    "openai native Responses missing response.completed: unexpected EOF"
                )
            trimmed = line.strip()
            if not trimmed.startswith("data:"):
                continue
            value = trimmed[len("data:") :].strip()
            if value == "[DONE]":
                raise LLMError(
                    "openai native Responses missing response.completed: unexpected EOF"
                )
            try:
                data = json.loads(value)
            except (TypeError, ValueError) as exc:
                if usage is not None:
                    usage.safe_add(_utf8_len(value))
                raise ToolProtocolError("invalid Responses event", str(exc)) from exc
            event = _ResponsesStreamEvent.from_json(data)
            if usage is not None:
                usage.event(event)
            event_type = event.type
            if event_type in _TEXT_EVENT_TYPES:
                snapshot = event_type.endswith(".done")
                if snapshot:
                    text_value = event.text
                else:
                    try:
                        text_value = _json_string(event.delta)
                    except ValueError as exc:
                        raise ToolProtocolError("invalid text delta", str(exc)) from exc
                if not text_value:
                    continue
                if event_type.startswith("response.output_text"):
                    part = _Part(event.output_index, event.content_index, "output_text")
                    delta = Delta(content=text_value)
                elif event_type.startswith("response.reasoning_text"):
                    part = _Part(event.output_index, event.content_index, "reasoning_text")
                    delta = Delta(thinking=text_value)
                else:
                    part = _Part(event.output_index, event.summary_index, "summary_text")
                    delta = Delta(thinking=text_value)
                add_part(part, delta, snapshot)
            elif event_type in ("response.output_item.added", "response.output_item.done"):
                item = event.item
                if item.type != "function_call":
                    for part, delta in _iter_text_parts(
                        _ResponsesResponse(output=[item])
                    ):
                        add_part(_Part(event.output_index, part.index, part.kind), delta, True)
                    continue
                state = state_for(event.output_index)
                before = _utf8_len(state.arguments)
                state.merge(item, event_type == "response.output_item.done")
                progress.add(0, 0, _utf8_len(state.arguments) - before)
            elif event_type in (
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
            ):
                state = state_for(event.output_index)
                if event.item_id:
                    if state.id and state.id != event.item_id:
                        raise ToolProtocolError("argument delta item_id mismatch")
                    state.id = event.item_id
                before = _utf8_len(state.arguments)
                if event_type == "response.function_call_arguments.done":
                    state.merge(_ResponsesOutputItem(arguments=event.arguments), True)
                else:
                    if state.snapshot:
                        raise ToolProtocolError("arguments delta after final snapshot")
                    try:
                        argument_value = _json_string(event.delta)
                    except ValueError as exc:
                        raise ToolProtocolError("invalid arguments delta", str(exc)) from exc
                    if (
                        _utf8_len(state.arguments) + _utf8_len(argument_value)
                        > RESPONSES_MAX_BYTES
                    ):
                        raise ToolProtocolError("function arguments too large")
                    state.arguments += argument_value
                progress.add(0, 0, _utf8_len(state.arguments) - before)
            elif event_type == "response.completed":
                snapshot_response = event.response
                snapshot_response.completed()
                for index, item in enumerate(snapshot_response.output):
                    if item.type != "function_call":
                        continue
                    state = state_for(index)
                    before = _utf8_len(state.arguments)
                    state.merge(item, True)
                    progress.add(0, 0, _utf8_len(state.arguments) - before)
                calls = [
                    FunctionCall(
                        id=states[index].id,
                        call_id=states[index].call_id,
                        name=states[index].name,
                        arguments=states[index].arguments,
                    )
                    for index in sorted(states)
                ]
                _validate_calls(calls)
                for part, delta in _iter_text_parts(snapshot_response):
                    add_part(part, delta, True)
                progress.finish(snapshot_response.usage)
                result = self._tool_response(text, calls)
                result.response_id = snapshot_response.id
                return result
            elif event_type == "response.incomplete":
                _raise_incomplete(event.response)
            elif event_type in ("response.failed", "response.cancelled"):
                raise LLMError(
                    f"openai native {event_type}: {event.response.failure_detail()}"
                )
            elif event_type in ("error", "response.error"):
                raise LLMError(
                    "openai native error: "
                    f"{event.code} {event.error.code} "
                    f"{_first_non_empty(event.message, event.error.message, 'unspecified API error')}"
                )

    async def _native_responses_nonstream(
        self,
        payload: dict[str, Any],
        usage: _RequestUsage | None,
        progress: _GenerationTracker,
    ) -> ToolResponse:
        response = await self._post(self._endpoint("/responses"), payload, False)
        try:
            body = _read_response_text(
                response,
                _response_wire_limit(progress.text_limit),
                "openai native response body exceeds "
                f"{_response_wire_limit(progress.text_limit)} bytes",
            )
            status = int(getattr(response, "status_code", 0))
            if status < 200 or status >= 300:
                raise APIError(status, body)
            try:
                parsed_json = json.loads(body)
            except (TypeError, ValueError) as exc:
                _account_unreadable_body(usage, body, True)
                raise ToolProtocolError("invalid Responses response", str(exc)) from exc
            parsed = _ResponsesResponse.from_json(parsed_json)
            if usage is not None:
                usage.response(parsed)
            parsed.completed()
            text = _ToolText(progress.text, progress.text_limit)
            for _part, delta in _iter_text_parts(parsed):
                text.add(delta)
            calls: list[FunctionCall] = []
            for item in parsed.output:
                if item.type != "function_call":
                    continue
                if item.status and item.status != "completed":
                    raise ToolProtocolError("function call did not complete")
                calls.append(
                    FunctionCall(
                        id=item.id,
                        call_id=item.call_id,
                        name=item.name,
                        arguments=item.arguments,
                    )
                )
                progress.add(0, 0, _utf8_len(item.arguments))
            _validate_calls(calls)
            progress.finish(parsed.usage)
            result = self._tool_response(text, calls)
            result.response_id = parsed.id
            return result
        finally:
            _close_response(response)

    # -- native tools: Chat Completions -----------------------------------

    async def _chat_completions_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        emit: Callable[[Delta], None] | None,
        usage: _RequestUsage | None,
        previous_response_id: str,
        continuation: list[Message] | None,
    ) -> ToolResponse:
        del previous_response_id, continuation  # Chat Completions has no continuation.
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": _native_chat_input(messages),
            "tools": _native_chat_tools(tools),
            "temperature": self.cfg.temperature,
            "top_p": self.cfg.top_p,
        }
        if self._parallel_calls_supported:
            payload["parallel_tool_calls"] = False
        if self.cfg.max_output_tokens:
            payload["max_tokens"] = self.cfg.max_output_tokens
        payload["stream"] = bool(self.cfg.stream)
        if self.cfg.stream and usage is not None:
            payload["stream_options"] = {"include_usage": True}
        thinking = _thinking_json(self.cfg.thinking)
        if thinking is not None:
            payload["thinking"] = thinking
        if self.cfg.reasoning_effort:
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        progress = _GenerationTracker(emit)
        progress.text_limit = _native_text_limit(self.cfg.max_output_tokens)
        try:
            try:
                return await self._chat_tools_request(payload, usage, progress)
            except APIError as exc:
                if not (
                    _unsupported_parameter(exc, "tool_choice")
                    or _unsupported_parameter(exc, "parallel_tool_calls")
                ):
                    raise
                _UNSUPPORTED_PARALLEL_CALLS.add(self._capability_key)
                self._parallel_calls_supported = False
                retry_payload = {
                    key: value
                    for key, value in payload.items()
                    if key != "parallel_tool_calls"
                }
                return await self._chat_tools_request(retry_payload, usage, progress)
        finally:
            progress.flush_pending()

    async def _chat_tools_request(
        self,
        payload: dict[str, Any],
        usage: _RequestUsage | None,
        progress: "_GenerationTracker",
    ) -> ToolResponse:
        if self.cfg.stream:
            response = await self._open_stream(
                self._endpoint("/chat/completions"), payload
            )
            reader = self._new_reader(response)
            progress.activity = reader.activity
            try:
                return await self._consume_native_chat(reader, usage, progress)
            finally:
                reader.close()
        return await self._native_chat_nonstream(payload, usage, progress)

    async def _consume_native_chat(
        self,
        reader: _SSEReader,
        usage: _RequestUsage | None,
        progress: _GenerationTracker,
    ) -> ToolResponse:
        text = _ToolText(progress.text, progress.text_limit)
        states: dict[int, _ChatCallState] = {}
        finish = ""
        last_usage: _GenerationUsage | None = None
        while True:
            line = await reader.next_line()
            if line is None:
                raise LLMError("openai native chat missing [DONE]: unexpected EOF")
            trimmed = line.strip()
            if not trimmed.startswith("data:"):
                continue
            value = trimmed[len("data:") :].strip()
            if value == "[DONE]":
                if finish not in ("stop", "tool_calls"):
                    _raise_unfinished_chat(finish)
                calls = [
                    FunctionCall(
                        call_id=states[index].id,
                        name=states[index].name,
                        arguments=states[index].arguments,
                    )
                    for index in sorted(states)
                ]
                _validate_calls(calls)
                progress.finish(last_usage)
                return self._tool_response(text, calls)
            try:
                chunk = json.loads(value)
            except (TypeError, ValueError) as exc:
                if usage is not None:
                    usage.safe_add(_utf8_len(value))
                raise ToolProtocolError("invalid chat event", str(exc)) from exc
            if not isinstance(chunk, dict):
                raise ToolProtocolError("invalid chat event")
            generation_usage = _GenerationUsage.from_json(chunk.get("usage"))
            if usage is not None:
                usage.supplied(generation_usage)
                for choice in chunk.get("choices") or []:
                    if isinstance(choice, dict):
                        delta = choice.get("delta")
                        if isinstance(delta, dict):
                            usage.chat(delta)
            error_payload = _APIErrorPayload.from_json(chunk.get("error"))
            if error_payload.message or error_payload.code:
                raise LLMError(
                    f"openai native chat error: {error_payload.code} "
                    f"{error_payload.message}"
                )
            if generation_usage is not None:
                last_usage = generation_usage
            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                if _int_field(choice, "index") != 0:
                    raise ToolProtocolError("unexpected chat choice index")
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    delta = {}
                delta_content = _text_field(delta, "content")
                delta_thinking = _first_non_empty(
                    _text_field(delta, "reasoning_content"),
                    _text_field(delta, "reasoning"),
                    _text_field(delta, "reasoning_text"),
                )
                tool_calls = delta.get("tool_calls") or []
                if finish and (
                    delta_content or delta_thinking or len(tool_calls) > 0
                ):
                    raise ToolProtocolError("chat delta after finish")
                text.add(Delta(content=delta_content, thinking=delta_thinking))
                for call in tool_calls:
                    if not isinstance(call, dict):
                        raise ToolProtocolError("invalid chat tool call")
                    index = _int_field(call, "index")
                    if index < 0 or index > 1024:
                        raise ToolProtocolError("invalid tool call index")
                    call_type = _text_field(call, "type")
                    if call_type and call_type != "function":
                        raise ToolProtocolError("unsupported chat tool type")
                    state = states.get(index)
                    if state is None:
                        state = _ChatCallState()
                        states[index] = state
                    call_id = _text_field(call, "id")
                    if call_id:
                        if state.id and state.id != call_id:
                            raise ToolProtocolError("conflicting chat call ID")
                        state.id = call_id
                    function = call.get("function")
                    if not isinstance(function, dict):
                        function = {}
                    name = _text_field(function, "name")
                    arguments = _text_field(function, "arguments")
                    if (
                        _utf8_len(state.arguments)
                        + _utf8_len(state.name)
                        + _utf8_len(arguments)
                        + _utf8_len(name)
                        > RESPONSES_MAX_BYTES
                    ):
                        raise ToolProtocolError("chat function arguments too large")
                    state.name += name
                    state.arguments += arguments
                    progress.add(0, 0, _utf8_len(arguments))
                finish_reason = _text_field(choice, "finish_reason")
                if finish_reason:
                    finish = finish_reason

    async def _native_chat_nonstream(
        self,
        payload: dict[str, Any],
        usage: _RequestUsage | None,
        progress: _GenerationTracker,
    ) -> ToolResponse:
        response = await self._post(self._endpoint("/chat/completions"), payload, False)
        try:
            body = _read_response_text(
                response,
                _response_wire_limit(progress.text_limit),
                "openai native response body exceeds "
                f"{_response_wire_limit(progress.text_limit)} bytes",
            )
            status = int(getattr(response, "status_code", 0))
            if status < 200 or status >= 300:
                raise APIError(status, body)
            try:
                parsed = json.loads(body)
            except (TypeError, ValueError) as exc:
                _account_unreadable_body(usage, body, True)
                raise ToolProtocolError("invalid chat response", str(exc)) from exc
            if not isinstance(parsed, dict):
                raise ToolProtocolError("invalid chat response")
            if usage is not None:
                usage.supplied(_GenerationUsage.from_json(parsed.get("usage")))
                for choice in parsed.get("choices") or []:
                    if isinstance(choice, dict):
                        usage.chat(choice.get("message") or {})
            error_payload = _APIErrorPayload.from_json(parsed.get("error"))
            if error_payload.message or error_payload.code:
                raise LLMError(
                    f"openai native chat error: {error_payload.code} "
                    f"{error_payload.message}"
                )
            choices = parsed.get("choices") or []
            if len(choices) != 1:
                raise ToolProtocolError("expected exactly one chat choice")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ToolProtocolError("expected exactly one chat choice")
            finish_reason = _text_field(choice, "finish_reason")
            if finish_reason not in ("stop", "tool_calls"):
                _raise_unfinished_chat(finish_reason)
            message = choice.get("message")
            if not isinstance(message, dict):
                message = {}
            text = _ToolText(progress.text, progress.text_limit)
            text.add(
                Delta(
                    content=_text_field(message, "content"),
                    thinking=_first_non_empty(
                        _text_field(message, "reasoning_content"),
                        _text_field(message, "reasoning"),
                        _text_field(message, "reasoning_text"),
                    ),
                )
            )
            calls: list[FunctionCall] = []
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    raise ToolProtocolError("invalid chat tool call")
                if _text_field(call, "type") != "function":
                    raise ToolProtocolError("unsupported chat tool type")
                function = call.get("function")
                if not isinstance(function, dict):
                    function = {}
                arguments = _text_field(function, "arguments")
                calls.append(
                    FunctionCall(
                        call_id=_text_field(call, "id"),
                        name=_text_field(function, "name"),
                        arguments=arguments,
                    )
                )
                progress.add(0, 0, _utf8_len(arguments))
            _validate_calls(calls)
            progress.finish(_GenerationUsage.from_json(parsed.get("usage")))
            return self._tool_response(text, calls)
        finally:
            _close_response(response)


# ---------------------------------------------------------------------------
# Response text helpers
# ---------------------------------------------------------------------------


def _read_response_text(
    response: Any, limit: int | None, overflow_message: str
) -> str:
    try:
        text = response.text
    except Exception as exc:  # noqa: BLE001 - transport surface
        raise NetworkError(f"openai response read failed: {exc}") from exc
    if text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)
    if limit is not None and _utf8_len(text) > limit:
        raise LLMError(overflow_message)
    return text


def _classify_transport_error(
    exc: Exception, stream: bool, timeout: float | None
) -> NetworkError:
    seconds = timeout or 0.0
    if isinstance(exc, requests.Timeout):
        if stream:
            return NetworkTimeoutError(
                f"openai stream: no network progress for {_format_duration(seconds)}"
            )
        return NetworkTimeoutError(
            f"openai request: no network progress for {_format_duration(seconds)}"
        )
    return NetworkError(f"openai network error: {exc}")
