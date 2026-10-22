"""End-to-end team stage machine: recon barrier, audit completion and pause."""

from __future__ import annotations

import asyncio

import pytest

from drx_agent.code_audit import test_agent_support as s
from drx_agent.code_audit import tools
from drx_agent.code_audit.agent.team import Team


def build_team(ws, handler, **overrides):
    cfg = s.make_config(ws, recon_agents=1, audit_agents=1, **overrides)
    client = s.ScriptedClient(handler)
    team = Team(cfg, s.make_prompts(), client, client, tools.Registry(ws, 1024))
    return team


def test_recon_then_audit_stage_completes():
    ws = s.new_workspace()
    seen = []

    def handler(messages, definitions, calls):
        phase = team.phase()
        seen.append(phase)
        tool_names = {item.name for item in definitions}
        if phase == "recon":
            return s.tool_response(
                "audit_plan_done",
                {
                    "summary": "入口侦察完成",
                    "audit_map": "entry1.go 入口",
                    "audit_files": ["entry1.go"],
                    "execute_instructions": "复核入口",
                },
                call_id="plan-%d" % calls,
            )
        if "file_review_update" in tool_names and not any(
            getattr(message, "type", "") == "function_call"
            and getattr(message, "name", "") == "file_review_update"
            for message in messages
        ):
            return s.tool_response(
                "file_review_update",
                {"path": "entry1.go", "status": "reviewed", "note": "已复核"},
                call_id="file-%d" % calls,
            )
        if not any(
            getattr(message, "type", "") == "function_call"
            and getattr(message, "name", "") == "report_finding"
            for message in messages
        ):
            return s.tool_response(
                "report_finding",
                {
                    "title": "测试风险",
                    "path": "entry1.go",
                    "severity": "high",
                    "evidence": "fixture evidence",
                    "impact": "fixture impact",
                },
                call_id="finding-%d" % calls,
            )
        return s.tool_response(
            "end_audit", {"summary": "全部覆盖", "vote": "approve"}, call_id="end-%d" % calls
        )

    team = build_team(ws, handler)
    events = []
    asyncio.run(team.run("审计测试", events.append))
    assert team.phase() == "completed"
    snapshot = team.snapshot()
    assert snapshot.audit.ended is True
    assert len(snapshot.findings) == 1
    assert any(event.kind == "state" for event in events)
    assert any(event.kind == "worker" for event in events)
    assert "recon" in seen and "audit" in seen
    assert [worker.saved.status.status for worker in team.workers] == ["completed", "completed"]
    assert team.is_running is False
    handoff = team.build_handoff()
    assert "入口侦察完成" in handoff


def test_max_turns_stops_worker_without_completion():
    ws = s.new_workspace()

    def handler(messages, definitions, calls):
        return s.tool_response("read_file", {"path": "entry1.go", "limit": 1}, call_id="r-%d" % calls)

    team = build_team(ws, handler, max_turns=2)
    events = []
    asyncio.run(team.run("inspect", events.append))
    assert team.phase() == "recon"
    assert any(event.kind == "error" and "max_turns" in event.content for event in events)
    assert any(
        worker.saved.status.status in ("failed", "cancelled") for worker in team.workers
    )


def test_cancel_pauses_without_completing():
    ws = s.new_workspace()
    gate = asyncio.Event()

    class BlockingClient(s.ScriptedClient):
        async def chat_tools(self, messages, tool_defs, **kwargs):
            await gate.wait()
            return s.tool_response("read_file", {"path": "entry1.go"}, call_id="never")

    cfg = s.make_config(ws, recon_agents=1, audit_agents=1)
    client = BlockingClient()
    team = Team(cfg, s.make_prompts(), client, client, tools.Registry(ws, 1024))

    async def scenario():
        task = asyncio.create_task(team.run("inspect", lambda event: None))
        await asyncio.sleep(0.05)
        assert team.is_running is True
        team.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert team.is_running is False
        assert team.phase() == "recon"
        assert all(worker.saved.status.status == "cancelled" for worker in team.workers)
        gate.set()

    asyncio.run(scenario())


def test_infinite_mode_ignores_max_turns_and_hides_end_tool():
    ws = s.new_workspace()

    def handler(messages, definitions, calls):
        if calls >= 3:
            raise asyncio.CancelledError()
        assert "end_audit" not in {item.name for item in definitions}
        return s.tool_response("review_state", {"limit": 1}, call_id="v-%d" % calls)

    team = build_team(ws, handler, max_turns=1)
    cfg = team.cfg
    team.configure_budget(True, 0, 0, 0)
    events = []
    try:
        asyncio.run(team.run("infinite", events.append))
    except asyncio.CancelledError:
        pass
    assert team.phase() != "completed"
