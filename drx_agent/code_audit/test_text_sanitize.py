"""Lone-surrogate hygiene across model streams, events and session files."""

from __future__ import annotations

import json
import os

import pytest

from drx_agent.code_audit import llm
from drx_agent.code_audit import test_agent_support as support
from drx_agent.code_audit import tools
from drx_agent.code_audit.agent import Team
from drx_agent.code_audit.agent.events import bounded_excerpt, short_text
from drx_agent.code_audit.test_llm import FakeResponse, install, make_cfg
from drx_agent.code_audit.text import sanitize_data, sanitize_text, utf8_len

HIGH = "\ud83d"
LOW = "\ude00"
REPLACEMENT = "\ufffd"


def test_sanitize_text_replaces_lone_surrogates():
    assert sanitize_text(f"a{HIGH}b{LOW}c") == f"a{REPLACEMENT}b{REPLACEMENT}c"
    assert sanitize_text("正常文本") == "正常文本"
    assert utf8_len(f"a{HIGH}") == len("a\ufffd".encode("utf-8"))


def test_sanitize_data_walks_nested_values():
    data = {"a": [f"{HIGH}", {"b": f"x{LOW}y"}], "n": 1}
    cleaned = sanitize_data(data)
    assert cleaned == {
        "a": [REPLACEMENT, {"b": f"x{REPLACEMENT}y"}],
        "n": 1,
    }


def test_event_truncation_helpers_survive_surrogates():
    text = "前缀" + HIGH * 2000
    assert REPLACEMENT in short_text(text, 64)
    assert REPLACEMENT in bounded_excerpt(text, 64)
    assert len(short_text(text, 64).encode("utf-8")) <= 64 + len("…".encode("utf-8"))


def _surrogate_chat_frame(content: str) -> str:
    payload = {
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
        ]
    }
    # The wire form escapes lone surrogates; the client decodes them back.
    return "data: " + json.dumps(payload, ensure_ascii=True)


def test_chat_stream_sanitizes_split_surrogate(monkeypatch):
    lines = [
        _surrogate_chat_frame(HIGH),
        _surrogate_chat_frame(LOW),
        "data: [DONE]",
    ]
    install(monkeypatch, FakeResponse(lines=lines))
    client = llm.OpenAIClient(make_cfg("chat", stream=True))
    emitted: list[str] = []
    answer = asyncio_run(
        client.chat_stream([], emit=lambda delta: emitted.append(delta.content))
    )
    assert answer == REPLACEMENT + REPLACEMENT
    assert emitted == [REPLACEMENT, REPLACEMENT]
    assert REPLACEMENT in answer


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_session_save_and_load_sanitize_surrogates(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "a.py"), "w", encoding="utf-8") as handle:
        handle.write("print(1)\n")
    cfg = support.make_config(ws, recon_agents=1, audit_agents=1)
    client = support.ScriptedClient(
        lambda messages, definitions, calls: support.llm.ToolResponse()
    )
    team = Team(cfg, support.make_prompts(), client, client, tools.Registry(ws, 1024))
    team.create_stage("recon")
    worker = team.workers[0]
    worker.agent.add_message(
        support.llm.Message(role="assistant", content=f"emoji {HIGH} {LOW}")
    )
    worker.agent.sanitize_messages()
    team.capture(worker, worker.agent)
    path = str(tmp_path / "session.json")
    team.save_session(path)
    raw = open(path, encoding="utf-8").read()
    assert REPLACEMENT in raw
    json.loads(raw)

    restored = Team(
        cfg,
        support.make_prompts(),
        client,
        client,
        tools.Registry(ws, 1024),
    )
    restored.load_session(path)
    contents = [
        message.content
        for message in restored.workers[0].saved.messages
        if getattr(message, "content", "")
    ]
    assert all(not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text) for text in contents)
