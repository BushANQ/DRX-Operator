"""End-to-end audit pipeline against a deterministic in-process model.

The transport below replaces only the HTTP layer: the real OpenAI client,
agent loop, tool registry, forum, budget, findings and session persistence all
run.  It proves the integrated pipeline without external credentials.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from drx_agent.code_audit import llm as llm_mod
from drx_agent.code_audit.agent import Team
from drx_agent.code_audit.config import (
    AgentConfig,
    Config,
    OpenAIConfig,
    PROMPTS_DIR,
    PromptConfig,
    SKILLS_DIR,
)
from drx_agent.code_audit.prompt import load_prompts
from drx_agent.code_audit.tools import Registry


class FakeResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.text = "\n".join(lines)
        self.closed = False

    def iter_lines(self):
        for line in self._lines:
            yield line

    def close(self) -> None:
        self.closed = True


def _frame(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _tool_response(
    call_id: str, name: str, arguments: dict, text: str = ""
) -> list[str]:
    lines: list[str] = []
    if text:
        lines.append(_frame({"choices": [{"index": 0, "delta": {"content": text}}]}))
    lines.append(
        _frame(
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
                                        "name": name,
                                        "arguments": json.dumps(
                                            arguments, ensure_ascii=False
                                        ),
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        )
    )
    lines.append(
        _frame(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                },
            }
        )
    )
    lines.append("data: [DONE]")
    return lines


RECON_SCRIPT = [
    ("todo_create", {"title": "审计 app.py 的命令拼接入口", "priority": "high"}),
    (
        "file_review_update",
        {"paths": ["app.py"], "status": "reviewed", "note": "入口文件已阅读"},
    ),
    ("project_note_update", {"note": "项目入口 app.py 接收用户输入并拼接命令"}),
    (
        "audit_plan_done",
        {
            "summary": "侦察完成：入口为 app.py",
            "audit_map": "app.py 接收 input() 并调用 os.system",
            "audit_files": ["app.py"],
        },
    ),
]

AUDIT_SCRIPT = [
    ("todo_create", {"title": "审计 app.py 的命令注入链路", "priority": "high"}),
    ("read_file", {"path": "app.py"}),
    (
        "file_review_update",
        {"paths": ["app.py"], "status": "reviewed", "note": "已完整阅读入口"},
    ),
    (
        "variable_review_update",
        {
            "name": "name",
            "path": "app.py",
            "status": "suspicious",
            "note": "来自 input() 的用户输入直接进入命令拼接",
        },
    ),
    (
        "flow_review_update",
        {
            "name": "app.py 用户输入 -> os.system",
            "status": "suspicious",
            "entry": "input()",
            "files": ["app.py"],
            "evidence": "os.system('echo ' + name)",
            "next_step": "确认命令注入可利用性",
        },
    ),
    ("todo_update", {"id": 1, "status": "completed"}),
    (
        "report_finding",
        {
            "severity": "high",
            "title": "app.py 命令注入",
            "path": "app.py",
            "line": 3,
            "evidence": "os.system('echo ' + name)，name 来自 input()",
            "impact": "攻击者可执行任意系统命令",
            "recommendation": "使用 subprocess 参数列表并校验输入",
            "cwe": "CWE-78",
        },
    ),
    (
        "end_audit",
        {"summary": "已覆盖 app.py 的全部入口与数据流", "vote": "approve"},
    ),
]


class ScriptedTransport:
    """Replies with the next scripted tool call for each agent conversation."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.calls = 0

    def post(self, url, headers, json_body, stream, timeout):
        self.requests.append(
            {"url": url, "payload": json_body, "stream": stream, "timeout": timeout}
        )
        payload = json_body
        tools = {tool.get("function", {}).get("name") for tool in payload.get("tools") or []}
        audit_phase = "report_finding" in tools
        script = AUDIT_SCRIPT if audit_phase else RECON_SCRIPT
        step = sum(
            1
            for message in payload.get("messages") or []
            if message.get("role") == "tool"
        )
        if step >= len(script):
            # Repeating a harmless read keeps the loop alive if an earlier
            # step was rejected; the test timeout turns that into a failure.
            name, arguments = "review_state", {}
        else:
            name, arguments = script[step]
        self.calls += 1
        call_id = f"call_{self.calls}"
        return FakeResponse(_tool_response(call_id, name, arguments))


