"""Blocking tool calls must not freeze the asyncio event loop."""

from __future__ import annotations

import asyncio
import time

from drx_agent.code_audit import test_agent_support as s
from drx_agent.code_audit.agent import core
from drx_agent.code_audit.agent.core import new_worker
from drx_agent.code_audit import tools


class BlockingBoard:
    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay

    def call(self, ctx, agent_id, stage, name, args):
        time.sleep(self.delay)
        return '{"ok": true}'


def _agent():
    ws = s.new_workspace()
    cfg = s.make_config(ws)
    agent = new_worker(
        cfg, s.make_prompts(), None, None, tools.Registry(ws, 1024), "recon-1", "recon", None
    )
    return agent


def test_forum_wait_runs_off_the_event_loop():
    agent = _agent()
    agent.board = BlockingBoard(0.25)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(6):
                await asyncio.sleep(0.03)
                ticks += 1

        task = asyncio.create_task(ticker())
        result = await agent._forum_call("forum_wait", {"timeout_seconds": 0.1})
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert result == '{"ok": true}'
    assert ticks >= 4


def test_registry_tools_run_off_the_event_loop():
    class BlockingRegistry:
        def call_with_full_result(self, ctx, name, args):
            time.sleep(0.25)
            return ("bounded", "full")

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(6):
                await asyncio.sleep(0.03)
                ticks += 1

        task = asyncio.create_task(ticker())
        bounded, full = await core._call_registry(BlockingRegistry(), "read_file", {})
        await task
        return ticks, bounded, full

    ticks, bounded, full = asyncio.run(scenario())
    assert (bounded, full) == ("bounded", "full")
    assert ticks >= 4


def test_fast_forum_calls_stay_direct():
    class FastBoard:
        def __init__(self):
            self.calls = []

        def call(self, ctx, agent_id, stage, name, args):
            self.calls.append(name)
            return "{}"

    agent = _agent()
    board = FastBoard()
    agent.board = board
    assert asyncio.run(agent._forum_call("forum_post", {})) == "{}"
    assert board.calls == ["forum_post"]
