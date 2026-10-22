"""Session persistence round-trip and validation."""

from __future__ import annotations

import json
import os

import pytest

from drx_agent.code_audit import test_agent_support as s
from drx_agent.code_audit import tools
from drx_agent.code_audit.agent.team import Team


def make_team(ws):
    cfg = s.make_config(ws, recon_agents=1, audit_agents=1)
    client = s.ScriptedClient(lambda messages, definitions, calls: s.llm.ToolResponse())
    team = Team(cfg, s.make_prompts(), client, client, tools.Registry(ws, 1024))
    team.create_stage("recon")
    return team


def test_save_and_load_roundtrip():
    ws = s.new_workspace()
    team = make_team(ws)
    worker = team.workers[0]
    worker.agent.plan = {"summary": "侦察完成", "audit_map": "地图", "audit_files": ["entry1.go"]}
    worker.agent.completed = True
    worker.agent.sanitize_messages()
    worker.agent.messages.append(s.llm.Message(role="user", content="保留消息"))
    team.capture(worker, worker.agent)
    original_name = team.board.name("recon-1")
    path = os.path.join(ws, "session.json")
    team.save_session(path)

    raw = json.loads(open(path, encoding="utf-8").read())
    assert raw["version"] == 2
    assert raw["phase"] == "recon"
    assert raw["recon_agents"] == 1 and raw["audit_agents"] == 1
    assert raw["workers"][0]["plan"]["summary"] == "侦察完成"

    restored = make_team(ws)
    restored.load_session(path)
    assert restored.phase() == "recon"
    assert len(restored.workers) == 1
    saved = restored.workers[0]
    assert saved.saved.completed is True
    assert saved.saved.plan["audit_map"] == "地图"
    assert saved.agent.plan["summary"] == "侦察完成"
    assert any(message.content == "保留消息" for message in saved.agent.messages)
    assert any(message.content.startswith("会话已恢复") for message in saved.agent.messages)
    assert restored.board.name("recon-1") == original_name
    assert len(restored.forum_messages()) == len(team.forum_messages())


def test_load_rejects_unsupported_version_and_phase():
    ws = s.new_workspace()
    team = make_team(ws)
    path = os.path.join(ws, "bad.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump({"version": 99, "workspace": ws, "phase": "recon"}, fp)
    with pytest.raises(ValueError) as error:
        team.load_session(path)
    assert "不支持的会话版本" in str(error.value)

    with open(path, "w", encoding="utf-8") as fp:
        json.dump({"version": 2, "workspace": ws, "phase": "invalid"}, fp)
    with pytest.raises(ValueError) as error:
        team.load_session(path)
    assert "无效的会话阶段" in str(error.value)


def test_load_rejects_broken_native_history_and_cursor():
    ws = s.new_workspace()
    team = make_team(ws)
    worker = team.workers[0]
    worker.agent.completed = True
    worker.agent.plan = {"summary": "s", "audit_map": "m", "audit_files": ["entry1.go"]}
    worker.agent.sanitize_messages()
    team.capture(worker, worker.agent)
    path = os.path.join(ws, "broken.json")
    team.save_session(path)
    raw = json.loads(open(path, encoding="utf-8").read())
    raw["workers"][0]["messages"].append(
        {
            "type": "function_call",
            "call_id": "broken",
            "name": "read_file",
            "arguments": "not-json",
        }
    )
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(raw, fp, ensure_ascii=False)
    with pytest.raises(ValueError) as error:
        team.load_session(path)
    assert "invalid native history" in str(error.value)

    raw["workers"][0]["messages"].pop()
    raw["workers"][0]["forum_cursor"] = 999
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(raw, fp, ensure_ascii=False)
    with pytest.raises(ValueError) as error:
        team.load_session(path)
    assert "invalid worker forum cursor" in str(error.value)


def test_load_rejects_running_team():
    ws = s.new_workspace()
    team = make_team(ws)
    path = os.path.join(ws, "session.json")
    team.save_session(path)
    team.running = True
    with pytest.raises(ValueError) as error:
        team.load_session(path)
    assert "请等待当前审计完全停止" in str(error.value)


def test_load_migrates_legacy_snapshot_session():
    ws = s.new_workspace()
    team = make_team(ws)
    legacy = {
        "workspace": ws,
        "snapshot": {
            "todos": [
                {"id": 1, "title": "遗留待办", "status": "completed", "priority": "high"}
            ],
            "findings": [
                {
                    "id": 1,
                    "severity": "high",
                    "title": "遗留漏洞",
                    "path": "entry1.go",
                    "line": 3,
                    "evidence": "旧证据",
                    "impact": "旧影响",
                    "recommendation": "旧建议",
                    "cwe": "CWE-78",
                }
            ],
            "project_note": {"note": "旧版工程笔记"},
            "files": [
                {"path": "entry1.go", "status": "reviewed", "note": "已读"}
            ],
            "variables": [],
            "flows": [],
            "audit": {"ended": False},
        },
    }
    path = os.path.join(ws, "legacy.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(legacy, fp, ensure_ascii=False)

    team.load_session(path)
    assert team.phase() == "audit"
    assert len(team.workers) == 1
    worker = team.workers[0]
    assert worker.saved.status.id == "legacy"
    assert worker.saved.completed is True
    assert worker.saved.plan["summary"] == "旧版审计会话迁移"
    assert worker.saved.plan["audit_files"] == ["entry1.go"]
    assert worker.saved.plan["audit_map"] == "旧版工程笔记"
    restored_snapshot = worker.agent.tools.snapshot()
    assert [finding.title for finding in restored_snapshot.findings] == ["遗留漏洞"]
    assert [item.path for item in restored_snapshot.files] == ["entry1.go"]
    assert "遗留漏洞" in team.handoff

    migrated = os.path.join(ws, "migrated.json")
    team.save_session(migrated)
    raw = json.loads(open(migrated, encoding="utf-8").read())
    assert raw["version"] == 2
    assert raw["phase"] == "audit"
    assert raw["workers"][0]["status"]["id"] == "legacy"


def test_load_repairs_unpaired_native_call():
    ws = s.new_workspace()
    team = make_team(ws)
    worker = team.workers[0]
    worker.agent.sanitize_messages()
    team.capture(worker, worker.agent)
    path = os.path.join(ws, "paused.json")
    team.save_session(path)
    raw = json.loads(open(path, encoding="utf-8").read())
    raw["workers"][0]["messages"].append(
        {
            "role": "user",
            "type": "function_call",
            "call_id": "paused-call",
            "name": "read_file",
            "arguments": "{}",
        }
    )
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(raw, fp, ensure_ascii=False)

    restored = make_team(ws)
    restored.load_session(path)
    messages = restored.workers[0].saved.messages
    assert messages[-1].type == "function_call_output"
    assert messages[-1].call_id == "paused-call"
    assert '"ok": false' in messages[-1].content
    assert "上次中断未产生工具执行结果" in messages[-1].content
