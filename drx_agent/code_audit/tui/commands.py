"""Command parsing and dispatch for the audit command line."""

from __future__ import annotations

import inspect
import json
import os
import re
from typing import Any

from drx_agent.code_audit.text import sanitize_data
from drx_agent.code_audit.tui import agents as agents_mod
from drx_agent.code_audit.tui import findings as findings_mod
from drx_agent.code_audit.tui import modal as modal_mod
from drx_agent.code_audit.tui.model import (
    DEFAULT_PAGE_SIZE,
    FileView,
    FindingView,
    PageView,
    UiState,
    norm_message,
    norm_snapshot,
)
from drx_agent.code_audit.tui.theme import bounded_text
from drx_agent.code_audit.tui.view import group_posts

COMMAND_NAMES = frozenset(
    {
        "go",
        "budget",
        "restore",
        "report",
        "list",
        "export",
        "save",
        "session",
        "sessions",
        "agents",
    }
)

HELP_LINES = [
    "输入目录或 /dir <目录>：开始新审计；支持带空格的引号路径。",
    "/budget：停止时打开预算表单；Tab 切换字段、←→ 选择模式、Ctrl+S 保存。"
    "0 表示不限；仅无限模式执行时间/token限额。",
    "Esc：对话框内关闭/取消；其他时候请求暂停，worker 停止后 go 继续。",
    "/say [消息]：打开广播草稿；Enter 换行，Ctrl+S 发送，Esc 取消。",
    "/thread <ID>：展开帖子及楼内回复；/forum [页码]：返回倒序帖子列表。",
    f"/search <关键词>：搜索标题、正文与回复；/search 清除；每页 {DEFAULT_PAGE_SIZE} 帖。",
    "/reply <ID> <内容>：回复帖子；论坛 Home/End 跳至当前页首/尾。",
    "/help、/report、/files [页码]、/sessions：独立对话框；只读对话框 gg 跳到底部。",
    "/list [页码]：漏洞选择列表；↑↓ 选择、Enter 详情、Esc 返回/关闭；后台继续运行。",
    "/agents：精简实时列表；↑↓/翻页/滚轮选择、Enter 完整诊断、Esc 返回列表。",
    "/export [文件]：完整 JSON 报告，默认 report.json。",
    "/save [文件]：保存当前会话；/session：选择会话，↑↓选择、Enter恢复。",
    "/restore [文件/片段]：支持省略.json、唯一片段；多个匹配弹窗选择，运行或保存时不可恢复。",
    "Tab 切换面板；论坛空输入时 ↑↓ 选帖、Enter 展开/收起、←→ 翻页。",
    "点击帖子标题展开/收起；PgUp/PgDn、鼠标滚轮在当前页独立滚动。",
    "主面板连续两次 Ctrl+G 跳到底部，不占用命令输入中的 g。",
    "/status：回到运行事件；窄屏用 Tab 切换状态与论坛。",
]


