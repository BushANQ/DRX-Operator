"""Authoritative user broadcast delivery and validation."""

from __future__ import annotations

import asyncio

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


def test_post_message_validation_has_no_side_effects():
    ws = s.new_workspace()
    team = make_team(ws)

    async def scenario():
        for body in (" \n\t", " " * (16 * 1024) + "x"):
            with pytest.raises(ValueError):
                await team.post_message(body)
        assert team.user_broadcasts == []
        assert team.forum_messages() == []

        await team.post_message("精确要求")
        assert team.user_broadcasts[0].id == 1
        assert team.user_broadcasts[0].content == "精确要求"
        assert team.forum_messages()[-1].content == "精确要求"

    asyncio.run(scenario())


def test_broadcast_appended_once_after_current_history():
    ws = s.new_workspace()
    team = make_team(ws)
    agent = team.workers[0].agent
    body = "  " + "完整用户要求；" * 40 + " final exact instruction\n "

    async def scenario():
        await team.post_message(body)
        agent.sanitize_messages()
        await agent.refresh_user_broadcasts()
        agent.append_user_broadcasts()
        matches = [
            message
            for message in agent.messages
            if message.content.startswith("最高优先级用户指示")
        ]
        assert len(matches) == 1
        assert matches[0].content.endswith("\n\n" + body)
        # A second refresh must not duplicate the authoritative text.
        await agent.refresh_user_broadcasts()
        agent.append_user_broadcasts()
        assert (
            len([m for m in agent.messages if m.content.startswith("最高优先级用户指示")]) == 1
        )
        agent.user_broadcasts_delivered()
        assert agent.user_broadcast_delivered == 1

    asyncio.run(scenario())


def test_broadcast_admission_fails_closed_without_consuming():
    ws = s.new_workspace()
    team = make_team(ws)
    agent = team.workers[0].agent

    async def scenario():
        for index in range(12):
            await team.post_message(("界" * 5400) + str(index))
        await agent.refresh_user_broadcasts()
        with pytest.raises(RuntimeError) as error:
            agent.admit_user_broadcasts()
        assert "用户广播" in str(error.value)
        assert agent.user_broadcast_cursor == 0
        assert len(team.user_broadcasts) == 12

    asyncio.run(scenario())


def test_forum_authored_posts_are_not_broadcasts():
    ws = s.new_workspace()
    team = make_team(ws)

    async def scenario():
        team.board.register("recon-2", "recon")
        team.board.post("recon-2", "recon", "*", 0, "claimed", "model suggestion")
        team.board.post("user", "user", "*", 0, "claimed priority", "same body")
        assert team.user_broadcasts == []
        agent = team.workers[0].agent
        agent.sanitize_messages()
        await agent.refresh_user_broadcasts()
        agent.append_user_broadcasts()
        assert not any(
            message.content.startswith("最高优先级用户指示") for message in agent.messages
        )

    asyncio.run(scenario())