def _config(tmp_path) -> Config:
    return Config(
        workspace=str(tmp_path / "target"),
        openai=OpenAIConfig(
            base_url="http://fake.local/v1",
            api_key="test-key",
            model="fake-model",
            api_interface="chat",
            max_context_tokens=64000,
            max_output_tokens=8000,
            stream=True,
            thinking=None,
            top_p=1.0,
            timeout_seconds=5,
        ),
        compress_openai=OpenAIConfig(
            base_url="http://fake.local/v1",
            api_key="test-key",
            model="fake-model",
            api_interface="chat",
            max_context_tokens=64000,
            max_output_tokens=8000,
            stream=False,
        ),
        prompts=PromptConfig(
            system=f"{PROMPTS_DIR}/system.md",
            plan_system=f"{PROMPTS_DIR}/plan_system.md",
            compress=f"{PROMPTS_DIR}/compress.md",
            skills_dir=SKILLS_DIR,
            templates_dir=f"{PROMPTS_DIR}/templates",
        ),
        agent=AgentConfig(
            recon_agents=1,
            audit_agents=1,
            forum_wait_seconds=1,
            moderator_enabled=False,
            moderator_interval_seconds=60,
            infinite_mode=False,
            budget_hours=8,
            max_tool_result_chars=12000,
            compress_at_ratio=0.75,
            retry_attempts=3,
            session_dir=str(tmp_path / "sessions"),
        ),
    )


def _make_workspace(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "app.py").write_text(
        "import os\n"
        "name = input()\n"
        "os.system('echo ' + name)\n",
        encoding="utf-8",
    )
    return target


def test_full_audit_pipeline_reaches_completed(tmp_path, monkeypatch):
    _make_workspace(tmp_path)
    cfg = _config(tmp_path)
    prompts = load_prompts(cfg.prompts)
    transport = ScriptedTransport()
    monkeypatch.setattr(llm_mod, "_TRANSPORT", transport)

    client = llm_mod.OpenAIClient(cfg.openai)
    compress = llm_mod.OpenAIClient(cfg.compress_openai)
    registry = Registry(cfg.workspace, cfg.agent.max_tool_result_chars)
    team = Team(cfg, prompts, client, compress, registry)
    events: list = []

    async def scenario():
        try:
            await asyncio.wait_for(
                team.run(str(tmp_path / "target"), events.append), timeout=90
            )
        finally:
            await team.close()

    asyncio.run(scenario())

    assert team.phase() == "completed"
    snapshot = team.snapshot()
    assert snapshot.audit.ended is True
    assert [finding.title for finding in snapshot.findings] == ["app.py 命令注入"]
    assert snapshot.findings[0].key
    assert snapshot.findings[0].evidence.startswith("[")
    assert any(file.path == "app.py" and file.status == "reviewed" for file in snapshot.files)
    assert snapshot.todos and snapshot.todos[0].status == "completed"
    forum_topics = [message.topic for message in team.forum_messages()]
    assert "阶段调度" in forum_topics
    assert "阶段交接" in forum_topics
    assert team.statuses()[0].status == "completed"
    state_events = [event for event in events if event.kind == "state"]
    assert state_events
    assert any(event.forum is not None for event in events)


def test_session_roundtrip_preserves_findings(tmp_path, monkeypatch):
    _make_workspace(tmp_path)
    cfg = _config(tmp_path)
    prompts = load_prompts(cfg.prompts)
    transport = ScriptedTransport()
    monkeypatch.setattr(llm_mod, "_TRANSPORT", transport)
    client = llm_mod.OpenAIClient(cfg.openai)
    compress = llm_mod.OpenAIClient(cfg.compress_openai)
    registry = Registry(cfg.workspace, cfg.agent.max_tool_result_chars)
    team = Team(cfg, prompts, client, compress, registry)

    async def run_team():
        try:
            await asyncio.wait_for(
                team.run(str(tmp_path / "target"), lambda event: None), timeout=90
            )
        finally:
            await team.close()

    asyncio.run(run_team())
    session_path = str(tmp_path / "sessions" / "audit-session.json")
    team.save_session(session_path)
    assert os.path.isfile(session_path)

    restored = Team(
        cfg,
        load_prompts(cfg.prompts),
        llm_mod.OpenAIClient(cfg.openai),
        llm_mod.OpenAIClient(cfg.compress_openai),
        Registry(cfg.workspace, cfg.agent.max_tool_result_chars),
    )
    restored.load_session(session_path)
    assert restored.phase() == "completed"
    assert [finding.title for finding in restored.snapshot().findings] == ["app.py 命令注入"]
    asyncio.run(restored.close())


def test_missing_api_key_surfaces_model_error(tmp_path, monkeypatch):
    _make_workspace(tmp_path)
    cfg = _config(tmp_path)
    cfg.openai.api_key = ""
    prompts = load_prompts(cfg.prompts)
    client = llm_mod.OpenAIClient(cfg.openai)
    team = Team(
        cfg,
        prompts,
        client,
        llm_mod.OpenAIClient(cfg.compress_openai),
        Registry(cfg.workspace, cfg.agent.max_tool_result_chars),
    )
    events: list = []

    async def scenario():
        try:
            await asyncio.wait_for(
                team.run(str(tmp_path / "target"), events.append), timeout=30
            )
        finally:
            await team.close()

    asyncio.run(scenario())
    assert team.phase() != "completed"
    errors = [event for event in events if event.kind == "error"]
    assert errors
