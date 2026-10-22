"""Rendering, layout and text-pipeline tests for the audit terminal interface."""

from __future__ import annotations

from types import SimpleNamespace

from drx_agent.code_audit.tui import findings as findings_mod
from drx_agent.code_audit.tui import modal as modal_mod
from drx_agent.code_audit.tui import view as view_mod
from drx_agent.code_audit.tui.model import (
    MessageView,
    PageView,
    PostView,
    UiState,
    compute_layout,
    format_duration,
    norm_budget,
    norm_message,
    norm_page,
    norm_snapshot,
    parse_timestamp,
)
from drx_agent.code_audit.tui.theme import (
    DANGER,
    FRAME,
    FOCUS,
    MUTED,
    SELECTED_BG,
    SUCCESS,
    WARNING,
    bounded_text,
    classify_line,
    display_width,
    fit_line,
    forum_excerpt,
    paint_line,
    wrap_lines,
)
from drx_agent.code_audit.test_tui_support import (
    make_budget,
    make_finding,
    make_message,
    make_post,
    make_snapshot,
)


def _page(posts: list[PostView], total: int | None = None) -> PageView:
    return PageView(
        posts=posts,
        page=1,
        page_size=60,
        total_posts=len(posts) if total is None else total,
        total_pages=1,
        has_more=False,
        gap=False,
    )


def _post(post_id: int, topic: str, content: str, *, pinned: bool = False,
          replies: list[MessageView] | None = None) -> PostView:
    return PostView(
        id=post_id,
        root=MessageView(
            id=post_id,
            agent_id="user",
            agent_name="user",
            stage="user",
            topic=topic,
            content=content,
            created_at="2026-09-13T10:00:00Z",
            pinned=pinned,
        ),
        replies=list(replies or []),
        updated_at="2026-09-13T10:00:00Z",
    )


# ---------------------------------------------------------------------------
# Layout math


def test_layout_matches_reference_widths() -> None:
    assert compute_layout(120, 40) == compute_layout(120, 40)
    layout = compute_layout(120, 40)
    assert (layout.left, layout.right, layout.height, layout.narrow) == (43, 76, 37, False)
    layout = compute_layout(132, 38)
    assert (layout.left, layout.right, layout.height) == (47, 84, 35)
    assert compute_layout(90, 30).left == 32
    assert compute_layout(89, 30).narrow is True
    assert compute_layout(89, 30).left == 89
    assert compute_layout(70, 25).left == 70
    assert compute_layout(18, 6).left == 18
    assert compute_layout(200, 60).left == 72
    assert compute_layout(200, 60).right == 127
    assert compute_layout(120, 4).height == 3


# ---------------------------------------------------------------------------
# Startup frame


def test_startup_frame_layout() -> None:
    state = UiState()
    state.add_event("请输入要审计的目录；论坛只显示真实通信。")
    lines = view_mod.snapshot(state, "")
    assert len(lines) == 40
    assert lines[0].startswith("代码审计 · 侦察 ·")
    assert "│ │ 论坛 1/1页 · 0帖 · 最新在前" in lines[2]
    assert "团队进度 / 操作" in lines[2]
    assert "覆盖 0/0 · Todo 0 · 漏洞 0" in lines[3]
    assert "普通 · 用时 0s / 不限" in lines[4]
    assert "tokens 0 / 不限" in lines[5]
    assert "团队尚未启动" in lines[6]
    assert "── 最近操作 ──" in lines[8]
    pinned, pane_lines = view_mod.left_content(state, 39, 37)
    flat = "".join(pinned + pane_lines)
    assert "请输入要审计的目录" in flat
    assert "论坛只显示真实通信。" in flat
    assert "当前页没有帖子；/search 清除搜索" in "\n".join(lines)
    assert lines[38].startswith("Tab 面板")
    assert lines[39] == "> audit directory / go /budget /help"
    for line in lines:
        assert display_width(line) <= 120
    # Wide pane rows are exactly the terminal width.
    assert display_width(lines[1]) == 120
    assert display_width(lines[37]) == 120


