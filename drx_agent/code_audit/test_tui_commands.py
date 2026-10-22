"""Command dispatch and modal interaction tests for the audit console."""

from __future__ import annotations

import asyncio
import json
import os
from unittest import mock

from drx_agent.code_audit.test_tui_support import (
    FakeTeam,
    make_config,
    make_finding,
    make_message,
    make_post,
    make_snapshot,
    make_worker,
)
from drx_agent.code_audit.tui import CodeAuditApp
from drx_agent.code_audit.tui import commands as commands_mod
from drx_agent.code_audit.tui.commands import (
    clean_input_dir,
    page_range,
    parse_command,
    report_payload,
    resolve_session_path,
    session_matches,
)
from drx_agent.code_audit.tui.model import EventView, apply_event, norm_worker

WORKDIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(WORKDIR, "sessions")


async def settle(pilot, rounds: int = 4) -> None:
    for _ in range(rounds):
        await pilot.pause()


async def submit(app, pilot, text: str) -> None:
    app.set_input(text)
    await pilot.press("enter")
    await settle(pilot)


def test_parse_and_path_helpers() -> None:
    assert parse_command("/list 2") == ("list", "2")
    assert parse_command("go") == ("go", "")
    assert parse_command("/REPLY 3 hi there") == ("reply", "3 hi there")
    assert parse_command("   ") == ("", "")
    assert clean_input_dir('"C:\\dir with space"') == "C:\\dir with space"
    assert clean_input_dir("'x'") == "x"
    assert page_range("", 12, 5) == (0, 5, 1)
    assert page_range("3", 12, 5) == (10, 12, 3)
    assert page_range("99", 12, 5) == (10, 12, 3)
    assert page_range("oops", 3, 5) == (0, 3, 1)


def test_session_matching_helpers() -> None:
    names = [
        "20260101-000000.000.json",
        "20260102-000000.000.json",
        "notes.txt",
    ]
    known = {name for name in names if name.endswith(".json")}
    with mock.patch.object(commands_mod, "list_session_files", return_value=names), mock.patch(
        "os.path.isfile", side_effect=lambda path: os.path.basename(path) in known
    ):
        matches = session_matches(SESSION_DIR, "20260101")
        assert len(matches) == 1 and matches[0].endswith("20260101-000000.000.json")
        assert len(session_matches(SESSION_DIR, "")) == 2
        assert session_matches(SESSION_DIR, "missing") == []
        assert resolve_session_path(
            SESSION_DIR, os.path.join(SESSION_DIR, "20260101-000000.000")
        ) == os.path.join(SESSION_DIR, "20260101-000000.000.json")
        assert resolve_session_path(SESSION_DIR, "nope") is None


def test_help_report_files_and_agents_modals() -> None:
    async def scenario(path: str) -> None:
        workers = [
            make_worker("recon-1", "桃子", status="running", activity="思考中", turn=2),
            make_worker(
                "audit-1",
                "守望者",
                status="completed",
                phase="audit",
                activity="阶段完成",
                turn=6,
            ),
        ]
        snapshot = make_snapshot(
            findings=[make_finding(1, title="XSS"), make_finding(2, severity="low")],
            files=[
                __import__("types").SimpleNamespace(
                    path="src/a.py", status="reviewed", note="ok"
                )
            ],
            summary="done",
            next_steps="next",
            note="note",
        )
        team = FakeTeam(workers=workers, snapshot=snapshot)
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/help")
            joined = app.snapshot_text()
            assert "操作帮助" in joined
            assert "输入目录或 /dir <目录>：开始新审计；支持带空格的引号路径。" in joined
            assert "↑↓ 滚动 · PgUp/PgDn 翻页 · gg / End 到底 · Esc 关闭" in joined
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None

            await submit(app, pilot, "/report")
            joined = app.snapshot_text()
            assert "审计报告摘要" in joined
            assert "漏洞 2 · 文件 1 · Todo 0 · 变量 0 · 链路 0" in joined
            assert "结论：done" in joined
            assert "项目笔记：note" in joined
            await pilot.press("escape")
            await settle(pilot)

            await submit(app, pilot, "/files")
            joined = app.snapshot_text()
            assert "文件覆盖详情" in joined
            assert "第 1 页 · 共 1 个；/files <页码>" in joined
            assert "[reviewed] src/a.py" in joined
            await pilot.press("escape")
            await settle(pilot)

            await submit(app, pilot, "/agents")
            joined = app.snapshot_text()
            assert "Agent 列表 · 2 个" in joined
            assert "> [running] 桃子 · #2" in joined
            await pilot.press("down")
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            joined = app.snapshot_text()
            assert "Agent 详情 · 守望者" in joined
            assert "── 状态 ──" in joined
            assert "ID：audit-1" in joined
            assert "── 最近输出摘录 ──" in joined
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is not None and app.ui.modal.kind == "agents"
            assert "Agent 列表 · 2 个" in app.snapshot_text()
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None

    asyncio.run(scenario(WORKDIR))


