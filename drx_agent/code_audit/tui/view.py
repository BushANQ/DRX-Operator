"""Line-accurate rendering of the audit screen and forum navigation helpers."""

from __future__ import annotations

import math
from typing import Any, Iterable

from drx_agent.code_audit.tui.model import (
    DEFAULT_PAGE_SIZE,
    PLACEHOLDER,
    ForumHeader,
    Layout,
    MessageView,
    PageView,
    PostView,
    UiState,
    compute_layout,
    format_clock,
    format_datetime,
    get_field,
    format_duration,
    norm_message,
    norm_page,
    norm_snapshot,
    norm_worker,
    parse_timestamp,
    phase_label,
    status_label,
)
from drx_agent.code_audit.tui.theme import (
    bounded_text,
    display_width,
    fit_line,
    forum_excerpt,
    wrap_lines,
)

LINE_ACCENT = "accent"
LINE_MUTED = "muted"
LINE_BODY = "body"
LINE_BORDER_FRAME = "border_frame"
LINE_BORDER_FOCUS = "border_focus"

LEFT_TITLE = "团队进度 / 操作"


# ---------------------------------------------------------------------------
# Generic helpers


def viewport(lines: list[str], start: int, height: int, follow: bool) -> list[str]:
    height = max(0, height)
    if follow:
        start = max(0, len(lines) - height)
    start = min(max(0, start), max(0, len(lines) - height))
    end = min(len(lines), start + height)
    out = list(lines[start:end])
    out.extend([""] * (height - len(out)))
    return out


def generation_label(progress: Any) -> str:
    if progress is None:
        return ""
    if progress.estimated:
        return f"generation≈{progress.output_tokens} tok"
    return f"generation {progress.output_tokens} tok"


def reviewed_file_count(state: UiState) -> int:
    return sum(1 for item in state.snapshot.files if item.status in ("reviewed", "skipped"))


def done_todo_count(state: UiState) -> int:
    return sum(1 for item in state.snapshot.todos if item.status in ("done", "completed"))


def summary_line(state: UiState) -> str:
    reviewed = sum(
        1 for item in state.snapshot.files if item.status in ("reviewed", "skipped")
    )
    return (
        f"覆盖 {reviewed}/{len(state.snapshot.files)} · "
        f"Todo {done_todo_count(state)}/{len(state.snapshot.todos)} · "
        f"漏洞 {len(state.snapshot.findings)}"
    )


# ---------------------------------------------------------------------------
# Header / help / input


def header_line(state: UiState) -> str:
    return fit_line(
        f"代码审计 · {state.header_state()} · {state.workspace}", state.width
    )


def narrow_header_line(state: UiState) -> str:
    return fit_line(f"审计 · {state.header_state()} · Tab: 状态/论坛", state.width)


WIDE_HELP = (
    "Tab 面板 · ↑↓选帖 Enter详情 ←→翻页 · Ctrl+G×2 到底 · 点击标题 · "
    "PgUp/PgDn/滚轮 · Esc暂停 · /help"
)
NARROW_HELP = "↑↓选帖 Enter详情 · Ctrl+G×2 到底 · Tab · Esc暂停 · /help"


def help_line(state: UiState) -> str:
    narrow = state.layout().narrow
    return fit_line(NARROW_HELP if narrow else WIDE_HELP, state.width)


TINY_NOTICE = "请扩大终端；Esc 停止"


def is_tiny(state: UiState) -> bool:
    return state.width < 20 or state.height < 7


# ---------------------------------------------------------------------------
# Left pane content