def test_narrow_and_tiny_frames() -> None:
    narrow = UiState(width=70, height=25)
    narrow.phase = "audit"
    lines = view_mod.snapshot(narrow, "")
    assert len(lines) == 25
    assert lines[0].startswith("审计 · 审计 · Tab: 状态/论坛")
    assert "论坛 1/1页 · 0帖 · 最新在前" in lines[2]
    assert "团队进度 / 操作" not in "\n".join(lines)
    assert lines[23].startswith("↑↓选帖 Enter详情")
    assert lines[24] == "> audit directory / go /budget /help"
    for line in lines:
        assert display_width(line) <= 70

    tiny = UiState(width=18, height=6)
    lines = view_mod.snapshot(tiny, "")
    assert len(lines) == 3
    assert lines[0] == "代码审计 · 侦察 · "
    assert lines[1] == "请扩大终端；Esc 停"
    assert display_width(lines[0]) == 18


def test_narrow_focus_switches_pane() -> None:
    state = UiState(width=70, height=25)
    state.focus = 0
    lines = view_mod.snapshot(state, "")
    assert "团队进度 / 操作" in "\n".join(lines)
    assert "论坛 1/1页" not in "\n".join(lines)


# ---------------------------------------------------------------------------
# Header states


def test_header_state_suffixes() -> None:
    state = UiState()
    state.workspace = "Z:\\target"
    state.busy = True
    assert view_mod.header_line(state).startswith("代码审计 · 侦察 / 运行中 · Z:")
    state.stopping = True
    state.saving = True
    assert "正在停止" in view_mod.header_line(state)
    assert "保存中" in view_mod.header_line(state)
    state.busy = False
    state.stopping = False
    state.saving = False
    state.phase = "completed"
    assert view_mod.header_line(state).startswith("代码审计 · 完成 · Z:")


# ---------------------------------------------------------------------------
# Left pane


def test_left_pane_team_rows_and_log() -> None:
    state = UiState()
    state.budget = norm_budget(make_budget(infinite_mode=True, hours=2, minutes=30))
    state.snapshot = norm_snapshot(
        make_snapshot(
            files=[SimpleNamespace(path="a.py", status="reviewed", note="")],
            todos=[SimpleNamespace(id=1, title="t", status="done", priority="high")],
            findings=[make_finding(1)],
        )
    )
    state.workers = [
        view_mod.norm_worker(
            SimpleNamespace(
                id="recon-1",
                name="桃子",
                status="running",
                activity="思考中",
                turn=3,
                generation=SimpleNamespace(
                    output_tokens=42, estimated=True, received_at="2026-09-13T11:22:33Z"
                ),
                last_model_activity="2026-09-13T11:22:33Z",
            )
        )
    ]
    state.add_event("calling read_file", "recon-1")
    pinned, lines = view_mod.left_content(state, 39, 37)
    joined = "\n".join(pinned + lines)
    assert "覆盖 1/1 · Todo 1 · 漏洞 1" in joined
    assert "无限 · 用时 0s / 2h30m" in joined
    assert "tokens 0 / 不限" in joined
    assert "[运行] 桃子 · #3" in joined
    assert "generation≈42 tok" in joined
    assert "输出 11:" in joined
    assert "── 最近操作 ──" in joined
    assert "calling read_file" in joined
    assert "recon-1" not in joined


def test_left_pane_worker_overflow_hint() -> None:
    state = UiState(height=12)
    state.workers = [
        view_mod.norm_worker(SimpleNamespace(id=f"w-{index}", name=f"n{index}", status="running"))
        for index in range(8)
    ]
    pinned, lines = view_mod.left_content(state, 39, 9)
    joined = "\n".join(pinned + lines)
    assert "提示：另" in joined
    assert "/agents" in joined


# ---------------------------------------------------------------------------
# Forum content


def test_forum_list_rendering() -> None:
    state = UiState()
    reply = MessageView(
        id=2,
        agent_id="audit-1",
        agent_name="守望者",
        stage="audit",
        reply_to=1,
        content="reply evidence",
        created_at="2026-09-13T10:02:00Z",
    )
    state.forum_page = _page([_post(1, "入口证据", "root evidence", replies=[reply])])
    state.selected_post = 1
    content = view_mod.forum_content(state, 72)
    joined = "\n".join(content)
    assert "> [+] 帖子 #1  入口证据" in joined
    assert "作者：user · user · 10:00:00 · 1 保留回复" in joined
    assert state.agent_label("user") == "用户"
    assert state.agent_label("coordinator") == "协调器"
    assert state.agent_label("system") == "系统"
    assert "摘录：root evidence" in joined
    assert "── 最新回复 · 守望者 ──" in joined
    assert "─" * 72 in joined
    assert view_mod.forum_title(state) == "论坛 1/1页 · 1帖 · 最新在前"