def test_findings_list_detail_and_escape_nesting() -> None:
    async def scenario(path: str) -> None:
        findings = [
            make_finding(1, severity="high", title="first"),
            make_finding(2, severity="medium", title="second", path="src/b.py", line=9),
        ]
        team = FakeTeam(snapshot=make_snapshot(findings=findings))
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/list")
            joined = app.snapshot_text()
            assert "漏洞列表 · 2 个" in joined
            assert "选中 1 / 2 · 本次快照" in joined
            assert "> #1 [high] first" in joined
            await pilot.press("down")
            await settle(pilot)
            assert "选中 2 / 2 · 本次快照" in app.snapshot_text()
            await pilot.press("enter")
            await settle(pilot)
            joined = app.snapshot_text()
            assert "漏洞详情 · 2 / 2 · #2" in joined
            assert "── 证据 ──" in joined
            assert "位置 · src/b.py:9" in joined
            assert "↑↓ 滚动 · PgUp/PgDn 翻页 · gg / End 到底 · Esc 返回列表" in joined
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is not None and app.ui.modal.findings.detail is False
            assert "漏洞列表 · 2 个" in app.snapshot_text()
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None

    asyncio.run(scenario(WORKDIR))


def test_agents_modal_follows_live_roster() -> None:
    async def scenario(path: str) -> None:
        workers = [make_worker("w-1", "one"), make_worker("w-2", "two")]
        team = FakeTeam(workers=workers)
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/agents")
            apply_event(
                app.ui,
                EventView(
                    kind="worker",
                    workers=[norm_worker(make_worker("w-2", "two renamed"))],
                ),
            )
            await settle(pilot)
            assert "two renamed" in app.snapshot_text()
            await pilot.press("escape")
            await settle(pilot)

    asyncio.run(scenario(WORKDIR))


def test_broadcast_compose_requires_send_and_posts_once() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam()
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/say hello")
            assert app.ui.modal is not None and app.ui.modal.compose is True
            assert app.ui.modal.compose_text == "hello"
            await pilot.press("s")
            await settle(pilot)
            assert app.ui.modal.compose_text == "hellos"
            assert team.posted == []
            await pilot.press("ctrl+s")
            await settle(pilot)
            assert team.posted == ["hellos"]
            assert app.ui.modal is None
            assert any(
                event.content.startswith("用户广播了：hellos")
                for event in app.ui.events
            )

            await submit(app, pilot, "/say")
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None
            assert team.posted == ["hellos"]

    asyncio.run(scenario(WORKDIR))


def test_budget_form_parses_infinite_and_numeric_fields() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam()
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/budget")
            assert app.ui.modal.budget is True
            assert "审计预算 · /budget" in app.snapshot_text()
            assert "普通模式不限额；下列预算仅在无限审计模式生效" in app.snapshot_text()
            # Hours: clear the prefilled 8, then type 2.
            await pilot.press("tab")
            await pilot.press("backspace")
            await pilot.press("2")
            await pilot.press("tab")
            await pilot.press("backspace")
            await pilot.press("3", "0")
            await pilot.press("tab")
            await pilot.press("backspace")
            await pilot.press("5", "0", "0")
            await settle(pilot)
            assert app.ui.modal.budget_form.fields == ["2", "30", "500"]
            await pilot.press("ctrl+s")
            await settle(pilot)
            assert team.budget_calls == [(False, 2, 30, 500)]
            assert app.ui.modal is None

            await submit(app, pilot, "/budget")
            await pilot.press("left")
            await settle(pilot)
            assert app.ui.modal.budget_form.infinite is True
            assert "时间与 token 任一用尽即停；修改预算不重置已用量" in app.snapshot_text()
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None

    asyncio.run(scenario(WORKDIR))


def test_budget_rejects_invalid_and_negative_values() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam()
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/budget")
            await pilot.press("tab")
            await pilot.press("backspace")
            await pilot.press("minus")
            await pilot.press("1")
            await pilot.press("ctrl+s")
            await settle(pilot)
            assert app.ui.modal is not None
            assert "请输入非负整数；0 表示不限" in app.snapshot_text()
            assert team.budget_calls == []
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.modal is None

    asyncio.run(scenario(WORKDIR))


