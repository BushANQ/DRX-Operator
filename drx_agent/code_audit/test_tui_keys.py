"""Key and mouse behaviour tests: wheel, shortcuts, tiny modal, resize."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from drx_agent.code_audit.test_tui_support import (
    FakeTeam,
    make_config,
    make_finding,
    make_post,
    make_snapshot,
    make_worker,
)
from drx_agent.code_audit.tui import CodeAuditApp
from drx_agent.code_audit.tui import modal as modal_mod
from drx_agent.code_audit.tui.theme import display_width

WORKDIR = os.path.dirname(os.path.abspath(__file__))


async def settle(pilot, rounds: int = 4) -> None:
    for _ in range(rounds):
        await pilot.pause()


def test_wheel_scrolls_focused_pane_and_moves_focus() -> None:
    async def scenario() -> None:
        posts = [make_post(index, f"topic-{index}", "body " * 40) for index in range(6, 0, -1)]
        team = FakeTeam(posts=posts, messages=[post.root for post in posts])
        app = CodeAuditApp(team, make_config(WORKDIR), "")
        async with app.run_test(size=(120, 20)) as pilot:
            await settle(pilot)
            assert app.ui.focus == 1
            assert app.ui.follow[1] is True
            app._wheel(SimpleNamespace(screen_x=100, screen_y=10), 3)
            await settle(pilot)
            assert app.ui.focus == 1
            assert app.ui.follow[1] is False
            assert app.ui.scroll[1] > 0
            app._wheel(SimpleNamespace(screen_x=3, screen_y=10), -3)
            await settle(pilot)
            assert app.ui.focus == 0
            assert app.ui.scroll[0] == 0
            # Wheel over the footer/command rows is ignored.
            app._wheel(SimpleNamespace(screen_x=100, screen_y=app.ui.height - 1), 3)
            await settle(pilot)
            assert app.ui.focus == 0

    asyncio.run(scenario())


def test_ctrl_c_while_running_stops_then_saves() -> None:
    async def scenario() -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(WORKDIR), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.set_input(WORKDIR)
            await pilot.press("enter")
            await settle(pilot, 5)
            assert app.ui.busy is True
            await pilot.press("ctrl+c")
            await settle(pilot)
            assert app.ui.quitting is True
            assert team.cancelled is True
            for _ in range(20):
                if not app.ui.busy:
                    break
                await pilot.pause()
            assert app.ui.busy is False
            assert team.saved

    asyncio.run(scenario())


def test_modal_render_follows_terminal_resize() -> None:
    async def scenario() -> None:
        team = FakeTeam(snapshot=make_snapshot(findings=[make_finding(1, title="one")]))
        app = CodeAuditApp(team, make_config(WORKDIR), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.set_input("/list")
            await pilot.press("enter")
            await settle(pilot)
            lines = app.render_snapshot()
            title_index = next(i for i, line in enumerate(lines) if "漏洞列表 · 1 个" in line)
            top = lines[title_index - 1]
            assert display_width(top[: top.index("╭")]) == 12
            await pilot.resize_terminal(70, 25)
            await settle(pilot)
            assert modal_mod.modal_size(app.ui) == (66, 23, 18)
            lines = app.render_snapshot()
            assert len(lines) == 25
            title_index = next(i for i, line in enumerate(lines) if "漏洞列表 · 1 个" in line)
            top = lines[title_index - 1]
            assert display_width(top[: top.index("╭", 1)]) == 2
            assert display_width(top) == 70
            for line in lines:
                assert display_width(line) <= 70

    asyncio.run(scenario())


def test_tiny_terminal_modal_dismiss_line() -> None:
    async def scenario() -> None:
        team = FakeTeam(snapshot=make_snapshot(findings=[make_finding(1)]))
        app = CodeAuditApp(team, make_config(WORKDIR), "")
        async with app.run_test(size=(18, 6)) as pilot:
            await settle(pilot)
            app.show_findings("")
            await settle(pilot)
            lines = app.render_snapshot()
            assert lines[0].startswith("漏洞列表 · 1 个")
            assert lines[1] == "请扩大终端；Esc 关"
            await pilot.press("enter")
            await settle(pilot)
            lines = app.render_snapshot()
            assert lines[1] == "请扩大终端；Esc 返"
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is not None
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None

    asyncio.run(scenario())


def test_agents_modal_mouse_selection() -> None:
    async def scenario() -> None:
        workers = [
            make_worker("w-1", "one", status="running", turn=1),
            make_worker("w-2", "two", status="running", turn=2),
        ]
        team = FakeTeam(workers=workers)
        app = CodeAuditApp(team, make_config(WORKDIR), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.open_agents()
            await settle(pilot)
            assert app.ui.modal.agent_index == 0
            # Body row 5 is the second worker block (starts are [2, 5]).
            await pilot.click("#modal-box", offset=(6, 7))
            await settle(pilot)
            assert app.ui.modal.agent_index == 1

    asyncio.run(scenario())


def test_gg_shortcut_reaches_modal_bottom_and_single_g_does_not() -> None:
    async def scenario() -> None:
        app = CodeAuditApp(FakeTeam(), make_config(WORKDIR), "")
        async with app.run_test(size=(120, 24)) as pilot:
            await settle(pilot)
            app.open_detail("reading", [f"line {index}" for index in range(200)])
            await settle(pilot)
            assert app.ui.modal.scroll == 0
            await pilot.press("g")
            await settle(pilot)
            assert app.ui.modal.scroll == 0
            await pilot.press("g")
            await settle(pilot)
            body = app.ui.modal
            width, height, body_height = modal_mod.modal_size(app.ui)
            assert body.scroll == max(0, len(body.wrapped) - body_height)
            await pilot.press("escape")
            await settle(pilot)

    asyncio.run(scenario())


def test_gg_not_captured_while_composing_broadcast() -> None:
    async def scenario() -> None:
        team = FakeTeam()
        app = CodeAuditApp(team, make_config(WORKDIR), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.open_broadcast("")
            await settle(pilot)
            await pilot.press("g")
            await pilot.press("g")
            await settle(pilot)
            assert app.ui.modal.compose_text == "gg"
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None

    asyncio.run(scenario())


def test_page_keys_scroll_left_pane_log() -> None:
    async def scenario() -> None:
        app = CodeAuditApp(FakeTeam(), make_config(WORKDIR), "")
        async with app.run_test(size=(120, 20)) as pilot:
            await settle(pilot)
            for index in range(40):
                app.ui.add_event(f"log line {index}")
            app.ui.focus = 0
            app.refresh_all()
            await pilot.press("home")
            await settle(pilot)
            assert app.ui.scroll[0] == 0
            assert app.ui.follow[0] is False
            await pilot.press("pagedown")
            await settle(pilot)
            assert app.ui.scroll[0] > 0
            await pilot.press("end")
            await settle(pilot)
            assert app.ui.follow[0] is True
            await pilot.press("home")
            await settle(pilot)
            assert app.ui.scroll[0] == 0
            assert app.ui.follow[0] is False

    asyncio.run(scenario())
