"""Headless application tests: startup, layout, keys, run lifecycle, mouse."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import os

from drx_agent.code_audit.test_tui_support import (
    BrokenTeam,
    FakeTeam,
    make_config,
    make_post,
    make_worker,
)
from drx_agent.code_audit.tui import CodeAuditApp
from drx_agent.code_audit.tui.model import PLACEHOLDER
from drx_agent.code_audit.tui.theme import display_width

WORKDIR = os.path.dirname(os.path.abspath(__file__))


async def settle(pilot, rounds: int = 3) -> None:
    for _ in range(rounds):
        await pilot.pause()


def test_startup_layout_and_notices() -> None:
    async def scenario() -> None:
        app = CodeAuditApp(
            FakeTeam(), make_config(), "", notices=["使用环境变量 DRX_LLM_API_KEY 中的模型凭据"]
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            lines = app.render_snapshot()
            assert len(lines) == 40
            assert lines[0].startswith("代码审计 · 侦察 ·")
            assert "│ │ 论坛 1/1页 · 0帖 · 最新在前" in lines[2]
            assert "团队进度 / 操作" in lines[2]
            assert "覆盖 0/0 · Todo 0 · 漏洞 0" in lines[3]
            assert "团队尚未启动" in lines[6]
            assert lines[38].startswith("Tab 面板")
            assert lines[39] == "> " + PLACEHOLDER
            assert app.query_one("#left").size.width == 43
            assert app.query_one("#forum").size.width == 76
            assert app.query_one("#left").size.height == 37
            assert app.query_one("#command").placeholder == PLACEHOLDER
            contents = [event.content for event in app.ui.events]
            assert contents[0] == "使用环境变量 DRX_LLM_API_KEY 中的模型凭据"
            assert contents[1] == "请输入要审计的目录；论坛只显示真实通信。"
            for line in lines:
                assert display_width(line) <= 120

    asyncio.run(scenario())


def test_engine_startup_failure_is_logged_not_raised() -> None:
    async def scenario() -> None:
        app = CodeAuditApp(BrokenTeam(), make_config(), "")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            lines = app.render_snapshot()
            assert len(lines) == 30
            assert "代码审计" in lines[0]
            assert any(
                event.content.startswith("初始化审计团队失败：")
                for event in app.ui.events
            )
            await pilot.press("tab")
            await pilot.pause()

    asyncio.run(scenario())


def test_directory_submission_starts_run() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.set_input(path)
            await pilot.press("enter")
            await settle(pilot, 5)
            assert team.workspaces == [path]
            assert app.ui.workspace_ready is True
            assert app.ui.workspace == path
            assert app.ui.busy is True
            assert team.run_texts and "安全审计" in team.run_texts[0]
            header = app.render_snapshot()[0]
            assert "/ 运行中" in header
            assert "启动团队审计；Esc 暂停，/say 打开广播草稿，Ctrl+S 发送。" in [
                event.content for event in app.ui.events
            ]

    asyncio.run(scenario(WORKDIR))


def test_pause_requires_two_escapes_and_stops_run() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.set_input(path)
            await pilot.press("enter")
            await settle(pilot, 5)
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.escape_armed is True
            assert team.cancelled is False
            assert "再按一次 Esc 暂停审计；其他按键取消确认。" in [
                event.content for event in app.ui.events
            ]
            # Another key disarms the confirmation.
            await pilot.press("x")
            await settle(pilot)
            assert app.ui.escape_armed is False
            await pilot.press("escape")
            await pilot.press("escape")
            await settle(pilot, 6)
            assert team.cancelled is True
            for _ in range(20):
                if not app.ui.busy:
                    break
                await pilot.pause()
            assert app.ui.busy is False
            assert app.ui.stopping is False
            contents = [event.content for event in app.ui.events]
            assert "本轮已停止；输入 go 继续，或 /export 导出报告。" in contents
            assert app.ui.saving is False
            assert team.saved  # session auto-saved at the end of the run

    asyncio.run(scenario(WORKDIR))


def test_go_without_workspace_is_rejected() -> None:
    async def scenario() -> None:
        app = CodeAuditApp(FakeTeam(), make_config(), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("g", "o")
            await pilot.press("enter")
            await settle(pilot)
            assert "请先输入审计目录，或 /restore <会话文件>。" in [
                event.content for event in app.ui.events
            ]
            assert app.ui.busy is False

    asyncio.run(scenario())


def test_tab_toggles_pane_and_narrow_layout_follows_focus() -> None:
    async def scenario() -> None:
        app = CodeAuditApp(FakeTeam(), make_config(), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.ui.focus == 1
            await pilot.press("tab")
            await settle(pilot)
            assert app.ui.focus == 0
            await pilot.press("tab")
            await settle(pilot)
            assert app.ui.focus == 1

            await pilot.resize_terminal(70, 25)
            await settle(pilot)
            assert app.ui.width == 70
            lines = app.render_snapshot()
            assert len(lines) == 25
            assert lines[0].startswith("审计 · 侦察 · Tab: 状态/论坛")
            assert "论坛 1/1页" in "\n".join(lines)
            assert "团队进度 / 操作" not in "\n".join(lines)
            await pilot.press("tab")
            await settle(pilot)
            lines = app.render_snapshot()
            assert "团队进度 / 操作" in "\n".join(lines)
            assert "论坛 1/1页" not in "\n".join(lines)

            await pilot.resize_terminal(18, 6)
            await settle(pilot)
            lines = app.render_snapshot()
            assert len(lines) == 3
            assert lines[0] == "代码审计 · 侦察 · "
            assert lines[1] == "请扩大终端；Esc 停"
            for line in lines:
                assert display_width(line) <= 18

    asyncio.run(scenario())


def test_enter_opens_post_modal_and_escape_closes() -> None:
    async def scenario() -> None:
        posts = [make_post(1, "入口证据", "root evidence")]
        team = FakeTeam(posts=posts, messages=[posts[0].root])
        app = CodeAuditApp(team, make_config(), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.ui.selected_post == 1
            await pilot.press("enter")
            await settle(pilot)
            assert app.ui.modal is not None
            lines = app.render_snapshot()
            assert "论坛帖子 #1 · 入口证据" in "\n".join(lines)
            assert "root evidence" in "\n".join(lines)
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None
            assert app.ui.busy is False

    asyncio.run(scenario())


def test_forum_arrow_selection_and_wheel_scroll() -> None:
    async def scenario() -> None:
        posts = [
            make_post(3, "third", "c" * 50),
            make_post(2, "second", "b" * 50),
            make_post(1, "first", "a" * 50),
        ]
        team = FakeTeam(posts=posts, messages=[post.root for post in posts])
        app = CodeAuditApp(team, make_config(), "")
        async with app.run_test(size=(120, 20)) as pilot:
            await settle(pilot)
            first = app.ui.selected_post
            await pilot.press("down")
            await settle(pilot)
            assert app.ui.selected_post != first
            await pilot.press("up")
            await settle(pilot)
            assert app.ui.selected_post == first
            await pilot.press("pagedown")
            await settle(pilot)
            assert app.ui.follow[1] is False
            assert app.ui.scroll[1] > 0
            await pilot.press("home")
            await settle(pilot)
            assert app.ui.scroll[1] == 0
            assert app.ui.follow[1] is True

    asyncio.run(scenario())


def test_click_post_header_opens_modal() -> None:
    async def scenario() -> None:
        posts = [make_post(1, "clickable", "body")]
        team = FakeTeam(posts=posts, messages=[posts[0].root])
        app = CodeAuditApp(team, make_config(), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.click("#forum", offset=(4, 2))
            await settle(pilot)
            assert app.ui.modal is not None
            assert "clickable" in "\n".join(app.render_snapshot())

    asyncio.run(scenario())


def test_ctrl_c_quits_when_idle() -> None:
    async def scenario() -> None:
        app = CodeAuditApp(FakeTeam(), make_config(), "")
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await pilot.press("ctrl+c")
            await settle(pilot)
        assert app.return_code == 0

    asyncio.run(scenario())


def test_worker_rows_render_team_progress() -> None:
    async def scenario() -> None:
        workers = [
            make_worker(
                "recon-1",
                "桃子",
                status="running",
                activity="思考中",
                turn=4,
                last_model_activity="2026-09-13T11:22:33Z",
                generation=SimpleNamespace(
                    output_tokens=1234,
                    reasoning_tokens=1200,
                    tool_tokens=0,
                    estimated=True,
                    received_at="2026-09-13T11:22:33Z",
                ),
            ),
            make_worker("audit-1", "守望者", status="completed", activity="阶段完成", turn=8),
        ]
        team = FakeTeam(workers=workers)
        app = CodeAuditApp(team, make_config(), "")
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            joined = "\n".join(app.render_snapshot())
            assert "[运行] 桃子 · #4" in joined
            assert "generation≈1234 tok" in joined
            assert "等待首段输出" not in joined
            assert "[完成] 守望者 · #8" in joined
            await pilot.press("tab")
            await settle(pilot)
            assert "[FAIL]" not in joined
            await pilot.press("tab")
            await settle(pilot)

    asyncio.run(scenario())


def test_auto_dir_starts_audit_immediately() -> None:
    async def scenario() -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(WORKDIR), auto_dir=WORKDIR)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, 6)
            assert team.workspaces == [WORKDIR]
            assert app.ui.busy is True
            assert app.ui.workspace_ready is True
            assert "启动团队审计" in "\n".join(
                event.content for event in app.ui.events
            )

    asyncio.run(scenario())
