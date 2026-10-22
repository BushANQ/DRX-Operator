"""IRC mailbox: delivery, replies, read cursor and session validation."""

from __future__ import annotations

import asyncio
import json

import pytest

from drx_agent.code_audit import test_agent_support as s
from drx_agent.code_audit import tools
from drx_agent.code_audit.agent import irc
from drx_agent.code_audit.agent.events import IRCMessage as _IRCMessage, WorkerStatus
from drx_agent.code_audit.agent.session import TeamSession, WorkerSession
from drx_agent.code_audit.agent.team import Team


def make_team():
    ws = s.new_workspace()
    cfg = s.make_config(ws, recon_agents=1, audit_agents=1)
    cfg.agent.moderator_enabled = True
    team = Team(cfg, s.make_prompts(), None, None, tools.Registry(ws, 1024))
    team.create_stage("recon")
    team.ensure_moderator()
    team.running = True
    team.stage_open = True
    return team


def test_send_deliver_reply_roundtrip():
    team = make_team()
    worker = team.workers[0]

    async def scenario():
        raw = await irc.send_irc(team, worker.agent.id, "检查入口可达性")
        payload = json.loads(raw)
        assert payload["ok"] is True
        message = payload["message"]
        assert message["state"] == "queued"
        assert message["stage"] == "recon"
        early = await irc.reply_irc(team, worker.agent.id, message["message_id"], "太早")
        assert json.loads(early)["ok"] is False

        assert await irc.deliver_worker_irc(team, worker) is True
        assert not await irc.deliver_worker_irc(team, worker)
        delivered = [m for m in team.irc_messages if m.state == "delivered"]
        assert len(delivered) == 1
        priority = [m for m in worker.agent.messages if m.content.startswith("优先 IRC message_id=")]
        assert len(priority) == 1 and "检查入口可达性" in priority[0].content

        wrong = await irc.reply_irc(team, "recon-2", message["message_id"], "越权")
        assert json.loads(wrong)["ok"] is False
        reply = await irc.reply_irc(team, worker.agent.id, message["message_id"], "已确认可达")
        assert json.loads(reply)["ok"] is True
        state = [m for m in team.irc_messages if m.message_id == message["message_id"]][0]
        assert state.state == "replied" and state.reply == "已确认可达"
        duplicate = await irc.reply_irc(team, worker.agent.id, message["message_id"], "重复")
        assert json.loads(duplicate)["ok"] is False

        read = json.loads(
            await irc.read_irc(team, {"message_id": message["message_id"], "timeout_seconds": 0})
        )
        assert read["ok"] is True and read["messages"][0]["state"] == "replied"
        assert read["next_after_id"] >= state.change_id

    asyncio.run(scenario())


def test_mailbox_limits_and_stage_gate():
    team = make_team()
    worker = team.workers[0]

    async def scenario():
        for index in range(8):
            raw = await irc.send_irc(team, worker.agent.id, "消息%d" % index)
            assert json.loads(raw)["ok"] is True
        full = json.loads(await irc.send_irc(team, worker.agent.id, "第九条"))
        assert full["ok"] is False and "mailbox full" in full["error"]

        team.stage_open = False
        blocked = json.loads(await irc.send_irc(team, worker.agent.id, "阶段外"))
        assert blocked["ok"] is False and "stage boundary" in blocked["error"]
        team.stage_open = True
        unknown = json.loads(await irc.send_irc(team, "ghost-1", "不存在"))
        assert unknown["ok"] is False

    asyncio.run(scenario())


def test_read_irc_timeout_and_change_cursor():
    team = make_team()
    worker = team.workers[0]

    async def scenario():
        raw = json.loads(await irc.send_irc(team, worker.agent.id, "等待回复"))
        cursor = raw["message"]["change_id"]
        timeout = json.loads(
            await irc.read_irc(team, {"after_id": cursor, "timeout_seconds": 1})
        )
        assert timeout["ok"] is True
        assert timeout["messages"] == []
        assert timeout.get("timed_out") is True

        async def later():
            await asyncio.sleep(0.05)
            await irc.deliver_worker_irc(team, worker)

        task = asyncio.create_task(later())
        streamed = json.loads(
            await irc.read_irc(team, {"after_id": cursor, "timeout_seconds": 2})
        )
        await task
        assert streamed["ok"] is True
        assert streamed["messages"] and streamed["messages"][0]["state"] == "delivered"
        assert raw["message"]["message_id"] == streamed["messages"][0]["message_id"]

    asyncio.run(scenario())


def test_validate_irc_session_rejects_bad_state():
    worker = WorkerSession(status=WorkerStatus(id="recon-1", phase="recon", status="pending"))
    session = TeamSession(
        version=2,
        phase="recon",
        workers=[worker],
        irc=[
            _IRCMessage(
                message_id=1,
                change_id=1,
                agent_id="recon-1",
                stage="recon",
                content="x",
                state="queued",
            )
        ],
        irc_next_id=1,
    )
    irc.validate_irc_session(session)
    session.irc[0].stage = "audit"
    with pytest.raises(ValueError):
        irc.validate_irc_session(session)
    session.irc[0].stage = "recon"
    session.irc[0].reply = "r"
    session.irc[0].state = "replied"
    with pytest.raises(ValueError):
        irc.validate_irc_session(session)