def left_content(state: UiState, width: int, height: int) -> tuple[list[str], list[str]]:
    pinned: list[str] = []
    lines: list[str] = []
    budget = state.budget
    reviewed = reviewed_file_count(state)
    pinned.append(
        fit_line(
            f"覆盖 {reviewed}/{len(state.snapshot.files)} · "
            f"Todo {len(state.snapshot.todos)} · 漏洞 {len(state.snapshot.findings)}",
            width,
        )
    )
    mode, time_limit, token_limit = "普通", "不限", "不限"
    if budget.infinite_mode:
        mode = "无限"
        if budget.hours > 0 or budget.minutes > 0:
            time_limit = f"{budget.hours}h{budget.minutes}m"
        if budget.token_limit > 0:
            token_limit = str(budget.token_limit)
    pinned.append(
        fit_line(f"{mode} · 用时 {format_duration(budget.elapsed)} / {time_limit}", width)
    )
    estimate = "≈" if budget.estimated else ""
    pinned.append(fit_line(f"tokens {estimate}{budget.used_tokens} / {token_limit}", width))
    if budget.stop_reason:
        pinned.extend(wrap_lines(budget.stop_reason, width))

    max_pinned = max(0, (height - 6 - len(pinned)) // 2)
    for index, worker in enumerate(state.workers):
        activity = worker.activity
        generation = generation_label(worker.generation)
        if worker.generation is not None and not worker.generation.received_at:
            activity = "等待首段输出 · " + activity
        elif worker.last_model_activity:
            activity += " · 输出 " + format_clock(worker.last_model_activity)
        row = (
            f"[{status_label(worker.status)}] "
            f"{state.agent_label(worker.id, worker.name)} · #{worker.turn}"
        )
        detail = generation
        if detail:
            detail += " · "
        detail += bounded_text(activity, 220)
        if index < max_pinned:
            pinned.append(fit_line(row, width))
            pinned.append(fit_line("  " + detail, width))
        else:
            lines.extend(wrap_lines(row, width))
            lines.extend(wrap_lines("  " + detail, width))
    if not state.workers:
        pinned.append("团队尚未启动")
    if len(state.workers) > max_pinned:
        pinned.append(
            fit_line(f"提示：另 {len(state.workers) - max_pinned} 位在下方 · /agents", width)
        )
    pinned.append("─" * max(1, width))
    lines.append("── 最近操作 ──")
    for entry in state.events:
        text = entry.at + " " + (
            state.agent_label(entry.agent_id) + " " + entry.content
        ).strip()
        lines.extend(wrap_lines(text, width))
    return pinned, lines


# ---------------------------------------------------------------------------
# Forum content


def _post_topic(post: PostView) -> str:
    topic = post.root.topic or "讨论"
    if post.root_missing:
        return "原帖不在当前保留窗口"
    return topic


def group_posts(messages: Iterable[MessageView]) -> list[PostView]:
    groups: dict[int, int] = {}
    parents: dict[int, int] = {}
    posts: list[PostView] = []
    stamps: list[datetime | None] = []
    for message in messages:
        thread = message.thread_id
        if not thread:
            thread = message.id
            if message.reply_to > 0:
                thread = parents.get(message.reply_to, 0) or message.reply_to
        parents[message.id] = thread
        if thread not in groups:
            groups[thread] = len(posts)
            posts.append(PostView(id=thread, root_missing=True))
            stamps.append(None)
        index = groups[thread]
        post = posts[index]
        if message.id == thread:
            post.root = message
            post.root_missing = False
        else:
            post.replies.append(message)
        stamp = parse_timestamp(message.created_at)
        current = stamps[index]
        newer = stamp is not None and (
            current is None or stamp > current or (stamp == current and message.id > post.last_id)
        )
        if newer:
            stamps[index] = stamp
            post.updated_at = stamp
            post.last_id = message.id
        if message.pinned:
            post.root.pinned = True
        if message.closed:
            post.root.closed = True

    def sort_key(post: PostView) -> tuple:
        stamp = parse_timestamp(post.updated_at)
        return (
            0 if post.root.pinned else 1,
            -(stamp.timestamp() if stamp else 0.0),
            -post.last_id,
        )

    posts.sort(key=sort_key)
    return posts


def forum_content(state: UiState, width: int) -> list[str]:
    cache = state.forum_cache
    if (
        cache.width == width
        and cache.revision == state.forum_revision
        and cache.thread_id == state.thread_id
    ):
        return cache.lines
    lines: list[str] = []
    headers: list[ForumHeader] = []
    page = state.forum_page
    if page.gap:
        lines.extend(
            wrap_lines(
                "保留缺口：较早消息已过期，正文与回复仅含当前 Board 保留内容。", width
            )
        )
    if state.forum_search and state.thread_id == 0:
        lines.extend(wrap_lines("搜索：" + state.forum_search + " · /search 清除", width))
    for post in page.posts:
        expanded = post.id in state.expanded_posts
        topic = _post_topic(post)
        marker = ">"
        if post.id != state.selected_post:
            marker = " "
        state_marker = "[-]" if expanded else "[+]"
        if post.root.pinned:
            topic = "[置顶] " + topic
        if post.root.closed:
            topic = "[关闭] " + topic
        start = len(lines)
        lines.extend(
            wrap_lines(f"{marker} {state_marker} 帖子 #{post.id}  {topic}", width)
        )
        headers.append(ForumHeader(id=post.id, start=start, end=len(lines)))
        lines.extend(
            wrap_lines(
                "作者："
                + state.agent_label(post.root.agent_id, post.root.agent_name)
                + " · "
                + phase_label(post.root.stage)
                + " · "
                + format_clock(post.updated_at)
                + f" · {len(post.replies)} 保留回复",
                width,
            )
        )
        if expanded:
            if post.root_missing:
                note = "保留缺口：原帖已过期；以下为当前保留回复。"
            else:
                note = "范围：当前保留正文与回复，并非完整历史。"
            lines.extend(wrap_lines(note, width))
        if not post.root_missing:
            if expanded:
                lines.append("")
                lines.extend(wrap_lines(post.root.content, width))
            else:
                lines.extend(forum_excerpt("摘录：" + post.root.content, width, 2))
        if expanded:
            for reply in post.replies:
                lines.append("")
                lines.extend(
                    wrap_lines(f"── 回复 #{reply.id} → #{reply.reply_to} ──", width)
                )
                lines.extend(
                    wrap_lines(
                        "作者："
                        + state.agent_label(reply.agent_id, reply.agent_name)
                        + " · "
                        + format_clock(reply.created_at),
                        width,
                    )
                )
                lines.extend(wrap_lines(reply.content, width))
        elif post.replies:
            reply = post.replies[-1]
            lines.append("")
            lines.extend(
                wrap_lines(
                    "── 最新回复 · "
                    + state.agent_label(reply.agent_id, reply.agent_name)
                    + " ──",
                    width,
                )
            )
            lines.extend(forum_excerpt("摘录：" + reply.content, width, 2))
        if headers:
            headers[-1].body_end = len(lines)
        lines.append("─" * max(1, width))
        lines.append("")
    if not page.posts:
        lines.extend(
            wrap_lines("当前页没有帖子；/search 清除搜索，/say 发帖，/forum 返回列表。", width)
        )
    state.forum_cache = type(cache)(
        width=width,
        revision=state.forum_revision,
        thread_id=state.thread_id,
        lines=lines,
        headers=headers,
    )
    return lines


def forum_title(state: UiState) -> str:
    if state.thread_id:
        return f"帖子 #{state.thread_id} · /forum 返回"
    mode = "最新在前" if state.follow[1] else "历史"
    page = state.forum_page
    return (
        f"论坛 {max(1, page.page)}/{max(1, page.total_pages)}页 · "
        f"{page.total_posts}帖 · {mode}"
    )


# ---------------------------------------------------------------------------
# Pane rendering


def render_pane(
    title: str, body: list[str], width: int, height: int, focused: bool
) -> list[tuple[str, str]]:
    width = max(4, width)
    height = max(3, height)
    inner = max(1, width - 4)
    border_kind = LINE_BORDER_FOCUS if focused else LINE_BORDER_FRAME
    rows: list[tuple[str, str]] = [(("╭" + "─" * (width - 2) + "╮"), border_kind)]
    content_rows = height - 2
    for index in range(content_rows):
        if index == 0:
            text = title
        elif index - 1 < len(body):
            text = body[index - 1]
        else:
            text = ""
        text = fit_line(text, inner)
        padding = " " * max(0, inner - display_width(text))
        line = "│" + " " + text + padding + " " + "│"
        rows.append((line, LINE_ACCENT if index == 0 else LINE_BODY))
    rows.append((("╰" + "─" * (width - 2) + "╯"), border_kind))
    return rows


def left_pane_lines(state: UiState, width: int, height: int) -> list[tuple[str, str]]:
    inner = max(1, width - 4)
    pinned, lines = left_content(state, inner, height)
    available = max(0, height - 3 - len(pinned))
    content = pinned + viewport(lines, state.scroll[0], available, state.follow[0])
    return render_pane(LEFT_TITLE, content, width, height, state.focus == 0)


def forum_pane_lines(state: UiState, width: int, height: int) -> list[tuple[str, str]]:
    inner = max(1, width - 4)
    lines = forum_content(state, inner)
    start = 0 if state.follow[1] else state.scroll[1]
    content = viewport(lines, start, max(1, height - 3), False)
    return render_pane(forum_title(state), content, width, height, state.focus == 1)


def frame_rows(state: UiState, input_value: str) -> list[list[tuple[str, str]]]:
    """Full frame without modal overlay: header, panes, help, command input.

    Each row is a list of ``(text, style)`` segments so overlays can splice
    content into the middle of a row while keeping the outside muted.
    """
    if is_tiny(state):
        display = input_value or PLACEHOLDER
        rows: list[list[tuple[str, str]]] = [
            [(header_line(state), LINE_ACCENT)],
            [(fit_line(TINY_NOTICE, state.width), LINE_MUTED)],
            [(fit_line("> " + display, state.width), "plain")],
        ]
        if len(rows) > max(1, state.height):
            rows = rows[: max(1, state.height)]
        return rows
    layout = state.layout()
    header = header_line(state)
    rows = [[(header, LINE_ACCENT)]]
    if layout.narrow:
        if state.focus == 0:
            pane = left_pane_lines(state, layout.left, layout.height)
        else:
            pane = forum_pane_lines(state, layout.right, layout.height)
        rows[0] = [(narrow_header_line(state), LINE_ACCENT)]
        rows.extend([[(text, kind)] for text, kind in pane])
    else:
        left = left_pane_lines(state, layout.left, layout.height)
        forum = forum_pane_lines(state, layout.right, layout.height)
        for index in range(layout.height):
            rows.append([(left[index][0] + " " + forum[index][0], left[index][1])])
    rows.append([(help_line(state), LINE_MUTED)])
    rows.append([("> " + (input_value or PLACEHOLDER), "plain")])
    return rows


def row_text(row: list[tuple[str, str]]) -> str:
    return "".join(text for text, _kind in row)


def rows_text(rows: list[list[tuple[str, str]]]) -> list[str]:
    return [row_text(row) for row in rows]


def screen_rows(state: UiState, input_value: str) -> list[list[tuple[str, str]]]:
    rows = frame_rows(state, input_value)
    if state.modal is not None:
        from drx_agent.code_audit.tui import modal as modal_mod

        rows = modal_mod.overlay_modal(state, rows)
    return rows


def screen_lines(state: UiState, input_value: str) -> list[tuple[str, str]]:
    """Single-segment rows for callers that do not need overlay composition."""
    return [(row_text(row), row[0][1] if row else LINE_BODY) for row in screen_rows(state, input_value)]


def snapshot(state: UiState, input_value: str) -> list[str]:
    """Plain-text screen lines (used by tests and the snapshot exporter)."""
    return rows_text(screen_rows(state, input_value))



# ---------------------------------------------------------------------------
# Forum navigation (state mutations; engine access is passed in)


def sync_team(state: UiState, team: Any) -> None:
    """Pull immutable engine state into the interface."""
    try:
        state.budget = state.budget
        budget = team.budget_status()
        from drx_agent.code_audit.tui.model import norm_budget

        state.budget = norm_budget(budget)
    except Exception:
        pass
    try:
        state.snapshot = norm_snapshot(team.snapshot())
    except Exception:
        pass
    try:
        state.workers = [norm_worker(item) for item in (team.statuses() or [])]
    except Exception:
        pass
    try:
        phase = team.phase()
        if isinstance(phase, str) and phase:
            state.phase = phase
    except Exception:
        pass
    refresh_forum_page(state, team)


def refresh_budget(state: UiState, team: Any) -> None:
    try:
        from drx_agent.code_audit.tui.model import norm_budget

        state.budget = norm_budget(team.budget_status())
    except Exception:
        pass


def refresh_forum_page(state: UiState, team: Any) -> None:
    anchor_id, anchor_offset = 0, 0
    if not state.follow[1] and state.forum_cache.headers:
        layout = state.layout()
        lines = forum_content(state, max(1, layout.right - 4))
        start = min(
            max(0, state.scroll[1]), max(0, len(lines) - max(1, layout.height - 3))
        )
        for header in state.forum_cache.headers:
            if header.start > start:
                break
            anchor_id, anchor_offset = header.id, start - header.start
    if state.thread_id == 0:
        page = team.forum_posts(
            max(1, state.forum_page.page), DEFAULT_PAGE_SIZE, state.forum_search
        )
        state.forum_page = norm_page(page)
    else:
        messages = [norm_message(item) for item in (team.forum_messages() or [])]
        page = PageView(page=1, page_size=DEFAULT_PAGE_SIZE, total_pages=1)
        page.gap = bool(messages) and messages[0].id > 1
        for post in group_posts(messages):
            if post.id == state.thread_id:
                page.posts = [post]
                page.total_posts = 1
                break
        state.forum_page = page
    state.forum_dirty = False
    reconcile_forum(state)
    if anchor_id:
        layout = state.layout()
        forum_content(state, max(1, layout.right - 4))
        for header in state.forum_cache.headers:
            if header.id == anchor_id:
                state.scroll[1] = header.start + min(
                    anchor_offset, max(0, header.body_end - header.start - 1)
                )
                break
        _lines, limit = scroll_content(state, 1)
        state.scroll[1] = min(state.scroll[1], limit)


def reconcile_forum(state: UiState) -> None:
    retained = {post.id for post in state.forum_page.posts}
    state.expanded_posts = {pid for pid in state.expanded_posts if pid in retained}
    if state.selected_post == 0 and state.forum_page.posts:
        state.selected_post = state.forum_page.posts[0].id
    state.forum_revision += 1


def scroll_content(state: UiState, pane: int) -> tuple[list[str], int]:
    layout = state.layout()
    available = max(1, layout.height - 3)
    if pane == 0:
        pinned, lines = left_content(state, max(1, layout.left - 4), layout.height)
        available = max(1, available - len(pinned))
        return lines, max(0, len(lines) - available)
    lines = forum_content(state, max(1, layout.right - 4))
    return lines, max(0, len(lines) - available)


def move_scroll(state: UiState, delta: int) -> None:
    _lines, limit = scroll_content(state, state.focus)
    position = state.scroll[state.focus]
    if state.follow[state.focus]:
        position = limit if state.focus == 0 else 0
    state.scroll[state.focus] = min(limit, max(0, position + delta))
    if state.focus == 1:
        state.follow[1] = state.scroll[1] == 0
    else:
        state.follow[0] = delta > 0 and state.scroll[0] >= limit


def reveal_selected_post(state: UiState) -> None:
    layout = state.layout()
    lines = forum_content(state, max(1, layout.right - 4))
    available = max(1, layout.height - 3)
    start = min(state.scroll[1], max(0, len(lines) - available))
    if state.follow[1]:
        start = 0
    for header in state.forum_cache.headers:
        if header.id != state.selected_post:
            continue
        end = min(header.body_end, header.end + 3)
        if (
            header.id in state.expanded_posts
            or header.start < start
            or end - header.start >= available
        ):
            start = header.start
        elif end > start + available:
            start = end - available
        state.scroll[1] = min(start, max(0, len(lines) - available))
        state.follow[1] = False
        return


def select_forum_post(state: UiState, delta: int) -> None:
    posts = state.forum_page.posts
    if not posts:
        return
    index = -1
    for position, post in enumerate(posts):
        if post.id == state.selected_post:
            index = position
            break
    if index >= 0 and state.selected_post in state.expanded_posts:
        layout = state.layout()
        lines = forum_content(state, max(1, layout.right - 4))
        available = max(1, layout.height - 3)
        start = min(state.scroll[1], max(0, len(lines) - available))
        if state.follow[1]:
            start = 0
        for header in state.forum_cache.headers:
            if header.id == state.selected_post and (
                (delta > 0 and header.body_end > start + available)
                or (delta < 0 and header.start < start)
            ):
                move_scroll(state, delta * 3)
                return
    if index < 0:
        index = 0
    else:
        index = min(len(posts) - 1, max(0, index + delta))
        if posts[index].id == state.selected_post:
            move_scroll(state, delta * 3)
            return
    state.selected_post = posts[index].id
    state.forum_revision += 1
    reveal_selected_post(state)


def show_forum_post_lines(state: UiState, post: PostView) -> tuple[str, list[str]]:
    topic = _post_topic(post)
    lines: list[str] = [
        "标题：" + topic,
        "",
        f"更新：{format_datetime(post.updated_at)} · {len(post.replies)} 保留回复",
        "范围：仅当前 Board 保留内容，不保证完整历史。",
        "",
    ]
    if post.root_missing:
        lines.append("保留缺口：原帖已过期，以下仅含当前保留回复。")
    else:
        lines.append(
            "作者："
            + state.agent_label(post.root.agent_id, post.root.agent_name)
            + " · "
            + phase_label(post.root.stage)
            + " · "
            + format_datetime(post.root.created_at)
        )
        lines.extend(["", "── 正文 ──", "", post.root.content])
    for reply in post.replies:
        lines.extend(
            [
                "",
                f"── 回复 #{reply.id} → #{reply.reply_to} ──",
                "作者："
                + state.agent_label(reply.agent_id, reply.agent_name)
                + " · "
                + phase_label(reply.stage)
                + " · "
                + format_datetime(reply.created_at),
                "",
                reply.content,
            ]
        )
    return f"论坛帖子 #{post.id} · {topic}", lines


def toggle_forum_post(
    state: UiState, post_id: int
) -> tuple[str, list[str]] | None:
    for post in state.forum_page.posts:
        if post.id != post_id:
            continue
        state.selected_post = post_id
        return show_forum_post_lines(state, post)
    state.add_event("所选帖子已移出当前页；↑↓ 重新选择，或 /thread <ID> 打开。")
    return None


def change_forum_page(state: UiState, delta: int, team: Any) -> None:
    if state.thread_id:
        return
    page = min(
        max(1, state.forum_page.total_pages), max(1, state.forum_page.page + delta)
    )
    if page == state.forum_page.page:
        return
    state.forum_page.page = page
    state.selected_post = 0
    state.scroll[1] = 0
    state.follow[1] = True
    refresh_forum_page(state, team)


def scroll_by_page(state: UiState, delta: int) -> None:
    layout = state.layout()
    move_scroll(state, delta * max(1, layout.height - 4))


def click_forum_row(state: UiState, x: int, y: int) -> int | None:
    """Return the post id whose header occupies screen row *y*, if any."""
    layout = state.layout()
    if state.width < 20 or state.height < 7:
        return None
    if layout.narrow and state.focus != 1:
        return None
    origin = 0 if layout.narrow else layout.left + 1
    if x < origin + 2 or x >= origin + layout.right - 2:
        return None
    if y < 3 or y >= layout.height:
        return None
    lines = forum_content(state, max(1, layout.right - 4))
    start = min(max(0, state.scroll[1]), max(0, len(lines) - max(1, layout.height - 3)))
    if state.follow[1]:
        start = 0
    row = start + y - 3
    for header in state.forum_cache.headers:
        if header.start <= row < header.end:
            state.focus = 1
            return header.id
    return None


def focus_from_pointer(state: UiState, x: int) -> None:
    layout = state.layout()
    if not layout.narrow:
        state.focus = 0 if x < layout.left else 1


def open_thread(state: UiState, post_id: int) -> PageView:
    """Build the single-post page for ``/thread`` from the current cache data."""
    page = PageView(page=1, page_size=DEFAULT_PAGE_SIZE, total_pages=1, total_posts=1)
    for post in state.forum_page.posts:
        if post.id == post_id:
            page.posts = [post]
            break
    return page


def layout_of(state: UiState) -> Layout:
    return compute_layout(state.width, state.height)