def test_forum_expanded_post_renders_replies() -> None:
    state = UiState()
    reply = MessageView(
        id=5,
        agent_id="audit-1",
        agent_name="守望者",
        stage="audit",
        reply_to=3,
        content="reply body",
        created_at="2026-09-13T10:02:00Z",
    )
    post = _post(3, "阶段交接", "root body", replies=[reply])
    state.forum_page = _page([post])
    state.expanded_posts = {3}
    state.selected_post = 3
    joined = "\n".join(view_mod.forum_content(state, 72))
    assert "> [-] 帖子 #3  阶段交接" in joined
    assert "范围：当前保留正文与回复，并非完整历史。" in joined
    assert "root body" in joined
    assert "── 回复 #5 → #3 ──" in joined
    assert "作者：守望者 · 10:02:00" in joined
    assert "reply body" in joined


def test_forum_pinned_and_missing_root() -> None:
    state = UiState()
    state.forum_page = _page([_post(4, "置顶帖", "pinned body", pinned=True)])
    joined = "\n".join(view_mod.forum_content(state, 72))
    assert "[置顶] 置顶帖" in joined

    state = UiState()
    missing = PostView(id=9, root_missing=True, updated_at="2026-09-13T10:00:00Z")
    state.forum_page = _page([missing])
    joined = "\n".join(view_mod.forum_content(state, 72))
    assert "帖子 #9  原帖不在当前保留窗口" in joined
    assert "摘录：" not in joined


def test_forum_search_banner_and_excerpt_truncation() -> None:
    state = UiState()
    state.forum_search = "xss"
    state.forum_page = _page([_post(1, "t", "A" * 700)], total=1)
    joined = "\n".join(view_mod.forum_content(state, 72))
    assert "搜索：xss · /search 清除" in joined
    assert " …" in joined
    assert "…（显示截断）" not in joined


def test_forum_paging_and_selection() -> None:
    state = UiState()
    state.forum_page = _page(
        [_post(2, "newer", "b"), _post(1, "older", "a")], total=2
    )
    state.forum_revision += 1
    state.selected_post = 2
    view_mod.select_forum_post(state, 1)
    assert state.selected_post == 1
    view_mod.select_forum_post(state, -5)
    assert state.selected_post == 2


# ---------------------------------------------------------------------------
# Text pipeline


def test_bounded_text_counts_utf8_bytes() -> None:
    assert bounded_text("short", 100) == "short"
    value = bounded_text("界" * 600, 1000)
    assert value.endswith(" …（显示截断）")
    assert len(value.encode("utf-8")) <= 1000 + len(" …（显示截断）".encode("utf-8"))
    assert bounded_text("界界界", 4) == "界 …（显示截断）"


def test_fit_and_wrap_use_cells() -> None:
    assert fit_line("abcdef", 3) == "abc"
    assert fit_line("a\nb", 3) == "a b"
    assert fit_line("界界界", 5) == "界界"
    assert wrap_lines("界界界", 4) == ["界界", "界"]
    assert wrap_lines("", 10) == [""]
    assert wrap_lines("a\nb", 10) == ["a", "b"]
    assert display_width("界") == 2
    assert display_width("·") == 1
    assert display_width("─") == 1


def test_forum_excerpt_limits_rows() -> None:
    lines = forum_excerpt("word " * 200, 30, 2)
    assert len(lines) == 2
    assert lines[1].endswith(" …")


def test_paint_classification_colours() -> None:
    assert classify_line("[high] row")[0] == DANGER
    assert classify_line("[failed] row")[0] == DANGER
    assert classify_line("! bad")[0] == DANGER
    assert classify_line("[medium] row")[0] == WARNING
    assert classify_line("保留缺口：x")[0] == WARNING
    assert classify_line("[completed] row")[0] == SUCCESS
    assert classify_line("[low] row")[0] == SUCCESS
    assert classify_line("[running] row")[0] == FOCUS
    assert classify_line("── 最近操作 ──")[0] == FOCUS
    assert classify_line("─" * 10)[0] == FRAME
    assert classify_line("作者：someone")[0] == MUTED
    assert classify_line("位置 · a:1")[0] == MUTED
    fg, bg, bold, selected = classify_line("> [high] row")
    assert selected is True and bg == SELECTED_BG and fg == DANGER and bold is True
    fg, bg, bold, selected = classify_line("> plain row")
    assert selected is True and bg == SELECTED_BG and bold is True
    assert classify_line("plain text") == (None, None, False, False)
    text = paint_line("> plain row")
    assert str(text) == "> plain row"


