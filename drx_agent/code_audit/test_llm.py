"""Tests for the asynchronous model client in :mod:`drx_agent.code_audit.llm`."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
import requests

from drx_agent.code_audit import llm
from drx_agent.code_audit.config import OpenAIConfig, ThinkingConfig


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    """Duck-typed response: status_code, iter_lines(), text, close()."""

    def __init__(
        self,
        status_code: int = 200,
        lines: list[str] | None = None,
        text: str = "",
        delay: float = 0.0,
        endless: bool = False,
    ):
        self.status_code = status_code
        self._lines = list(lines or [])
        self._text = text
        self._delay = delay
        self._endless = endless
        self.closed = False

    @property
    def text(self) -> str:
        return self._text

    def iter_lines(self):
        for line in self._lines:
            if self.closed:
                return
            yield line
            if self._delay:
                time.sleep(self._delay)
        if self._endless:
            while not self.closed:
                yield ": keepalive"
                time.sleep(0.01)

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url, headers, json_body, stream, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json_body,
                "stream": stream,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected request to {url}")
        item = self.responses.pop(0)
        if callable(item):
            return item()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def install(monkeypatch, *responses) -> FakeTransport:
    transport = FakeTransport(list(responses))
    monkeypatch.setattr(llm, "_TRANSPORT", transport)
    return transport


def make_cfg(api: str = "responses", stream: bool = False, **kwargs) -> OpenAIConfig:
    cfg = OpenAIConfig(
        base_url=kwargs.pop("base_url", "https://api.test/v1"),
        api_interface=api,
        api_key=kwargs.pop("api_key", "secret"),
        model=kwargs.pop("model", "test-model"),
        temperature=kwargs.pop("temperature", 0.0),
        top_p=kwargs.pop("top_p", 0.0),
        max_context_tokens=kwargs.pop("max_context_tokens", 0),
        max_output_tokens=kwargs.pop("max_output_tokens", 0),
        timeout_seconds=kwargs.pop("timeout_seconds", 0),
        stream=stream,
        thinking=kwargs.pop("thinking", None),
        reasoning_effort=kwargs.pop("reasoning_effort", ""),
    )
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def run(coro):
    return asyncio.run(coro)


def sse(*payloads: str) -> list[str]:
    return [f"data: {payload}" for payload in payloads] + [""]


def event(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Shared reply builders
# ---------------------------------------------------------------------------


def responses_completed(output=None, usage=None, response_id="", status="completed") -> FakeResponse:
    body = {"status": status, "output": output or []}
    if response_id:
        body["id"] = response_id
    if usage is not None:
        body["usage"] = usage
    return FakeResponse(text=json.dumps(body))


def responses_completed_event(output=None, usage=None, response_id="", status="completed") -> dict:
    body = {"status": status, "output": output or []}
    if response_id:
        body["id"] = response_id
    if usage is not None:
        body["usage"] = usage
    return {"type": "response.completed", "response": body}


def chat_completed(content: str = "ok", reasoning: str = "") -> FakeResponse:
    message: dict = {"content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    body = {"choices": [{"message": message, "finish_reason": "stop"}]}
    return FakeResponse(text=json.dumps(body))


def chat_stream_done(content: str = "ok", reasoning: str = "") -> list[str]:
    delta: dict = {"content": content}
    if reasoning:
        delta["reasoning_content"] = reasoning
    return sse(
        event({"choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]}),
        "[DONE]",
    )


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def test_estimate_text_tokens_matches_project_estimator():
    assert llm.estimate_text_tokens("") == 0
    assert llm.estimate_text_tokens("a") == 1
    assert llm.estimate_text_tokens("abcd") == 1
    assert llm.estimate_text_tokens("abcde") == 2
    assert llm.estimate_text_tokens("中") == 1
    assert llm.estimate_text_tokens("中文") == 2
    assert llm.estimate_text_tokens("é") == 2
    # ASCII run is rounded up, CJK characters count one each.
    assert llm.estimate_text_tokens("abc中文") == 3


def test_estimate_tokens_uses_per_message_overhead():
    messages = [
        llm.Message(role=llm.Role.USER, content="abcd"),
        llm.Message(role=llm.Role.ASSISTANT, content="", arguments=""),
    ]
    assert llm.estimate_tokens(messages) == 9


# ---------------------------------------------------------------------------
# SSE frame limits and large output
# ---------------------------------------------------------------------------


def test_large_sse_frame_and_terminal_snapshot(monkeypatch):
    text = "x" * (llm.RESPONSES_MAX_BYTES + 65536)
    cfg = make_cfg("responses", stream=True, max_output_tokens=98304)
    chat_cfg = make_cfg("chat", stream=True, max_output_tokens=98304)

    # Plain chat completions.
    install(
        monkeypatch,
        FakeResponse(
            lines=sse(
                event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
                "[DONE]",
            )
        ),
    )
    client = llm.OpenAIClient(chat_cfg)
    assert run(client.chat_stream([])) == text

    # Plain Responses stream: the terminal snapshot repeats the delta.
    install(
        monkeypatch,
        FakeResponse(
            lines=sse(
                event({"type": "response.output_text.delta", "output_index": 0, "delta": text}),
                event(
                    responses_completed_event(
                        output=[
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": text}],
                            }
                        ]
                    )
                ),
            )
        ),
    )
    client = llm.OpenAIClient(cfg)
    assert run(client.chat_stream([])) == text

    # Native Responses stream: deltas plus the repeated terminal snapshot.
    install(
        monkeypatch,
        FakeResponse(
            lines=sse(
                event({"type": "response.output_text.delta", "output_index": 0, "delta": text}),
                event(
                    responses_completed_event(
                        output=[
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": text}],
                            }
                        ]
                    )
                ),
            )
        ),
    )
    client = llm.OpenAIClient(cfg)
    assert run(client.chat_tools([], [])).content == text

    # Native chat completions.
    install(
        monkeypatch,
        FakeResponse(
            lines=sse(
                event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
                "[DONE]",
            )
        ),
    )
    client = llm.OpenAIClient(chat_cfg)
    assert run(client.chat_tools([], [])).content == text


def test_native_text_limit_rejects_oversized_output(monkeypatch):
    payloads = []
    remaining = llm.RESPONSES_MAX_BYTES + 65536
    while remaining > 0:
        chunk = min(65536, remaining)
        payloads.append(
            event(
                {
                    "type": "response.reasoning_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "x" * chunk,
                }
            )
        )
        remaining -= chunk
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat_tools([], []))
    assert "native text exceeds 1048576 bytes" in str(excinfo.value)


def test_sse_frame_limit_is_enforced():
    response = FakeResponse(lines=["data: " + "x" * 100])

    async def scenario():
        reader = llm._SSEReader(response, 10, 0)
        reader.start()
        try:
            with pytest.raises(llm.LLMError) as excinfo:
                await reader.next_line()
            assert "SSE frame exceeds" in str(excinfo.value)
        finally:
            reader.close()

    run(scenario())
    assert response.closed


def test_to_json_matches_wire_shape():
    tool = llm.ToolDefinition(
        name="read_file",
        description="read",
        parameters={"type": "object"},
        strict=True,
    )
    assert tool.to_json() == {
        "type": "function",
        "name": "read_file",
        "description": "read",
        "parameters": {"type": "object"},
        "strict": True,
    }


def test_native_text_limit_does_not_overflow():
    maximum = sys.maxsize
    assert llm._native_text_limit(0) == llm.RESPONSES_MAX_BYTES
    assert llm._native_text_limit(98304) == 98304 * 16
    assert llm._native_text_limit(maximum // 16 + 1) == maximum
    assert llm._native_text_limit(maximum) == maximum


def test_native_nonstream_rejects_oversized_arguments(monkeypatch):
    huge = "x" * (llm.RESPONSES_MAX_BYTES + 1)
    body = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "read_file",
                "arguments": '{"path":"' + huge + '"}',
            }
        ],
    }
    install(monkeypatch, FakeResponse(text=json.dumps(body)))
    client = llm.OpenAIClient(make_cfg("responses", max_output_tokens=98304))
    with pytest.raises(llm.ToolProtocolError) as excinfo:
        run(client.chat_tools([], []))
    assert "arguments too large" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Timeouts and cancellation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api", ["responses", "chat"])
def test_stream_idle_timeout_rejects_heartbeats(monkeypatch, api):
    response = FakeResponse(endless=True)
    install(monkeypatch, response)
    client = llm.OpenAIClient(make_cfg(api, stream=True))
    client.timeout_seconds = 0.12
    with pytest.raises(llm.StreamTimeoutError) as excinfo:
        run(client.chat_stream([]))
    error = excinfo.value
    assert isinstance(error, llm.NetworkError)
    assert isinstance(error, llm.IncompleteGeneration)
    assert error.retryable
    assert "no model output" in str(error)
    assert response.closed


def test_stream_output_progress_refreshes_idle_timeout(monkeypatch):
    payloads = [
        event(
            {
                "type": "response.reasoning_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "x",
            }
        )
        for _ in range(8)
    ]
    payloads.append(event(responses_completed_event()))
    install(monkeypatch, FakeResponse(lines=sse(*payloads), delay=0.03))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    client.timeout_seconds = 0.15
    result = run(client.chat_tools([], []))
    assert result.thinking == "x" * 8


def test_header_wait_timeout(monkeypatch):
    def slow_response():
        time.sleep(0.25)
        return FakeResponse(lines=sse(event(responses_completed_event())))

    install(monkeypatch, slow_response)
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    client.timeout_seconds = 0.08
    with pytest.raises(llm.NetworkTimeoutError) as excinfo:
        run(client.chat_stream([]))
    assert "no network progress" in str(excinfo.value)


def test_timeout_errors_carry_retry_classification():
    header = llm.NetworkTimeoutError("no network progress")
    assert isinstance(header, llm.NetworkError)
    assert isinstance(header, TimeoutError)
    assert header.retryable

    stream = llm.StreamTimeoutError(0.25)
    assert isinstance(stream, llm.NetworkError)
    assert isinstance(stream, llm.IncompleteGeneration)
    assert "model generation incomplete" in str(stream).lower()
    assert "no model output" in str(stream)
    assert stream.retryable


def test_nonstream_request_timeout(monkeypatch):
    def slow_response():
        time.sleep(0.25)
        return responses_completed()

    install(monkeypatch, slow_response)
    client = llm.OpenAIClient(make_cfg("responses"))
    client.timeout_seconds = 0.08
    with pytest.raises(llm.NetworkTimeoutError):
        run(client.chat([]))


def test_transport_timeout_is_retryable_network_error(monkeypatch):
    install(monkeypatch, requests.Timeout("timed out"))
    client = llm.OpenAIClient(make_cfg("chat"))
    with pytest.raises(llm.NetworkError) as excinfo:
        run(client.chat([]))
    assert excinfo.value.retryable


def test_cancellation_closes_stream(monkeypatch):
    response = FakeResponse(
        lines=sse(
            event(
                {
                    "type": "response.reasoning_text.delta",
                    "output_index": 0,
                    "delta": "partial",
                }
            )
        ),
        endless=True,
    )
    install(monkeypatch, response)
    client = llm.OpenAIClient(make_cfg("responses", stream=True))

    async def scenario():
        task = asyncio.create_task(client.chat_tools([], []))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert response.closed


def test_emit_error_aborts_stream(monkeypatch):
    response = FakeResponse(
        lines=sse(
            event({"choices": [{"index": 0, "delta": {"content": "hello"}}]}),
            "[DONE]",
        )
    )
    install(monkeypatch, response)
    client = llm.OpenAIClient(make_cfg("chat", stream=True))

    def emit(delta):
        raise RuntimeError("consumer stopped")

    with pytest.raises(RuntimeError):
        run(client.chat_stream([], emit=emit))
    assert response.closed


# ---------------------------------------------------------------------------
# Responses parsing (plain)
# ---------------------------------------------------------------------------


def test_responses_replays_history_as_easy_messages(monkeypatch):
    output = [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "considered carefully"}],
            "content": [{"type": "output_text", "text": "not an answer"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "answer"},
                {"type": "reasoning_text", "text": "not answer text"},
            ],
        },
        {
            "type": "function_call",
            "content": [{"type": "output_text", "text": "not executable"}],
        },
    ]
    transport = install(monkeypatch, responses_completed(output=output))
    history = [
        llm.Message(role=llm.Role.SYSTEM, content="instructions"),
        llm.Message(role=llm.Role.USER, content="question"),
        llm.Message(role=llm.Role.ASSISTANT, content="prior answer"),
        llm.Message(role=llm.Role.TOOL, content="tool result"),
    ]
    client = llm.OpenAIClient(make_cfg("responses"))
    assert run(client.chat(history)) == "<think>considered carefully</think>answer"
    body = transport.calls[0]["json"]
    assert transport.calls[0]["url"] == "https://api.test/v1/responses"
    assert body["input"] == [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "tool result"},
    ]


def test_responses_stream_separates_reasoning_and_recovers_snapshots(monkeypatch):
    payloads = [
        event(
            {
                "type": "response.reasoning_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "<tool_call>example</tool_call>",
            }
        ),
        event(
            {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "summary_index": 0,
                "delta": "summary",
            }
        ),
        event(
            {
                "type": "response.function_call_arguments.delta",
                "delta": "do not execute",
            }
        ),
        event({"type": "unknown.delta", "delta": {"text": "also not executable"}}),
        event(
            {
                "type": "response.output_text.delta",
                "output_index": 1,
                "content_index": 0,
                "delta": "answer",
            }
        ),
        event({"type": "response.output_text.done", "text": "answer"}),
        event(
            {
                "type": "response.reasoning_text.done",
                "text": "<tool_call>example</tool_call>",
            }
        ),
        event(
            responses_completed_event(
                output=[
                    {
                        "type": "reasoning",
                        "content": [
                            {
                                "type": "reasoning_text",
                                "text": "<tool_call>example</tool_call>",
                            }
                        ],
                        "summary": [{"type": "summary_text", "text": "summary"}],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "answer"},
                            {"type": "output_text", "text": " recovered"},
                        ],
                    },
                ]
            )
        ),
    ]
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    assert (
        run(client.chat_stream([]))
        == "<think><tool_call>example</tool_call>summary</think>answer recovered"
    )


def test_responses_stream_completed_snapshot_only(monkeypatch):
    output = [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "summary"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "safe answer"}],
        },
    ]
    install(monkeypatch, FakeResponse(lines=sse(event(responses_completed_event(output=output)))))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    assert run(client.chat([])) == "<think>summary</think>safe answer"


@pytest.mark.parametrize(
    "terminal",
    [
        {"type": "response.failed", "response": {"status": "failed", "error": {"code": "server_error", "message": "failed generation"}}},
        {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}},
        {"type": "response.cancelled", "response": {"status": "cancelled"}},
        {"type": "error", "code": "server_error", "message": "failed generation"},
        {"type": "response.completed", "response": {"status": "incomplete"}},
    ],
)
def test_responses_stream_rejects_unsuccessful_termination(monkeypatch, terminal):
    install(
        monkeypatch,
        FakeResponse(
            lines=sse(
                event({"type": "response.output_text.delta", "delta": "partial"}),
                event(terminal),
            )
        ),
    )
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    with pytest.raises(llm.LLMError):
        run(client.chat_stream([]))


def test_responses_stream_bare_done_is_an_error(monkeypatch):
    install(monkeypatch, FakeResponse(lines=sse("[DONE]")))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat_stream([]))
    assert "stream ended without response.completed" in str(excinfo.value)


def test_responses_chat_rejects_unsuccessful_response(monkeypatch):
    bodies = [
        {"status": "failed", "error": {"code": "server_error", "message": "failed"}},
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "partial"}],
                }
            ],
        },
        {"status": "cancelled"},
        {"error": {"message": "rejected"}},
    ]
    for body in bodies:
        install(monkeypatch, FakeResponse(text=json.dumps(body)))
        client = llm.OpenAIClient(make_cfg("responses"))
        with pytest.raises(llm.LLMError):
            run(client.chat([]))


def test_responses_plain_continuation_uses_previous_id_and_delta_input(monkeypatch):
    transport = install(
        monkeypatch,
        FakeResponse(lines=sse(event(responses_completed_event()))),
        FakeResponse(lines=sse(event(responses_completed_event()))),
    )
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    history = [
        llm.Message(role=llm.Role.SYSTEM, content="instructions"),
        llm.Message(role=llm.Role.USER, content="first"),
    ]
    run(client.chat_stream(history))
    first_body = transport.calls[0]["json"]
    assert "previous_response_id" not in first_body
    assert len(first_body["input"]) == 2

    run(
        client.chat_stream(
            history + [llm.Message(role=llm.Role.ASSISTANT, content="prior")],
            previous_response_id="resp-1",
            continuation=[llm.Message(role=llm.Role.USER, content="second")],
        )
    )
    second_body = transport.calls[1]["json"]
    assert second_body["previous_response_id"] == "resp-1"
    assert second_body["input"] == [{"role": "user", "content": "second"}]


# ---------------------------------------------------------------------------
# Native Responses tool flow
# ---------------------------------------------------------------------------


def test_native_responses_snapshot_dedupes_calls_and_keeps_reasoning_inert(monkeypatch):
    payloads = [
        event(
            {
                "type": "response.reasoning_text.delta",
                "output_index": 1,
                "content_index": 0,
                "delta": "<tool_call>dangerous example</tool_call>",
            }
        ),
        event(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_real",
                    "call_id": "call_real",
                    "name": "read_file",
                    "arguments": "",
                },
            }
        ),
        event(
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc_real",
                "delta": '{"path":',
            }
        ),
        event(
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc_real",
                "delta": '"a.go"}',
            }
        ),
        event(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_real",
                    "call_id": "call_real",
                    "name": "read_file",
                    "arguments": '{"path":"a.go"}',
                },
            }
        ),
        event(
            responses_completed_event(
                output=[
                    {
                        "type": "function_call",
                        "id": "fc_real",
                        "call_id": "call_real",
                        "name": "read_file",
                        "arguments": '{"path":"a.go"}',
                    },
                    {
                        "type": "reasoning",
                        "content": [
                            {
                                "type": "reasoning_text",
                                "text": "<tool_call>dangerous example</tool_call>",
                            }
                        ],
                    },
                ]
            )
        ),
    ]
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    thinking = []
    result = run(
        client.chat_tools(
            [], [], emit=lambda delta: thinking.append(delta.thinking) if delta.thinking else None
        )
    )
    assert result.content == ""
    assert result.thinking == "<tool_call>dangerous example</tool_call>"
    assert "".join(thinking) == "<tool_call>dangerous example</tool_call>"
    assert len(result.calls) == 1
    assert result.calls[0].call_id == "call_real"
    assert result.calls[0].arguments == '{"path":"a.go"}'


@pytest.mark.parametrize(
    "payloads, kind",
    [
        (
            [
                event(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_real",
                            "call_id": "call_real",
                            "name": "read_file",
                            "arguments": "",
                        },
                    }
                ),
                event(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "item_id": "fc_other",
                        "delta": "{}",
                    }
                ),
                event(responses_completed_event()),
            ],
            "argument delta item_id mismatch",
        ),
        (
            [
                event(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "item_id": "fc_real",
                        "delta": '{"path":"good"}',
                    }
                ),
                event(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_real",
                            "call_id": "call_real",
                            "name": "read_file",
                            "arguments": '{"path":"different"}',
                        },
                    }
                ),
                event(responses_completed_event()),
            ],
            "conflicting function arguments",
        ),
        (
            [
                event(
                    responses_completed_event(
                        output=[
                            {
                                "type": "function_call",
                                "id": "fc_not_a_call_id",
                                "name": "read_file",
                                "arguments": "{}",
                            }
                        ]
                    )
                )
            ],
            "missing call_id",
        ),
        (
            [
                event(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_real",
                            "call_id": "call_real",
                            "name": "read_file",
                            "arguments": "",
                        },
                    }
                ),
                event(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_real",
                            "call_id": "call_real",
                            "name": "read_file",
                            "arguments": "[]",
                        },
                    }
                ),
                event(responses_completed_event()),
            ],
            "arguments must be a complete JSON object",
        ),
        (
            [
                event(
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 0,
                        "arguments": "{}",
                    }
                ),
                event(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "delta": "{}",
                    }
                ),
                event(responses_completed_event()),
            ],
            "arguments delta after final snapshot",
        ),
    ],
)
def test_native_responses_rejects_malformed_protocol(monkeypatch, payloads, kind):
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    with pytest.raises(llm.ToolProtocolError) as excinfo:
        run(client.chat_tools([], []))
    assert kind in str(excinfo.value) or kind == excinfo.value.kind


def test_native_responses_continuation_request_shape(monkeypatch):
    for stream in (False, True):
        response = {
            "id": "resp-1",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": "{}",
                    "status": "completed",
                }
            ],
        }

        def handler(response=response, stream=stream):
            if stream:
                return FakeResponse(
                    lines=sse(event({"type": "response.completed", "response": response}))
                )
            return FakeResponse(text=json.dumps(response))

        transport = install(monkeypatch, handler, handler)
        client = llm.OpenAIClient(make_cfg("responses", stream=stream))
        tools = [llm.ToolDefinition(name="read_file", parameters={"type": "object"})]
        history = [
            llm.Message(role=llm.Role.SYSTEM, content="instructions"),
            llm.Message(role=llm.Role.USER, content="first"),
        ]
        first = run(client.chat_tools(history, tools))
        assert first.response_id == "resp-1"
        first_body = transport.calls[0]["json"]
        assert first_body["tool_choice"] == "required"
        assert first_body["parallel_tool_calls"] is False
        assert "previous_response_id" not in first_body
        assert len(first_body["input"]) == 2
        assert first_body["tools"] == [
            {
                "type": "function",
                "name": "read_file",
                "description": "",
                "parameters": {"type": "object"},
                "strict": False,
            }
        ]

        second = run(
            client.chat_tools(
                history + [llm.Message(role=llm.Role.ASSISTANT, content="prior")],
                tools,
                previous_response_id=first.response_id,
                continuation=[llm.Message(role=llm.Role.USER, content="second")],
            )
        )
        assert second.response_id == "resp-1"
        second_body = transport.calls[1]["json"]
        assert second_body["previous_response_id"] == "resp-1"
        assert len(second_body["input"]) == 1


def test_native_responses_nonstream_and_progress(monkeypatch):
    body = {
        "status": "completed",
        "usage": {
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        "output": [
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "thought"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": "{}",
            },
        ],
    }
    install(monkeypatch, FakeResponse(text=json.dumps(body)))
    client = llm.OpenAIClient(make_cfg("responses"))
    progress = []
    result = run(
        client.chat_tools(
            [],
            [],
            emit=lambda delta: progress.append(delta.progress) if delta.progress else None,
        )
    )
    assert result.content == "answer"
    assert result.thinking == "thought"
    assert len(result.calls) == 1
    assert progress[0].output_tokens == 0
    assert progress[0].estimated is True
    assert progress[0].received_at == ""
    final = progress[-1]
    assert final.estimated is False
    assert (final.output_tokens, final.reasoning_tokens, final.tool_tokens) == (7, 2, 0)
    assert final.received_at


def test_native_responses_progress_snapshots_do_not_double_count(monkeypatch):
    payloads = [
        event({"type": "response.output_text.delta", "output_index": 0, "delta": "ab"}),
        event({"type": "response.output_text.done", "output_index": 0, "text": "abcd"}),
        event(
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "12345678"}],
                },
            }
        ),
        event(
            {
                "type": "response.output_item.done",
                "output_index": 2,
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": "{}",
                },
            }
        ),
        event(
            responses_completed_event(
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "abcd"}],
                    },
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "12345678"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": "{}",
                    },
                ]
            )
        ),
    ]
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    final = {}

    def emit(delta):
        if delta.progress is not None:
            final["progress"] = delta.progress

    result = run(client.chat_tools([], [], emit=emit))
    assert result.content == "abcd"
    assert result.thinking == "12345678"
    assert len(result.calls) == 1
    progress = final["progress"]
    assert progress.estimated is True
    assert (progress.output_tokens, progress.reasoning_tokens, progress.tool_tokens) == (4, 2, 1)


# ---------------------------------------------------------------------------
# Native Chat Completions tool flow
# ---------------------------------------------------------------------------


def test_native_chat_assembles_streamed_tool_calls(monkeypatch):
    payloads = [
        event({"choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]}),
        event({"choices": [{"index": 0, "delta": {"content": "answer"}}]}),
        event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path":'},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"a.go"}'},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        event(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                ]
            }
        ),
        event({"choices": [], "usage": {"completion_tokens": 3}}),
        "[DONE]",
    ]
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    result = run(client.chat_tools([], []))
    assert result.content == "answer"
    assert result.thinking == "think"
    assert len(result.calls) == 1
    assert result.calls[0].call_id == "call_1"
    assert result.calls[0].arguments == '{"path":"a.go"}'


@pytest.mark.parametrize(
    "payloads, expected",
    [
        (
            [
                event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "real",
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": "{}",
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "changed",
                                            "function": {"arguments": ""},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                event(
                    {
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                        ]
                    }
                ),
                "[DONE]",
            ],
            "conflicting chat call ID",
        ),
        (
            [
                event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": "{}",
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                event(
                    {
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                        ]
                    }
                ),
                "[DONE]",
            ],
            "missing call_id",
        ),
    ],
)
def test_native_chat_rejects_ambiguous_calls(monkeypatch, payloads, expected):
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    with pytest.raises(llm.ToolProtocolError) as excinfo:
        run(client.chat_tools([], []))
    assert expected in str(excinfo.value) or expected == excinfo.value.kind


def test_native_chat_exposes_multiple_calls_in_index_order(monkeypatch):
    payloads = [
        event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_b",
                                    "type": "function",
                                    "function": {"name": "search", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        event(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                ]
            }
        ),
        "[DONE]",
    ]
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    result = run(client.chat_tools([], []))
    assert [call.call_id for call in result.calls] == ["call_a", "call_b"]


def test_native_responses_rejects_duplicate_call_ids(monkeypatch):
    call = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": "{}",
    }
    install(
        monkeypatch,
        FakeResponse(lines=sse(event(responses_completed_event(output=[call, call])))),
    )
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    with pytest.raises(llm.ToolProtocolError) as excinfo:
        run(client.chat_tools([], []))
    assert "duplicate call_id" in str(excinfo.value)


def test_native_chat_finish_length_is_incomplete(monkeypatch):
    payloads = [
        event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "partial"},
                        "finish_reason": "length",
                    }
                ]
            }
        ),
        "[DONE]",
    ]
    install(monkeypatch, FakeResponse(lines=sse(*payloads)))
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    with pytest.raises(llm.IncompleteGeneration):
        run(client.chat_tools([], []))


def test_native_chat_eof_before_done_is_an_error(monkeypatch):
    install(
        monkeypatch,
        FakeResponse(
            lines=sse(
                event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "real",
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": "{}",
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                )
            )
        ),
    )
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat_tools([], []))
    assert "missing [DONE]" in str(excinfo.value)


def test_native_chat_nonstream_requires_successful_finish(monkeypatch):
    body = {
        "choices": [
            {
                "message": {
                    "content": "answer",
                    "reasoning_content": "thought",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"completion_tokens": 7},
    }
    install(monkeypatch, FakeResponse(text=json.dumps(body)))
    client = llm.OpenAIClient(make_cfg("chat"))
    result = run(client.chat_tools([], []))
    assert result.content == "answer"
    assert result.thinking == "thought"
    assert len(result.calls) == 1

    install(
        monkeypatch,
        FakeResponse(
            text=json.dumps(
                {
                    "choices": [
                        {"message": {"content": "partial"}, "finish_reason": "length"}
                    ]
                }
            )
        ),
    )
    with pytest.raises(llm.IncompleteGeneration):
        run(client.chat_tools([], []))


def test_native_chat_tool_history_is_converted(monkeypatch):
    client = llm.OpenAIClient(make_cfg("chat"))
    history = [
        llm.Message(
            role=llm.Role.ASSISTANT,
            content="prior",
            type="function_call",
            call_id="c1",
            name="read_file",
            arguments="{}",
        ),
        llm.Message(type="function_call", call_id="c2", name="search", arguments="{}"),
        llm.Message(type="function_call_output", call_id="c1", content="out1"),
        llm.Message(type="function_call_output", call_id="c2", content="out2"),
    ]
    transport = install(
        monkeypatch,
        FakeResponse(
            text=json.dumps(
                {
                    "choices": [
                        {"message": {"content": "done"}, "finish_reason": "stop"}
                    ]
                }
            )
        ),
    )
    run(client.chat_tools(history, []))
    messages = transport.calls[0]["json"]["messages"]
    assert messages[0]["role"] == "assistant"
    assert len(messages[0]["tool_calls"]) == 2
    assert messages[1] == {"role": "tool", "tool_call_id": "c1", "content": "out1"}
    assert messages[2] == {"role": "tool", "tool_call_id": "c2", "content": "out2"}


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


def _usage_reply(api: str, stream: bool, provider: bool):
    if api == "responses":
        output = [
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "abcdefghijklmnop"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer12"}],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": "{}",
            },
        ]
        usage = {"total_tokens": 7} if provider else None
        if stream:
            payloads = [
                event(
                    {
                        "type": "response.reasoning_text.delta",
                        "output_index": 0,
                        "delta": "abc",
                    }
                ),
                event(
                    {
                        "type": "response.reasoning_text.delta",
                        "output_index": 0,
                        "delta": "defghijklmnop",
                    }
                ),
                event(
                    {
                        "type": "response.output_text.delta",
                        "output_index": 1,
                        "delta": "answer12",
                    }
                ),
                event(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 2,
                        "delta": "{",
                    }
                ),
                event(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 2,
                        "delta": "}",
                    }
                ),
                event(responses_completed_event(output=output, usage=usage)),
            ]
            return FakeResponse(lines=sse(*payloads))
        body = {"status": "completed", "output": output}
        if usage is not None:
            body["usage"] = usage
        return FakeResponse(text=json.dumps(body))

    message = {
        "content": "answer12",
        "reasoning_content": "abcdefghijklmnop",
        "tool_calls": [
            {
                "index": 0,
                "type": "function",
                "id": "call_1",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    usage = {"prompt_tokens": 3, "completion_tokens": 4} if provider else None
    if stream:
        payloads = [event({"choices": [{"index": 0, "delta": message, "finish_reason": "tool_calls"}]})]
        if provider:
            payloads.append(event({"choices": [], "usage": usage}))
        payloads.append("[DONE]")
        return FakeResponse(lines=sse(*payloads))
    body = {"choices": [{"message": message, "finish_reason": "tool_calls"}]}
    if usage is not None:
        body["usage"] = usage
    return FakeResponse(text=json.dumps(body))


@pytest.mark.parametrize("api", ["responses", "chat"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("provider", [False, True])
def test_usage_provider_correction_and_fallback(monkeypatch, api, stream, native, provider):
    install(monkeypatch, _usage_reply(api, stream, provider))
    client = llm.OpenAIClient(make_cfg(api, stream=stream))
    updates: list[llm.UsageUpdate] = []
    messages = [
        llm.Message(role=llm.Role.USER, content="input" * 200),
    ]
    if native:
        run(client.chat_tools(messages, [], observer=updates.append))
    else:
        run(client.chat(messages, observer=updates.append))
    assert len(updates) >= 2
    initial, final = updates[0], updates[-1]
    assert initial.request_id != 0
    assert initial.estimated and not initial.final
    assert final.final
    for update in updates[:-1]:
        assert update.request_id == final.request_id
        assert not update.final
    if provider:
        assert final.estimated is False
        assert final.total_tokens == 7
        assert final.total_tokens < initial.total_tokens
    else:
        assert final.estimated is True
        assert final.total_tokens - initial.total_tokens == 9


def test_usage_observer_gets_final_update_on_api_error(monkeypatch):
    install(monkeypatch, FakeResponse(500, text="boom"))
    client = llm.OpenAIClient(make_cfg("chat"))
    updates: list[llm.UsageUpdate] = []
    with pytest.raises(llm.APIError):
        run(client.chat([], observer=updates.append))
    assert len(updates) >= 2
    assert updates[0].final is False
    assert updates[-1].final is True
    assert updates[-1].estimated is True
    # The unreadable error body is retained as a conservative byte estimate.
    assert updates[-1].total_tokens > updates[0].total_tokens


def test_usage_retains_partial_output_on_cancellation(monkeypatch):
    response = FakeResponse(
        lines=sse(
            event(
                {
                    "type": "response.reasoning_text.delta",
                    "output_index": 0,
                    "delta": "abcdefghijklmnop",
                }
            )
        ),
        endless=True,
    )
    install(monkeypatch, response)
    client = llm.OpenAIClient(make_cfg("responses", stream=True))
    updates: list[llm.UsageUpdate] = []

    async def scenario():
        task = asyncio.create_task(client.chat_tools([], [], observer=updates.append))
        for _ in range(200):
            if len(updates) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert len(updates) >= 2
    assert updates[-1].final is True
    assert updates[-1].estimated is True
    assert updates[-1].total_tokens - updates[0].total_tokens == 4


def test_usage_requests_get_distinct_request_ids(monkeypatch):
    install(
        monkeypatch,
        responses_completed(usage={"total_tokens": 3}),
        responses_completed(usage={"total_tokens": 3}),
    )
    client = llm.OpenAIClient(make_cfg("responses"))
    request_ids = set()
    for _ in range(2):
        updates: list[llm.UsageUpdate] = []
        run(client.chat([], observer=updates.append))
        request_ids.add(updates[0].request_id)
        assert updates[-1].final and updates[-1].total_tokens == 3
    assert len(request_ids) == 2


def test_usage_initial_cancellation_prevents_network_admission(monkeypatch):
    transport = install(monkeypatch, responses_completed())
    client = llm.OpenAIClient(make_cfg("responses"))
    updates: list[llm.UsageUpdate] = []

    def observer(update):
        updates.append(update)
        asyncio.current_task().cancel()

    with pytest.raises(asyncio.CancelledError):
        run(client.chat_tools([llm.Message(content="question")], [], observer=observer))
    assert transport.calls == []
    assert len(updates) == 2
    assert updates[1].final is True
    assert updates[0].total_tokens == updates[1].total_tokens


def test_usage_concurrent_requests_are_independent(monkeypatch):
    install(
        monkeypatch,
        *[
            FakeResponse(
                text=json.dumps(
                    {
                        "status": "completed",
                        "output": [],
                        "usage": {"total_tokens": 7},
                    }
                )
            )
            for _ in range(6)
        ],
    )
    client = llm.OpenAIClient(make_cfg("responses"))
    collected: dict[int, list[llm.UsageUpdate]] = {}

    def observer(update):
        collected.setdefault(update.request_id, []).append(update)

    async def scenario():
        await asyncio.gather(
            *[client.chat([], observer=observer) for _ in range(6)]
        )

    run(scenario())
    assert len(collected) == 6
    for updates in collected.values():
        finals = [update for update in updates if update.final]
        assert len(finals) == 1
        assert finals[0].total_tokens == 7
        assert finals[0].estimated is False


def test_final_usage_callback_cancellation_discards_result(monkeypatch):
    install(monkeypatch, responses_completed())
    client = llm.OpenAIClient(make_cfg("responses"))

    def observer(update):
        if update.final:
            asyncio.current_task().cancel()

    with pytest.raises(asyncio.CancelledError):
        run(client.chat_tools([], [], observer=observer))


def test_none_messages_and_tools_are_accepted(monkeypatch):
    install(
        monkeypatch,
        FakeResponse(
            text=json.dumps(
                {
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ]
                }
            )
        ),
    )
    client = llm.OpenAIClient(make_cfg("chat"))
    result = run(client.chat_tools(None, None))
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, retryable",
    [(500, True), (502, True), (401, False), (403, False), (408, True), (429, True)],
)
def test_api_error_classification(monkeypatch, status, retryable):
    install(monkeypatch, FakeResponse(status, text="upstream failure"))
    client = llm.OpenAIClient(make_cfg("chat"))
    with pytest.raises(llm.APIError) as excinfo:
        run(client.chat([]))
    error = excinfo.value
    assert error.status_code == status
    assert error.status == status
    assert error.retryable is retryable
    assert error.body == "upstream failure"
    assert f"openai status {status}" in str(error)


def test_malformed_sse_json_is_reported(monkeypatch):
    install(monkeypatch, FakeResponse(lines=["data: {"]))
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    with pytest.raises(llm.LLMError):
        run(client.chat_stream([]))


def test_chat_stream_requires_done(monkeypatch):
    install(
        monkeypatch,
        FakeResponse(lines=sse(event({"choices": [{"index": 0, "delta": {"content": "partial"}}]}))),
    )
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat_stream([]))
    assert "stream ended without [DONE]" in str(excinfo.value)


def test_responses_nonstream_body_limit(monkeypatch):
    install(monkeypatch, FakeResponse(text="x" * (llm.RESPONSES_MAX_BYTES + 1)))
    client = llm.OpenAIClient(make_cfg("responses"))
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat([]))
    assert "response body exceeds" in str(excinfo.value)


def test_chat_nonstream_rejects_empty_choices(monkeypatch):
    install(monkeypatch, FakeResponse(text=json.dumps({"choices": []})))
    client = llm.OpenAIClient(make_cfg("chat"))
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat([]))
    assert "no choices" in str(excinfo.value)


def test_missing_api_key_prevents_network_request(monkeypatch):
    transport = install(monkeypatch, responses_completed())
    client = llm.OpenAIClient(make_cfg("responses", api_key=""))
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat([]))
    assert "missing API key" in str(excinfo.value)
    with pytest.raises(llm.LLMError) as excinfo:
        run(client.chat_tools([], []))
    assert str(excinfo.value) == "missing API key"
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Tool history validation
# ---------------------------------------------------------------------------


def test_validate_tool_history_accepts_paired_calls():
    messages = [
        llm.Message(role=llm.Role.SYSTEM, content="system"),
        llm.Message(role=llm.Role.USER, content="question"),
        llm.Message(type="function_call", call_id="c1", name="read_file", arguments="{}"),
        llm.Message(type="function_call", call_id="c2", name="search", arguments="{}"),
        llm.Message(type="function_call_output", call_id="c1", content="out1"),
        llm.Message(type="function_call_output", call_id="c2", content="out2"),
    ]
    llm.validate_tool_history(messages)


@pytest.mark.parametrize(
    "messages, expected",
    [
        (
            [llm.Message(type="function_call_output", call_id="c1", content="x")],
            "orphan output",
        ),
        (
            [
                llm.Message(type="function_call", call_id="c1", name="f", arguments="{}"),
                llm.Message(type="function_call_output", call_id="c1", content="x"),
                llm.Message(type="function_call", call_id="c1", name="f", arguments="{}"),
            ],
            "duplicates call_id",
        ),
        (
            [llm.Message(type="function_call", name="f", arguments="{}")],
            "invalid native history call",
        ),
        (
            [
                llm.Message(type="function_call", call_id="c1", name="f", arguments="{}"),
                llm.Message(type="function_call", call_id="c2", name="f", arguments="{}"),
                llm.Message(type="function_call_output", call_id="c1", content="x"),
            ],
            "unresolved function calls",
        ),
        (
            [
                llm.Message(type="function_call", call_id="c1", name="f", arguments="{}"),
                llm.Message(role=llm.Role.USER, content="interrupt"),
                llm.Message(type="function_call_output", call_id="c1", content="x"),
            ],
            "interrupts an unresolved function call",
        ),
        (
            [llm.Message(type="bogus")],
            "unsupported native history item type",
        ),
    ],
)
def test_validate_tool_history_rejects_broken_pairing(messages, expected):
    with pytest.raises(llm.ToolProtocolError) as excinfo:
        llm.validate_tool_history(messages)
    assert expected in str(excinfo.value)


def test_chat_tools_validates_history_before_network(monkeypatch):
    transport = install(monkeypatch, responses_completed())
    client = llm.OpenAIClient(make_cfg("responses"))
    broken = [llm.Message(type="function_call_output", call_id="missing", content="x")]
    with pytest.raises(llm.ToolProtocolError):
        run(client.chat_tools(broken, []))
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Request options
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api", ["responses", "chat"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_thinking_and_reasoning_options(monkeypatch, api, stream, native):
    if api == "responses":
        if stream:
            response = FakeResponse(
                lines=sse(event(responses_completed_event(output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}])))
            )
        else:
            response = responses_completed(
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            )
    else:
        if stream:
            response = FakeResponse(lines=chat_stream_done("ok"))
        else:
            response = chat_completed("ok")
    transport = install(monkeypatch, response)
    client = llm.OpenAIClient(
        make_cfg(
            api,
            stream=stream,
            thinking=ThinkingConfig(type="enabled"),
            reasoning_effort="max",
        )
    )
    if native:
        run(client.chat_tools([], []))
    else:
        run(client.chat([]))
    body = transport.calls[0]["json"]
    assert body["thinking"] == {"type": "enabled"}
    if api == "responses":
        assert body["reasoning"] == {"effort": "max"}
        assert "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == "max"
        assert "reasoning" not in body


def test_chat_models_api_interface_routes(monkeypatch):
    transport = install(monkeypatch, chat_completed("ok"))
    client = llm.OpenAIClient(make_cfg("chat"))
    assert run(client.chat([llm.Message(content="question")])) == "ok"
    assert transport.calls[0]["url"] == "https://api.test/v1/chat/completions"

    transport = install(monkeypatch, responses_completed())
    client = llm.OpenAIClient(make_cfg("responses"))
    run(client.chat([]))
    assert transport.calls[0]["url"] == "https://api.test/v1/responses"


def test_chat_stream_is_used_when_stream_enabled(monkeypatch):
    install(monkeypatch, FakeResponse(lines=chat_stream_done("streamed", "thinking")))
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    assert run(client.chat([])) == "<think>thinking</think>streamed"


def test_nonstream_chat_parses_reasoning_fields(monkeypatch):
    bodies = [
        {"choices": [{"message": {"content": "a", "reasoning_content": "t"}}]},
        {"choices": [{"message": {"content": "a", "reasoning": "t"}}]},
        {"choices": [{"message": {"content": "a", "reasoning_text": "t"}}]},
    ]
    for body in bodies:
        install(monkeypatch, FakeResponse(text=json.dumps(body)))
        client = llm.OpenAIClient(make_cfg("chat"))
        assert run(client.chat([])) == "<think>t</think>a"


async def _close_client_smoke():
    client = llm.OpenAIClient(make_cfg("responses"))
    await client.close()


def test_close_is_safe(monkeypatch):
    transport = install(monkeypatch, responses_completed())
    run(_close_client_smoke())
    assert transport.closed is True


def _responses_tool_stream(call_id: str = "call-1") -> FakeResponse:
    output = [
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": call_id,
            "name": "read_file",
            "arguments": "{}",
            "status": "completed",
        }
    ]
    return FakeResponse(lines=sse(event(responses_completed_event(output))))


def _chat_tool_stream(call_id: str = "call-1") -> FakeResponse:
    frames = sse(
        event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        event(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                ]
            }
        ),
    )
    return FakeResponse(lines=frames + ["data: [DONE]"])


def test_responses_tool_choice_fallback_is_remembered(monkeypatch):
    capability = ("https://fallback.test/v1", "fallback-model")
    llm._UNSUPPORTED_TOOL_CHOICE.discard(capability)
    llm._UNSUPPORTED_PARALLEL_CALLS.discard(capability)
    rejection = FakeResponse(
        status_code=400,
        text=json.dumps(
            {
                "error": {
                    "message": "Thinking mode does not support this tool_choice",
                    "type": "invalid_request_error",
                }
            }
        ),
    )
    transport = install(
        monkeypatch, rejection, _responses_tool_stream(), _responses_tool_stream()
    )
    cfg = make_cfg(
        "responses", stream=True, base_url=capability[0], model=capability[1]
    )
    tools = [llm.ToolDefinition(name="read_file", parameters={"type": "object"})]
    first = run(llm.OpenAIClient(cfg).chat_tools([], tools))
    assert first.calls[0].call_id == "call-1"
    assert transport.calls[0]["json"]["tool_choice"] == "required"
    assert "tool_choice" not in transport.calls[1]["json"]
    assert transport.calls[1]["json"]["parallel_tool_calls"] is False

    second = run(llm.OpenAIClient(cfg).chat_tools([], tools))
    assert second.calls[0].call_id == "call-1"
    assert "tool_choice" not in transport.calls[2]["json"]


def test_chat_parallel_tool_calls_fallback_is_remembered(monkeypatch):
    capability = ("https://chat-fallback.test/v1", "chat-model")
    llm._UNSUPPORTED_PARALLEL_CALLS.discard(capability)
    rejection = FakeResponse(
        status_code=400,
        text=json.dumps(
            {
                "error": {
                    "message": "This model does not support parallel_tool_calls",
                }
            }
        ),
    )
    transport = install(monkeypatch, rejection, _chat_tool_stream())
    cfg = make_cfg("chat", stream=True, base_url=capability[0], model=capability[1])
    tools = [llm.ToolDefinition(name="read_file", parameters={"type": "object"})]
    result = run(llm.OpenAIClient(cfg).chat_tools([], tools))
    assert result.calls[0].call_id == "call-1"
    assert transport.calls[0]["json"]["parallel_tool_calls"] is False
    assert "parallel_tool_calls" not in transport.calls[1]["json"]


def test_responses_continuation_fallback_includes_call_items(monkeypatch):
    capability = ("https://continuation.test/v1", "continuation-model")
    for registry in (
        llm._UNSUPPORTED_TOOL_CHOICE,
        llm._UNSUPPORTED_PARALLEL_CALLS,
        llm._UNSUPPORTED_RESPONSES_CONTINUATION,
        llm._CONTINUATION_NEEDS_CALLS,
    ):
        registry.discard(capability)
    rejection = FakeResponse(
        status_code=400,
        text=json.dumps(
            {
                "error": {
                    "message": (
                        "No tool call found for tool output with call_id call-1"
                    )
                }
            }
        ),
    )
    transport = install(
        monkeypatch,
        rejection,
        _responses_tool_stream(),
        _responses_tool_stream(),
    )
    cfg = make_cfg(
        "responses", stream=True, base_url=capability[0], model=capability[1]
    )
    tools = [llm.ToolDefinition(name="read_file", parameters={"type": "object"})]
    call = llm.Message(
        type="function_call",
        call_id="call-1",
        name="read_file",
        arguments="{}",
    )
    output = llm.Message(
        type="function_call_output", call_id="call-1", content="{}"
    )
    history = [
        llm.Message(role="system", content="system"),
        llm.Message(role="user", content="first"),
        call,
        output,
    ]
    result = run(
        llm.OpenAIClient(cfg).chat_tools(
            history,
            tools,
            previous_response_id="resp-1",
            continuation=[output],
        )
    )
    assert result.calls[0].call_id == "call-1"
    assert transport.calls[0]["json"]["previous_response_id"] == "resp-1"
    assert [item.get("type") for item in transport.calls[0]["json"]["input"]] == [
        "function_call_output"
    ]
    second_body = transport.calls[1]["json"]
    assert second_body["previous_response_id"] == "resp-1"
    assert [item.get("type") for item in second_body["input"]] == [
        "function_call",
        "function_call_output",
    ]
    assert capability in llm._CONTINUATION_NEEDS_CALLS
    assert capability not in llm._UNSUPPORTED_RESPONSES_CONTINUATION

    second = run(
        llm.OpenAIClient(cfg).chat_tools(
            history,
            tools,
            previous_response_id="resp-2",
            continuation=[output],
        )
    )
    assert second.calls[0].call_id == "call-1"
    third_body = transport.calls[2]["json"]
    assert third_body["previous_response_id"] == "resp-2"
    assert [item.get("type") for item in third_body["input"]] == [
        "function_call",
        "function_call_output",
    ]


def test_responses_continuation_falls_back_to_full_history(monkeypatch):
    capability = ("https://no-continuation.test/v1", "no-continuation-model")
    for registry in (
        llm._UNSUPPORTED_TOOL_CHOICE,
        llm._UNSUPPORTED_PARALLEL_CALLS,
        llm._UNSUPPORTED_RESPONSES_CONTINUATION,
        llm._CONTINUATION_NEEDS_CALLS,
    ):
        registry.discard(capability)
    rejection = FakeResponse(
        status_code=400,
        text=json.dumps(
            {
                "error": {
                    "message": (
                        "No tool call found for tool output with call_id call-1"
                    )
                }
            }
        ),
    )
    transport = install(
        monkeypatch, rejection, rejection, _responses_tool_stream()
    )
    cfg = make_cfg(
        "responses", stream=True, base_url=capability[0], model=capability[1]
    )
    tools = [llm.ToolDefinition(name="read_file", parameters={"type": "object"})]
    call = llm.Message(
        type="function_call", call_id="call-1", name="read_file", arguments="{}"
    )
    output = llm.Message(
        type="function_call_output", call_id="call-1", content="{}"
    )
    history = [
        llm.Message(role="system", content="system"),
        llm.Message(role="user", content="first"),
        call,
        output,
    ]
    result = run(
        llm.OpenAIClient(cfg).chat_tools(
            history, tools, previous_response_id="resp-1", continuation=[output]
        )
    )
    assert result.calls[0].call_id == "call-1"
    third_body = transport.calls[2]["json"]
    assert "previous_response_id" not in third_body
    assert len(third_body["input"]) == len(history)
    assert capability in llm._UNSUPPORTED_RESPONSES_CONTINUATION


def test_multi_call_fallback_keeps_continuation(monkeypatch):
    capability = ("https://multi.test/v1", "multi-model")
    for registry in (
        llm._UNSUPPORTED_TOOL_CHOICE,
        llm._UNSUPPORTED_PARALLEL_CALLS,
        llm._UNSUPPORTED_RESPONSES_CONTINUATION,
        llm._CONTINUATION_NEEDS_CALLS,
    ):
        registry.discard(capability)
    output = [
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": "{}",
            "status": "completed",
        },
        {
            "type": "function_call",
            "id": "fc-2",
            "call_id": "call-2",
            "name": "list_files",
            "arguments": "{}",
            "status": "completed",
        },
    ]
    rejection = FakeResponse(
        status_code=400,
        text=json.dumps(
            {"error": {"message": "Thinking mode does not support this tool_choice"}}
        ),
    )
    transport = install(
        monkeypatch,
        rejection,
        FakeResponse(lines=sse(event(responses_completed_event(output)))),
    )
    cfg = make_cfg(
        "responses", stream=True, base_url=capability[0], model=capability[1]
    )
    client = llm.OpenAIClient(cfg)
    tools = [llm.ToolDefinition(name="read_file", parameters={"type": "object"})]
    result = run(client.chat_tools([], tools))
    assert [call.call_id for call in result.calls] == ["call-1", "call-2"]
    assert result.multi_call_fallback is True
    assert client._continuation_supported is True
    assert capability not in llm._UNSUPPORTED_RESPONSES_CONTINUATION
    assert "tool_choice" not in transport.calls[1]["json"]
