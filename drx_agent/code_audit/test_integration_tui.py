"""Headless TUI flow over the real engine and a deterministic model."""

from __future__ import annotations

import asyncio
import os

import pytest

from drx_agent.code_audit import llm as llm_mod
from drx_agent.code_audit.agent import Team
from drx_agent.code_audit.config import PROMPTS_DIR, SKILLS_DIR, PromptConfig
from drx_agent.code_audit.prompt import load_prompts
from drx_agent.code_audit.test_integration_audit import (
    ScriptedTransport,
    _config,
    _make_workspace,
)
from drx_agent.code_audit.tools import Registry
from drx_agent.code_audit.tui import CodeAuditApp
from drx_agent.code_audit.tui import commands as commands_mod


def _build(tmp_path, monkeypatch):
    _make_workspace(tmp_path)
    cfg = _config(tmp_path)
    prompts = load_prompts(cfg.prompts)
    transport = ScriptedTransport()
    monkeypatch.setattr(llm_mod, "_TRANSPORT", transport)
    client = llm_mod.OpenAIClient(cfg.openai)
    compress = llm_mod.OpenAIClient(cfg.compress_openai)
    registry = Registry(cfg.workspace, cfg.agent.max_tool_result_chars)
    team = Team(cfg, prompts, client, compress, registry)
    app = CodeAuditApp(team, cfg, "", notices=["测试凭据"])
    return app, team, cfg


async def _wait_for(pilot, predicate, timeout=60.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await pilot.pause(0.05)
        try:
            if predicate():
                return True
        except Exception:
            pass
    return False


def test_tui_runs_full_audit_and_renders(tmp_path, monkeypatch):
    app, team, cfg = _build(tmp_path, monkeypatch)
    target = str(tmp_path / "target")

    async def scenario():
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                frame = app.render_snapshot()
                assert any("代码审计" in line for line in frame)
                assert any("团队进度" in line for line in frame)
                assert any("论坛" in line for line in frame)
                assert "请输入要审计的目录" in app.snapshot_text().replace("\n", "")

                await commands_mod.submit(app, target)
                assert await _wait_for(
                    pilot, lambda: not app.ui.busy and app.ui.phase == "completed"
                ), app.snapshot_text()
                frame = app.render_snapshot()
                text = "\n".join(frame)
                assert "全部审计 Agent 已完成" in text
                assert "app.py 命令注入" in text or "漏洞" in text

                await commands_mod.submit(app, "/list")
                await pilot.pause()
                text = app.snapshot_text()
                assert "漏洞列表" in text
                assert "app.py 命令注入" in text

                await commands_mod.submit(app, "/agents")
                await pilot.pause()
                assert "Agent 列表" in app.snapshot_text()

                await commands_mod.submit(app, "/help")
                await pilot.pause()
                assert "操作帮助" in app.snapshot_text()
        finally:
            await team.close()

    asyncio.run(scenario())


def test_tui_pause_confirmation_and_export(tmp_path, monkeypatch):
    app, team, cfg = _build(tmp_path, monkeypatch)
    target = str(tmp_path / "target")
    export_path = tmp_path / "audit-report.md"

    async def scenario():
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await commands_mod.submit(app, target)
                assert await _wait_for(
                    pilot, lambda: not app.ui.busy and app.ui.phase == "completed"
                ), app.snapshot_text()

                await commands_mod.submit(app, f"/export {export_path}")
                await pilot.pause()
                assert export_path.is_file()
                assert "app.py 命令注入" in export_path.read_text(encoding="utf-8")

                # Restarting from a completed run creates a fresh audit stage.
                await commands_mod.submit(app, "go")
                assert await _wait_for(pilot, lambda: app.ui.busy), app.snapshot_text()
                # Double Escape pauses; the first one only arms the guard.
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("escape")
                assert await _wait_for(pilot, lambda: not app.ui.busy), app.snapshot_text()
                assert app.ui.phase != "completed"
                assert not team.is_running
        finally:
            await team.close()

    asyncio.run(scenario())


def test_tui_stays_responsive_during_blocking_forum_wait(tmp_path, monkeypatch):
    import time

    from drx_agent.code_audit import test_agent_support as support

    _make_workspace(tmp_path)
    cfg = _config(tmp_path)
    cfg.agent.forum_wait_seconds = 2
    prompts = load_prompts(cfg.prompts)
    state = {"wait_started": False}

    def handler(messages, definitions, calls):
        names = {item.name for item in definitions}
        if "report_finding" in names:
            return support.tool_response(
                "review_state", {}, call_id="audit-%d" % calls
            )
        outputs = sum(
            1
            for message in messages
            if getattr(message, "type", "") == "function_call_output"
        )
        if outputs == 0:
            state["wait_started"] = True
            return support.tool_response(
                "forum_wait", {"timeout_seconds": 1.5}, call_id="wait-%d" % calls
            )
        return support.tool_response(
            "audit_plan_done",
            {
                "summary": "侦察完成",
                "audit_map": "app.py",
                "audit_files": ["app.py"],
            },
            call_id="plan-%d" % calls,
        )

    client = support.ScriptedClient(handler)
    team = Team(
        cfg,
        prompts,
        client,
        client,
        Registry(cfg.workspace, cfg.agent.max_tool_result_chars),
    )
    app = CodeAuditApp(team, cfg, "")
    target = str(tmp_path / "target")

    async def scenario():
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await commands_mod.submit(app, target)
                assert await _wait_for(pilot, lambda: state["wait_started"])
                ticks = 0

                async def ticker():
                    nonlocal ticks
                    while True:
                        await asyncio.sleep(0.05)
                        ticks += 1

                tick_task = asyncio.create_task(ticker())
                started = time.monotonic()
                await pilot.pause(0)
                idle_latency = time.monotonic() - started
                await asyncio.sleep(0.25)
                observed_ticks = ticks
                tick_task.cancel()
                # A blocking forum wait would hold the event loop; the pause
                # would take the whole timeout and the ticker would stall.
                assert idle_latency < 0.5, idle_latency
                assert observed_ticks >= 3, observed_ticks
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("escape")
                assert await _wait_for(
                    pilot, lambda: not app.ui.busy, timeout=10
                ), app.snapshot_text()
        finally:
            await team.close()

    asyncio.run(scenario())