def test_infinite_mode_defers_start_until_budget_confirmed() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam()
        app = CodeAuditApp(team, make_config(path, infinite_mode=True), auto_dir=path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.ui.modal is not None and app.ui.modal.budget is True
            assert app.ui.pending_dir == path
            assert app.ui.busy is False
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.pending_dir == ""
            assert app.ui.modal is None
            assert app.ui.busy is False
            assert team.run_texts == []

            await submit(app, pilot, "/budget")
            await pilot.press("ctrl+s")
            await settle(pilot)
            assert app.ui.modal is None
            assert app.ui.busy is False

    asyncio.run(scenario(WORKDIR))


def test_sessions_listing_and_restore() -> None:
    first = os.path.join(SESSION_DIR, "20260101-000000.000.json")
    second = os.path.join(SESSION_DIR, "20260102-000000.000.json")

    async def scenario() -> None:
        team = FakeTeam()
        app = CodeAuditApp(team, make_config(SESSION_DIR), "")
        with mock.patch.object(
            commands_mod, "list_session_files", return_value=["a.json", "b.json"]
        ), mock.patch.object(
            commands_mod,
            "session_matches",
            side_effect=lambda _dir, query: (
                [second, first]
                if not query or "202601" in query
                else []
            ),
        ), mock.patch.object(
            commands_mod,
            "resolve_session_path",
            side_effect=lambda _dir, path: path if path.endswith(".json") else None,
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await settle(pilot)
                await submit(app, pilot, "/session")
                joined = app.snapshot_text()
                assert "已保存会话 · ↑↓ 选择 Enter 恢复" in joined
                assert app.ui.modal.sessions.items == [second, first]
                assert app.ui.modal.sessions.selected == 0
                assert "> " + second in joined
                assert "  " + first in joined
                await pilot.press("enter")
                await settle(pilot, 6)
                assert team.loaded == [second]
                assert app.ui.session_path == second
                assert app.ui.workspace == "已恢复会话"
                assert app.ui.workspace_ready is True
                assert any(
                    event.content.startswith("已恢复会话：") for event in app.ui.events
                )

                # Ambiguous restore opens the picker again.
                await submit(app, pilot, "/restore 202601")
                assert app.ui.modal is not None and app.ui.modal.kind == "sessions"
                await pilot.press("escape")
                await settle(pilot)

                # Tab completion expands a unique /restore fragment.
                app.set_input("/restore " + first)
                await pilot.press("tab")
                await settle(pilot)
                assert app.input_value() == '/restore "' + first + '"'

    asyncio.run(scenario())


def test_restore_failure_is_reported_in_modal() -> None:
    broken = os.path.join(SESSION_DIR, "20260101-000000.000.json")

    async def scenario() -> None:
        team = FakeTeam(fail_run="load")
        app = CodeAuditApp(team, make_config(SESSION_DIR), "")
        with mock.patch.object(
            commands_mod,
            "resolve_session_path",
            side_effect=lambda _dir, path: broken,
        ), mock.patch.object(
            commands_mod, "list_session_files", return_value=[]
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await settle(pilot)
                await submit(app, pilot, "/restore 20260101")
                await settle(pilot, 6)
                assert any(
                    event.content.startswith("恢复会话失败：")
                    for event in app.ui.events
                )
                assert app.ui.session_path == ""

    asyncio.run(scenario())


def test_export_report_payload_and_failure_log() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam(snapshot=make_snapshot(findings=[make_finding(1, title="XSS")]))
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            with mock.patch.object(
                commands_mod, "write_report", return_value=(True, "已完整导出 1 个漏洞")
            ) as patched:
                await submit(app, pilot, "/export report.json")
                await settle(pilot)
                patched.assert_called_once()
                assert any(
                    event.content == "已完整导出 1 个漏洞" for event in app.ui.events
                )
            with mock.patch.object(
                commands_mod, "write_report", return_value=(False, "拒绝访问")
            ):
                await submit(app, pilot, "/export report.json")
                await settle(pilot)
                assert any(
                    event.content == "导出失败：拒绝访问" for event in app.ui.events
                )

    asyncio.run(scenario(WORKDIR))


def test_report_payload_is_complete() -> None:
    from types import SimpleNamespace

    from drx_agent.code_audit.tui.model import norm_snapshot

    snapshot = norm_snapshot(
        make_snapshot(
            findings=[make_finding(1, title="XSS")],
            files=[
                SimpleNamespace(path="src/a.py", status="reviewed", note=""),
                SimpleNamespace(path="src/b.py", status="unreviewed", note=""),
            ],
            todos=[SimpleNamespace(id=1, title="todo", status="done", priority="high")],
            summary="s",
            next_steps="n",
            note="p",
        )
    )
    from drx_agent.code_audit.tui.model import UiState

    state = UiState()
    state.snapshot = snapshot
    payload = report_payload(state)
    assert payload["count"] == 1
    assert payload["findings"][0]["title"] == "XSS"
    assert payload["audit"] == {"ended": False, "summary": "s", "next_steps": "n"}
    assert [item["path"] for item in payload["files"]["reviewed"]] == ["src/a.py"]
    assert [item["path"] for item in payload["files"]["unreviewed"]] == ["src/b.py"]
    assert payload["todos"][0]["title"] == "todo"
    assert payload["project_note"] == {"note": "p"}
    json.dumps(payload)


def test_thread_search_reply_and_status_commands() -> None:
    async def scenario(path: str) -> None:
        root = make_message(1, "root body", topic="入口证据")
        team = FakeTeam(messages=[root], posts=[make_post(1, "入口证据", "root body")])
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/thread 1")
            assert app.ui.thread_id == 1
            assert app.ui.modal is not None
            assert "论坛帖子 #1 · 入口证据" in app.snapshot_text()
            assert "帖子 #1 · /forum 返回" in app.snapshot_text()
            await pilot.press("escape")
            await settle(pilot)
            assert app.ui.thread_id == 1
            await submit(app, pilot, "/forum")
            assert app.ui.thread_id == 0
            await submit(app, pilot, "/search body")
            assert app.ui.forum_search == "body"
            await submit(app, pilot, "/reply 1 answer")
            assert team.replied == [(1, "answer")]
            await submit(app, pilot, "/reply nope answer")
            assert any(
                event.content == "用法：/reply <帖子或回复ID> <内容>"
                for event in app.ui.events
            )
            await submit(app, pilot, "/status")
            assert app.ui.focus == 0
            assert app.ui.follow[0] is True
            await submit(app, pilot, "/nonsense")
            assert any(
                event.content == "未知命令；输入 /help 查看帮助。"
                for event in app.ui.events
            )

    asyncio.run(scenario(WORKDIR))


def test_stop_command_requests_pause() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.set_input(path)
            await pilot.press("enter")
            await settle(pilot, 5)
            assert app.ui.busy is True
            await submit(app, pilot, "/stop")
            assert team.cancelled is True
            for _ in range(20):
                if not app.ui.busy:
                    break
                await pilot.pause()
            assert app.ui.busy is False
            assert any(
                event.content == "本轮已停止；输入 go 继续，或 /export 导出报告。"
                for event in app.ui.events
            )
            await submit(app, pilot, "/stop")
            assert any(
                event.content == "当前没有运行中的审计。" for event in app.ui.events
            )

    asyncio.run(scenario(WORKDIR))


def test_dir_command_starts_audit_or_prompts_budget() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/dir " + path)
            await settle(pilot, 5)
            assert team.workspaces == [path]
            assert app.ui.busy is True

    asyncio.run(scenario(WORKDIR))

    async def infinite(path: str) -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(path, infinite_mode=True), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/dir " + path)
            assert app.ui.modal is not None and app.ui.modal.budget is True
            assert app.ui.pending_dir == path
            assert team.workspaces == []
            await pilot.press("ctrl+s")
            await settle(pilot, 6)
            assert team.workspaces == [path]

    asyncio.run(infinite(WORKDIR))


def test_save_command_writes_session() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam()
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await submit(app, pilot, "/save")
            await settle(pilot)
            assert any(
                event.content == "尚未打开工作区，无法保存。" for event in app.ui.events
            )
            await submit(app, pilot, "/dir " + path)
            await settle(pilot, 6)
            assert app.ui.workspace_ready is True
            await submit(app, pilot, "/save")
            await settle(pilot, 6)
            assert team.saved
            assert any(
                event.content.startswith("已保存会话：") for event in app.ui.events
            )

    asyncio.run(scenario(WORKDIR))


def test_go_after_pause_continues_with_previous_task() -> None:
    async def scenario(path: str) -> None:
        team = FakeTeam(block=True)
        app = CodeAuditApp(team, make_config(path), "")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.set_input(path)
            await pilot.press("enter")
            await settle(pilot, 5)
            await submit(app, pilot, "/stop")
            for _ in range(20):
                if not app.ui.busy:
                    break
                await pilot.pause()
            assert app.ui.busy is False
            previous = app.ui.last_query
            await submit(app, pilot, "go")
            await settle(pilot, 5)
            assert app.ui.busy is True
            assert team.run_texts[-1].startswith("继续基于当前团队状态和论坛审计")
            assert "上次任务：" in team.run_texts[-1]
            assert previous in team.run_texts[-1]

    asyncio.run(scenario(WORKDIR))
