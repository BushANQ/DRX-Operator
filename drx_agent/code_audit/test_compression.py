"""Context budget estimation and compression behavior."""

from __future__ import annotations

import asyncio

import pytest

from drx_agent.code_audit import test_agent_support as s
from drx_agent.code_audit import tools
from drx_agent.code_audit.agent import context as ctx
from drx_agent.code_audit.agent.core import new_worker


def make_agent(workspace=None, compress_client=None, **overrides):
    ws = workspace or s.new_workspace()
    overrides.setdefault("compress_at_ratio", 0.5)
    cfg = s.make_config(ws, **overrides)
    registry = tools.Registry(ws, 1024)
    return new_worker(
        cfg, s.make_prompts(), s.ScriptedClient(lambda m, d, n: None), compress_client, registry, "budget-1", "audit"
    )


def test_estimate_text_tokens_matches_reference():
    assert ctx.estimate_text_tokens("") == 0
    assert ctx.estimate_text_tokens("abcd") == 1
    assert ctx.estimate_text_tokens("a" * 9) == 3
    assert ctx.estimate_text_tokens("界") == 1
    assert ctx.estimate_text_tokens("é") == 2
    assert ctx.estimate_text_tokens("ab界") == 2


def test_estimate_tokens_adds_message_overhead():
    messages = [
        s.llm.Message(role="system", content="abcd"),
        s.llm.Message(role="user", content="界"),
    ]
    assert ctx.estimate_tokens(messages) == 1 + 4 + 1 + 4


def test_bounded_text_respects_serialized_budget():
    agent = make_agent()
    agent.cfg.agent.max_tool_result_chars = 1024
    text = "界" * 4000
    wrapped = ctx.bounded_text(agent, "review_state", text)
    assert len(wrapped.encode("utf-8")) <= 1024
    assert '"truncated":true' in wrapped
    small = ctx.bounded_text(agent, "x", "abc")
    assert '"ok":true' in small and '"truncated"' not in small


def test_compression_threshold_trigger_boundary():
    agent = make_agent(compress_at_ratio=0.1)
    limit = agent.cfg.compression_threshold()
    calls = []

    async def fake_compress(emit, reason):
        calls.append(reason)

    agent.compress_context = fake_compress

    from drx_agent.code_audit.agent.native import tool_definition_tokens

    async def scenario():
        agent.messages = [s.llm.Message(role="system", content="s"), s.llm.Message(role="user", content="")]
        tool_tokens = tool_definition_tokens(agent)
        padding = limit - 1 - ctx.estimate_tokens(agent.messages) - tool_tokens
        agent.messages[1].content = "界" * max(0, padding)
        assert ctx.estimate_tokens(agent.messages) + tool_tokens == limit - 1
        await agent.compress_if_needed(lambda event: None)
        assert calls == []
        agent.messages[1].content += "界"
        await agent.compress_if_needed(lambda event: None)
        assert calls == ["estimated context budget reached"]

    asyncio.run(scenario())


def test_compression_replaces_history_with_state_and_resume():
    summaries = ["保留入口证据", "保留入口证据"]
    compressor = s.ScriptedClient(lambda m, d, n: s.llm.ToolResponse(content=summaries.pop(0)))
    agent = make_agent(compress_client=compressor, compress_at_ratio=0.5)
    agent.cfg.compress_openai.max_context_tokens = 16000
    agent.cfg.compress_openai.max_output_tokens = 1000
    agent.messages = [
        s.llm.Message(role="system", content=agent.system_prompt()),
        s.llm.Message(role="user", content="审计目标"),
        s.llm.Message(role="assistant", content="中间证据" * 100),
    ]
    before = list(agent.messages)
    asyncio.run(agent.compress_context(lambda event: None, "test"))
    assert agent.messages is not before
    assert "保留入口证据" in agent.messages[1].content
    assert "继续剩余工作" in agent.messages[3].content
    assert compressor.calls == 1


def test_failed_compaction_keeps_exact_history():
    compressor = s.ScriptedClient(lambda m, d, n: s.llm.ToolResponse(content=""))
    agent = make_agent(compress_client=compressor, compress_at_ratio=0.5)
    agent.messages = [
        s.llm.Message(role="system", content=agent.system_prompt()),
        s.llm.Message(role="user", content="审计目标"),
    ]
    agent.forum_pending = "Forum notification:\n pending  "
    before = list(agent.messages)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(agent.compress_context(lambda event: None, "fail"))
    assert "空摘要" in str(error.value)
    assert agent.messages == before
    assert agent.forum_pending == "Forum notification:\n pending  "


def test_compression_shrinks_oldest_units_on_context_error():
    calls = {"n": 0}

    def handler(messages, definitions, n):
        calls["n"] += 1
        body = messages[1].content
        if "CALL3" in body:
            raise RuntimeError("maximum context length exceeded")
        return s.llm.ToolResponse(content="摘要保留最新证据")

    compressor = s.ScriptedClient(handler)
    agent = make_agent(compress_client=compressor, compress_at_ratio=0.5)
    for index in range(4):
        agent.messages.append(
            s.llm.Message(
                type="function_call", name="read_file", call_id=str(index), arguments="CALL%d" % index
            )
        )
        agent.messages.append(
            s.llm.Message(type="function_call_output", call_id=str(index), content="RESULT%d" % index)
        )
    asyncio.run(agent.compress_context(lambda event: None, "shrink"))
    assert calls["n"] >= 2
    assert "摘要保留最新证据" in agent.messages[1].content


def test_compression_projection_never_sends_native_items():
    seen = []

    def handler(messages, definitions, n):
        seen.extend(messages)
        return s.llm.ToolResponse(content="ok")

    compressor = s.ScriptedClient(handler)
    agent = make_agent(compress_client=compressor, compress_at_ratio=0.5)
    agent.messages = [
        s.llm.Message(role="system", content=agent.system_prompt()),
        s.llm.Message(type="function_call", name="read_file", call_id="c1", arguments="{}"),
        s.llm.Message(type="function_call_output", call_id="c1", content="long evidence"),
    ]
    asyncio.run(agent.compress_context(lambda event: None, "native"))
    assert all(not message.type for message in seen)
    assert any("c1" in message.content for message in seen)