# ---------------------------------------------------------------------------
# Model helpers


def test_duration_and_clock_formatting() -> None:
    assert format_duration(0) == "0s"
    assert format_duration(1.4) == "1s"
    assert format_duration(61) == "1m1s"
    assert format_duration(3661) == "1h1m1s"
    assert format_duration(3600) == "1h0m0s"
    assert format_duration(7200) == "2h0m0s"
    assert format_duration(90) == "1m30s"
    stamp = parse_timestamp("2026-09-13T11:22:33.123456789Z")
    assert stamp is not None and stamp.strftime("%H:%M:%S") == "11:22:33"
    assert view_mod.format_clock("not-a-date") == "not-a-date"


def test_group_posts_orders_and_groups() -> None:
    messages = [
        norm_message(make_message(1, "root", topic="t1", created_at="2026-09-13T10:00:00Z")),
        norm_message(
            make_message(2, "reply", reply_to=1, created_at="2026-09-13T10:05:00Z")
        ),
        norm_message(make_message(3, "newer", topic="t2", created_at="2026-09-13T10:01:00Z")),
    ]
    posts = view_mod.group_posts(messages)
    assert [post.id for post in posts] == [1, 3]
    assert posts[0].replies and posts[0].replies[0].id == 2
    assert view_mod.group_posts(
        [norm_message(make_message(5, "pinned", topic="p", pinned=True))]
    )[0].root.pinned is True


def test_norm_helpers_accept_mappings() -> None:
    budget = norm_budget(
        {"infinite_mode": True, "hours": 1, "minutes": 2, "token_limit": 3, "elapsed": 4.5}
    )
    assert (budget.infinite_mode, budget.hours, budget.minutes, budget.token_limit) == (
        True,
        1,
        2,
        3,
    )
    page = norm_page(
        {
            "posts": [
                {
                    "id": 1,
                    "root": {"id": 1, "content": "body", "topic": "topic"},
                    "replies": [],
                }
            ],
            "page": 2,
            "total_posts": 1,
            "total_pages": 3,
        }
    )
    assert page.page == 2 and page.total_pages == 3
    assert page.posts[0].root.content == "body"


# ---------------------------------------------------------------------------
# Findings / modal geometry


def test_findings_modal_lines_and_overlay() -> None:
    state = UiState()
    items = [norm_snapshot(make_snapshot(findings=[make_finding(index + 1)])).findings[0]
             for index in range(3)]
    findings_mod.open_findings(state, items, 0)
    modal_mod.wrap_modal(state, 92, 19)
    findings_mod.update_findings(state, "down", 19)
    body = findings_mod.findings_body(state, 92)
    assert any(line.startswith("> #2 [high]") for line in body)
    assert any(line.startswith("  #1 [high]") for line in body)
    assert any("位置 · src/app.py:10" in line for line in body)
    lines = view_mod.snapshot(state, "")
    assert len(lines) == 40
    joined = "\n".join(lines)
    assert "漏洞列表 · 3 个" in joined
    assert "本次快照" in joined
    assert "↑↓ 选择 · gg / End 末项 · Enter 详情 · Esc 关闭" in joined
    # Box is 96 wide, centered at column 12.
    title_index = next(i for i, line in enumerate(lines) if "漏洞列表 · 3 个" in line)
    top = lines[title_index - 1]
    assert display_width(top[: top.index("╭")]) == 12
    assert display_width(top[: top.index("╮") + 1]) == 12 + 96
    assert display_width(top) == 120


def test_modal_size_clamps_to_terminal() -> None:
    state = UiState(width=120, height=40)
    assert modal_mod.modal_size(state) == (96, 24, 19)
    state = UiState(width=70, height=25)
    assert modal_mod.modal_size(state) == (66, 23, 18)
    state = UiState(width=30, height=10)
    assert modal_mod.modal_size(state) == (26, 8, 3)


def test_finding_detail_body_sections() -> None:
    state = UiState()
    item = norm_snapshot(make_snapshot(findings=[make_finding(7, title="XSS")])).findings[0]
    findings_mod.open_findings(state, [item], 0)
    title, lines = findings_mod.finding_detail_lines(state, 0)
    assert title == "漏洞详情 · 1 / 1 · #7"
    for section in ("── 概览 ──", "── 证据 ──", "── 影响 ──", "── 修复建议 ──"):
        assert section in lines
    assert "[high]  CWE-79" in lines
    assert "位置 · src/app.py:10" in lines
