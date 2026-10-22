"""Retry, disconnect and billing classification tests."""

from __future__ import annotations

import asyncio

import pytest

from drx_agent.code_audit import test_agent_support  # noqa: F401 - installs stubs
from drx_agent.code_audit.agent import retry


def test_is_context_length_error_markers():
    assert retry.is_context_length_error(RuntimeError("maximum context length exceeded"))
    assert retry.is_context_length_error(RuntimeError("context length problem"))
    assert retry.is_context_length_error(RuntimeError("input_tokens too large"))
    assert not retry.is_context_length_error(RuntimeError("openai status 401: unauthorized"))
    assert not retry.is_context_length_error(None)


def test_model_disconnect_classification():
    assert retry.is_model_disconnect(ConnectionResetError("connection reset by peer"))
    assert retry.is_model_disconnect(TimeoutError("timed out"))
    assert retry.is_model_disconnect(RuntimeError("openai status 503: unavailable"))
    assert retry.is_model_disconnect(RuntimeError("stream ended without response.completed"))
    assert retry.is_model_disconnect(RuntimeError("connection refused"))
    assert not retry.is_model_disconnect(RuntimeError("maximum context length exceeded"))
    assert not retry.is_model_disconnect(RuntimeError("openai status 401: unauthorized"))
    assert not retry.is_model_disconnect(asyncio.CancelledError())


def test_billing_classification_markers():
    assert retry.is_billing_exhausted(RuntimeError("openai status 402: payment required"))
    assert retry.is_billing_exhausted(RuntimeError("账户余额不足"))
    assert retry.is_billing_exhausted(RuntimeError("insufficient quota"))
    assert retry.is_billing_exhausted(RuntimeError("quota_exceeded"))
    assert not retry.is_billing_exhausted(RuntimeError("openai status 429: rate_limit_exceeded"))
    assert not retry.is_billing_exhausted(RuntimeError("openai status 503: service unavailable"))
    assert not retry.is_billing_exhausted(RuntimeError("maximum context length exceeded"))


def test_billing_stops_without_retry():
    calls = 0
    stops = []

    async def request():
        nonlocal calls
        calls += 1
        raise RuntimeError("openai status 402: payment required")

    runner = retry.ModelRequestRunner(lambda _event: None, stops.append)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run(request))
    assert calls == 1
    assert len(stops) == 1


def test_network_retry_increases_and_saturates(monkeypatch):
    waits = []
    attempts = 0

    async def fake_wait(delay):
        waits.append(delay)
        if len(waits) >= 20:
            raise asyncio.CancelledError()

    async def request():
        nonlocal attempts
        attempts += 1
        raise ConnectionResetError("connection reset")

    runner = retry.ModelRequestRunner(lambda _event: None, wait=fake_wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run(request))
    assert len(waits) == 20
    assert waits[0] == pytest.approx(1.0)
    assert waits[-1] == pytest.approx(retry.MODEL_RETRY_MAX_DELAY)
    for index in range(1, len(waits)):
        assert waits[index] >= waits[index - 1]
        assert 0 < waits[index] <= retry.MODEL_RETRY_MAX_DELAY


def test_non_transport_failure_is_not_retried():
    calls = 0

    async def request():
        nonlocal calls
        calls += 1
        raise RuntimeError("openai status 401: unauthorized")

    runner = retry.ModelRequestRunner(lambda _event: None)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run(request))
    assert calls == 1


def test_incomplete_generation_retries_three_times(monkeypatch):
    from drx_agent.code_audit import test_agent_support

    calls = 0

    async def request():
        nonlocal calls
        calls += 1
        raise test_agent_support.llm.IncompleteGeneration("max_output_tokens")

    runner = retry.ModelRequestRunner(lambda _event: None)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(runner.run(request))
    assert "重试3次" in str(error.value)
    assert calls == 4


def test_successful_request_returns_value():
    async def request():
        return "answer"

    runner = retry.ModelRequestRunner(lambda _event: None)
    assert asyncio.run(runner.run(request)) == "answer"
