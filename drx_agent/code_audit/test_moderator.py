"""Forum moderator: prompt, assignments, decisions and revocations."""

from __future__ import annotations

import asyncio
import json

from drx_agent.code_audit import test_agent_support as s
from drx_agent.code_audit import tools
from drx_agent.code_audit.agent import moderator
from drx_agent.code_audit.agent.team import Team


def make_team(handler=None):
    ws = s.new_workspace()
    cfg = s.make_config(ws, recon_agents=1, audit_agents=1)
    cfg.agent.moderator_enabled = True
    cfg.openai.max_context_tokens = 200000
    cfg.openai.max_output_tokens = 4000
    client = s.ScriptedClient(handler or (lambda messages, definitions, calls: s.llm.ToolResponse()))
    team = Team(cfg, s.make_prompts(), client, client, tools.Registry(ws, 1024))
    team.create_stage("audit")
    team.ensure_moderator()
    return team


def test_moderator_prompt_identity_and_modes():
    team = make_team()
    prompt = moderator.moderator_prompt(team.moderator.agent)
    assert "内部路由 ID 是 moderator" in prompt
    assert "固定公开名字是论坛管理员" in prompt
    assert "review_stage=audit" in prompt
    assert "moderator_idle" in prompt
    team.cfg.agent.infinite_mode = True
    infinite = moderator.moderator_prompt(team.moderator.agent)
    assert "无限模式" in infinite
    assert "不批准结束审计" in infinite


def test_moderator_tool_permissions():
    assert moderator.moderator_allowed_tool("moderator_decide") is True
    assert moderator.moderator_allowed_tool("forum_post") is True
    assert moderator.moderator_allowed_tool("report_finding") is False


def test_assign_records_queue_and_defers_during_recon():
    ws = s.new_workspace()
    cfg = s.make_config(ws, recon_agents=1, audit_agents=1)
    cfg.agent.moderator_enabled = True
    cfg.openai.max_context_tokens = 200000
    cfg.openai.max_output_tokens = 4000
    client = s.ScriptedClient(lambda messages, definitions, calls: s.llm.ToolResponse())
    team = Team(cfg, s.make_prompts(), client, client, tools.Registry(ws, 1024))
    team.create_stage("recon")
    team.ensure_moderator()
    worker = team.workers[0]

    async def scenario():
        error = await moderator.assign_moderator_work(team, "recon-1", "")
        assert "1–4096" in error
        error = await moderator.assign_moderator_work(team, "ghost", "x")
        assert "当前阶段真实成员" in error
        assert await moderator.assign_moderator_work(team, "recon-1", "复核入口") == ""
        assert team.pending_assignments["recon-1"] == ["复核入口"]
        posts = team.forum_messages()
        assert posts[-1].to == "recon-1"
        assert posts[-1].topic == "待审计建议"
        assert posts[-1].content.startswith("此建议留待审计阶段核实")

    asyncio.run(scenario())


def test_apply_moderator_assignments_reopens_audit_workers():
    team = make_team()
    worker = team.workers[0]
    worker.agent.completed = True
    worker.saved.completed = True
    worker.saved.snapshot.audit = s.tools.AuditState(ended=True, summary="done")
    team.pending_assignments["audit-1"] = ["继续核验"]

    reopened = moderator.apply_moderator_assignments(team, "audit")
    assert reopened is True
    assert worker.saved.completed is False
    assert worker.agent.completed is False
    assert worker.saved.snapshot.audit.ended is False
    assert moderator.apply_moderator_assignments(team, "recon") is False


def test_moderator_activation_approves_audit_stage():
    calls = {"n": 0}

    def handler(messages, definitions, count):
        if not definitions:
            return s.llm.ToolResponse(content="摘要保留")
        calls["n"] += 1
        if calls["n"] == 1:
            return s.tool_response("moderator_review_state", {}, call_id="m1")
        return s.tool_response(
            "moderator_decide",
            {"stage": "audit", "decision": "approve", "reason": "证据齐备"},
            call_id="m2",
        )

    team = make_team(handler)
    team.pending_assignments = {}
    approved = asyncio.run(
        moderator.moderator_activation(team, team.moderator, "阶段结束审查", "audit")
    )
    assert approved is True
    assert calls["n"] == 2
    posts = team.forum_messages()
    assert any(post.topic == "阶段审查意见" and post.content == "approve: 证据齐备" for post in posts)
    assert team.moderator.saved.status.status == "idle"


def test_moderator_activation_rejects_invalid_decide():
    def handler(messages, definitions, count):
        if not definitions:
            return s.llm.ToolResponse(content="摘要保留")
        return s.tool_response(
            "moderator_decide",
            {"stage": "recon", "decision": "approve", "reason": "x"},
            call_id="bad-%d" % count,
        )

    team = make_team(handler)
    approved = asyncio.run(
        moderator.moderator_activation(team, team.moderator, "阶段结束审查", "audit")
    )
    assert approved is False


def test_revoke_finding_keeps_evidence_and_aggregation():
    team = make_team()
    worker = team.workers[0]
    finding = s.tools.Finding(title="风险", path="entry1.go", evidence="证据")
    finding.key = team.finding_key(finding)
    worker.saved.snapshot.findings = [finding]
    team._snapshot = team.aggregate()
    rejected = asyncio.run(
        moderator.moderator_tool(team, team.moderator, "moderator_revoke_finding", {})
    )
    assert "source-backed reason" in rejected
    accepted = json.loads(
        asyncio.run(
            moderator.moderator_tool(
                team,
                team.moderator,
                "moderator_revoke_finding",
                {"finding_key": finding.key, "reason": "不可达", "evidence": "源码反证"},
            )
        )
    )
    assert accepted["ok"] is True
    assert team.revocations[0]["finding_key"] == finding.key
    assert finding.key not in [item.key for item in team.aggregate().findings]


def test_review_stage_returns_immediately_when_moderator_disabled():
    team = make_team()
    team.cfg.agent.moderator_enabled = False
    approved = asyncio.run(
        asyncio.wait_for(moderator.review_stage(team, "audit"), timeout=1.0)
    )
    assert approved is True


def test_review_stage_returns_immediately_without_moderator():
    team = make_team()
    team.moderator = None
    approved = asyncio.run(
        asyncio.wait_for(moderator.review_stage(team, "audit"), timeout=1.0)
    )
    assert approved is True