def parse_command(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if text.startswith("/"):
        text = text[1:]
    text = text.strip()
    if not text:
        return "", ""
    parts = text.split()
    command = parts[0].lower()
    return command, text[len(parts[0]) :].strip()


def clean_input_dir(path: str) -> str:
    text = (path or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    if text == "~" or text.startswith("~/") or text.startswith("~\\"):
        home = os.path.expanduser("~")
        text = os.path.join(home, text[1:].lstrip("/\\"))
    return text


def page_range(arg: str, total: int, page_size: int) -> tuple[int, int, int]:
    try:
        page = int((arg or "").strip() or 1)
    except ValueError:
        page = 1
    page = max(1, page)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return start, min(total, start + page_size), page


async def submit(app: Any, value: str) -> None:
    """Parse and execute one submitted command line."""
    ui: UiState = app.ui
    command, arg = parse_command(value)
    if value.startswith("/") or command in COMMAND_NAMES:
        await _dispatch(app, command, arg)
        return
    if ui.busy:
        ui.add_event("运行期间请使用 /say [消息] 打开广播草稿，Ctrl+S 确认发送。")
        return
    if ui.saving:
        ui.add_event("请等待保存完成。")
        return
    path = clean_input_dir(value)
    if not ui.workspace_ready:
        if ui.budget_cfg.infinite_mode:
            ui.pending_dir = path
            app.open_budget_prompt()
            return
        app.start_directory(path)
        return
    if os.path.isdir(path):
        if ui.budget_cfg.infinite_mode:
            ui.pending_dir = path
            app.open_budget_prompt()
            return
        app.start_directory(path)
        return
    app.run_query(value)


async def _dispatch(app: Any, command: str, arg: str) -> None:
    ui: UiState = app.ui
    if command == "say":
        app.open_broadcast(arg)
    elif command == "reply":
        id_text, _, content = arg.partition(" ")
        if not content or not id_text.isdigit() or int(id_text) <= 0:
            ui.add_event("用法：/reply <帖子或回复ID> <内容>")
            return
        try:
            result = app.team.reply_message(int(id_text), content)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # pragma: no cover - defensive
            ui.add_event("回复失败：" + str(exc))
        else:
            app.refresh_forum()
    elif command == "thread":
        if not arg.isdigit() or int(arg) <= 0:
            ui.add_event("用法：/thread <帖子ID>")
            return
        post_id = int(arg)
        messages = [norm_message(item) for item in (app.team.forum_messages() or [])]
        page = PageView(page=1, page_size=DEFAULT_PAGE_SIZE, total_pages=1, total_posts=1)
        page.gap = bool(messages) and messages[0].id > 1
        selected = None
        for post in group_posts(messages):
            if post.id == post_id:
                selected = post
                break
        if selected is None:
            ui.add_event("帖子不存在或已过期")
            return
        page.posts = [selected]
        ui.forum_page = page
        ui.selected_post = post_id
        ui.expanded_posts = {post_id}
        ui.forum_revision += 1
        ui.thread_id = post_id
        from drx_agent.code_audit.tui import view as view_mod

        title, lines = view_mod.show_forum_post_lines(ui, selected)
        app.open_detail(title, lines)
        ui.focus = 1
        ui.scroll[1] = 0
        ui.follow[1] = True
    elif command == "forum":
        page_number = 1
        if arg:
            if not arg.isdigit() or int(arg) < 1:
                ui.add_event("用法：/forum [正整数页码]")
                return
            page_number = int(arg)
        ui.thread_id = 0
        ui.forum_page.page = page_number
        ui.selected_post = 0
        ui.focus = 1
        ui.scroll[1] = 0
        ui.follow[1] = True
        app.refresh_forum()
    elif command == "search":
        ui.thread_id = 0
        ui.forum_search = arg
        ui.forum_page.page = 1
        ui.selected_post = 0
        ui.focus = 1
        ui.scroll[1] = 0
        ui.follow[1] = True
        app.refresh_forum()
    elif command == "go":
        if not ui.workspace_ready:
            ui.add_event("请先输入审计目录，或 /restore <会话文件>。")
            return
        query = "继续基于当前团队状态和论坛审计，不要从头开始。"
        if ui.last_query:
            query += "\n上次任务：\n" + ui.last_query
        previous = ui.last_query
        app.run_query(query)
        ui.last_query = previous
    elif command == "budget":
        if ui.busy or ui.saving:
            ui.add_event("运行或保存期间不可修改预算。")
            return
        app.open_budget_prompt()
    elif command == "dir":
        if ui.budget_cfg.infinite_mode:
            ui.pending_dir = clean_input_dir(arg)
            app.open_budget_prompt()
        else:
            app.start_directory(arg)
    elif command == "save":
        await app.save_session(clean_input_dir(arg) if arg else "")
    elif command in ("session", "sessions"):
        app.list_sessions(arg)
    elif command == "restore":
        await app.restore_session(arg)
    elif command == "list":
        app.show_findings(arg)
    elif command == "report":
        app.show_report()
    elif command == "export":
        app.export_report(arg)
    elif command == "files":
        app.show_files(arg)
    elif command == "agents":
        app.open_agents()
    elif command == "help":
        app.open_detail("操作帮助", HELP_LINES)
    elif command == "status":
        ui.focus = 0
        ui.follow[0] = True
    elif command == "stop":
        app.request_stop()
    else:
        ui.add_event("未知命令；输入 /help 查看帮助。")


# ---------------------------------------------------------------------------
# Snapshot-driven views


def refresh_snapshot(ui: UiState, team: Any) -> None:
    try:
        ui.snapshot = norm_snapshot(team.snapshot())
    except Exception:
        pass


def show_findings(ui: UiState, team: Any) -> None:
    refresh_snapshot(ui, team)
    start, _end, _page = page_range("", len(ui.snapshot.findings), 5)
    findings_mod.open_findings(ui, list(ui.snapshot.findings), start)


def findings_for_page(ui: UiState, arg: str) -> tuple[int, int, int]:
    return page_range(arg, len(ui.snapshot.findings), 5)


def report_lines(ui: UiState) -> list[str]:
    snapshot = ui.snapshot
    return [
        f"漏洞 {len(snapshot.findings)} · 文件 {len(snapshot.files)} · "
        f"Todo {len(snapshot.todos)} · 变量 {len(snapshot.variables)} · "
        f"链路 {len(snapshot.flows)}",
        "结论：" + bounded_text(snapshot.audit.summary, 4000),
        "下一步：" + bounded_text(snapshot.audit.next_steps, 2000),
        "项目笔记：" + bounded_text(snapshot.project.note, 4000),
        "/list 查看证据；/export 导出完整数据。",
    ]


def files_lines(ui: UiState, arg: str) -> list[str]:
    start, end, page = page_range(arg, len(ui.snapshot.files), 20)
    lines = [f"第 {page} 页 · 共 {len(ui.snapshot.files)} 个；/files <页码>"]
    for item in ui.snapshot.files[start:end]:
        lines.append("[" + item.status + "] " + bounded_text(item.path, 500))
        lines.append(bounded_text(item.note, 500))
    return lines


def report_payload(ui: UiState, revocations: list[Any] | None = None) -> dict[str, Any]:
    snapshot = ui.snapshot

    def convert(value: Any) -> Any:
        if hasattr(value, "__dict__"):
            return {key: convert(item) for key, item in vars(value).items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value

    reviewed = [item for item in snapshot.files if item.status in ("reviewed", "skipped")]
    unreviewed = [item for item in snapshot.files if item.status not in ("reviewed", "skipped")]

    def file_dict(item: FileView) -> dict[str, Any]:
        return {"path": item.path, "status": item.status, "note": item.note}

    return {
        "generated_at": _timestamp(),
        "audit": {
            "ended": snapshot.audit.ended,
            "summary": snapshot.audit.summary,
            "next_steps": snapshot.audit.next_steps,
        },
        "count": len(snapshot.findings),
        "findings": [_finding_dict(item) for item in snapshot.findings],
        "revocations": convert(revocations or []),
        "todos": [
            {
                "id": todo.id,
                "title": todo.title,
                "status": todo.status,
                "priority": todo.priority,
            }
            for todo in snapshot.todos
        ],
        "files": {
            "reviewed": [file_dict(item) for item in reviewed],
            "unreviewed": [file_dict(item) for item in unreviewed],
        },
        "variables": [
            {"name": item.name, "path": item.path, "status": item.status, "note": item.note}
            for item in snapshot.variables
        ],
        "flows": [
            {"name": item.name, "status": item.status, "note": item.note}
            for item in snapshot.flows
        ],
        "project_note": {"note": snapshot.project.note},
    }


def _findings_dicts(items: list[FindingView]) -> list[dict[str, Any]]:
    return [_finding_dict(item) for item in items]


def _finding_dict(item: FindingView) -> dict[str, Any]:
    return {
        "id": item.id,
        "severity": item.severity,
        "title": item.title,
        "path": item.path,
        "line": item.line,
        "evidence": item.evidence,
        "impact": item.impact,
        "recommendation": item.recommendation,
        "cwe": item.cwe,
    }


def _timestamp() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_report(ui: UiState, path: str, revocations: list[Any] | None = None) -> tuple[bool, str]:
    import stat

    payload = sanitize_data(report_payload(ui, revocations))
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
    except OSError as exc:
        return False, str(exc)
    return True, f"已完整导出 {len(ui.snapshot.findings)} 个漏洞及全部审计记录：{path}"


def list_session_files(session_dir: str) -> list[str]:
    """Directory listing hook kept separate so the interface stays testable."""
    return os.listdir(session_dir)


def session_matches(session_dir: str, query: str) -> list[str]:
    try:
        entries = list_session_files(session_dir)
    except OSError:
        return []
    base = os.path.basename((query or "").strip()).lower()
    if base.endswith(".json"):
        base = base[: -len(".json")]
    matches: list[str] = []
    for name in entries:
        path = os.path.join(session_dir, name)
        if os.path.isdir(path) or not name.lower().endswith(".json"):
            continue
        stem = name[: -len(".json")].lower()
        if not base or stem.startswith(base) or base in stem:
            matches.append(path)
    matches.sort(reverse=True)
    return matches


def resolve_session_path(session_dir: str, path: str) -> str | None:
    if os.path.isfile(path):
        return path
    candidates = [
        path + ".json",
    ]
    if not os.path.isabs(path):
        candidates.append(os.path.join(session_dir, path))
        candidates.append(os.path.join(session_dir, path + ".json"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def is_int(text: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+", (text or "").strip()))


__all__ = [
    "COMMAND_NAMES",
    "HELP_LINES",
    "clean_input_dir",
    "files_lines",
    "findings_for_page",
    "list_session_files",
    "parse_command",
    "page_range",
    "refresh_snapshot",
    "report_lines",
    "report_payload",
    "resolve_session_path",
    "session_matches",
    "show_findings",
    "submit",
    "write_report",
]
