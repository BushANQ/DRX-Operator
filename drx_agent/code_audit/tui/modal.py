"""Modal overlay rendering and modal key dispatch."""

from __future__ import annotations

from drx_agent.code_audit.text import sanitize_text
from drx_agent.code_audit.tui import agents as agents_mod
from drx_agent.code_audit.tui import budget as budget_mod
from drx_agent.code_audit.tui import findings as findings_mod
from drx_agent.code_audit.tui.model import (
    MAX_BROADCAST_BYTES,
    ModalState,
    SessionsModal,
    UiState,
)
from drx_agent.code_audit.tui.theme import (
    display_width,
    fit_line,
    render_row,
    safe_text,
    wrap_lines,
)
from drx_agent.code_audit.tui.view import viewport

DEFAULT_FOOTER = "↑↓ 滚动 · PgUp/PgDn 翻页 · gg / End 到底 · Esc 关闭"


def modal_size(state: UiState) -> tuple[int, int, int]:
    width = min(96, max(4, state.width - 4))
    height = min(24, max(4, state.height - 2))
    return width, height, max(1, height - 5)


def modal_cells(text: str, start: int, width: int) -> str:
    """Slice terminal cells, replacing wide glyphs cut by an edge with spaces."""
    if width <= 0:
        return ""
    out: list[str] = []
    position = 0
    used = 0
    for char in text:
        size = display_width(char)
        end = position + size
        if position >= start + width:
            break
        if end > start:
            if position < start or end > start + width:
                count = min(end, start + width) - max(position, start)
                out.append(" " * count)
                used += count
            else:
                out.append(char)
                used += size
        position = end
    out.append(" " * max(0, width - used))
    return "".join(out)


# ---------------------------------------------------------------------------
# Body construction


def wrap_sessions(state: UiState, inner: int, body_height: int) -> None:
    modal = state.modal
    if modal is None or modal.sessions is None:
        return
    data = modal.sessions
    modal.wrapped = []
    data.starts = []
    for index, item in enumerate(data.items):
        data.starts.append(len(modal.wrapped))
        marker = "> " if index == data.selected else "  "
        modal.wrapped.extend(wrap_lines(marker + item, inner))
    if not data.items:
        modal.wrapped = wrap_lines("没有已保存的会话", inner)
    else:
        data.selected = min(max(0, data.selected), len(data.items) - 1)
        row = data.starts[data.selected]
        if row < modal.scroll or row >= modal.scroll + body_height:
            modal.scroll = row
    modal.wrap_width = inner


def wrap_modal(state: UiState, inner: int, body_height: int) -> None:
    modal = state.modal
    if modal is None:
        return
    if modal.kind == "agents":
        agents_mod.wrap_agents(state, inner)
        return
    if modal.kind == "findings" and modal.findings is not None and not modal.findings.detail:
        findings_mod.wrap_findings(state, inner)
        return
    if modal.kind == "sessions":
        wrap_sessions(state, inner, body_height)
        return
    if modal.kind in ("budget", "compose"):
        return
    if modal.wrap_width == inner:
        return
    modal.wrapped = []
    for line in modal.lines:
        modal.wrapped.extend(wrap_lines(line, inner))
    modal.wrap_width = inner


def _compose_body(state: UiState, inner: int, body_height: int) -> list[str]:
    modal = state.modal
    assert modal is not None
    logical = modal.compose_text.split("\n")
    lines: list[str] = []
    for chunk in logical:
        lines.extend(wrap_lines(chunk, max(1, inner)))
    start = max(0, len(lines) - body_height)
    visible = lines[start : start + body_height]
    visible.extend([""] * (body_height - len(visible)))
    return visible


def modal_body(
    state: UiState, inner: int, body_height: int
) -> tuple[list[list[tuple[str, str]]], tuple[int, int] | None]:
    modal = state.modal
    if modal is None:
        return [], None
    body_height = max(1, body_height)
    inner = max(1, inner)
    if modal.kind == "budget":
        rows, cursor = budget_mod.budget_lines(state, inner, body_height)
        return [[(text, kind)] for text, kind in rows], cursor
    if modal.kind == "compose":
        visible = _compose_body(state, inner, body_height)
        cursor_row = body_height - 1
        return [[(text, "plain")] for text in visible], (
            cursor_row,
            display_width(visible[cursor_row]),
        )
    if modal.kind == "findings" and modal.findings is not None and not modal.findings.detail:
        findings_mod.wrap_findings(state, inner)
        lines = viewport(modal.findings.list_wrapped, modal.scroll, body_height, False)
        return [[(line, "body")] for line in lines], None
    if modal.kind == "agents":
        agents_mod.wrap_agents(state, inner)
        lines = viewport(modal.wrapped, modal.scroll, body_height, False)
        return [[(line, "body")] for line in lines], None
    if modal.kind == "sessions":
        wrap_sessions(state, inner, body_height)
        lines = viewport(modal.wrapped, modal.scroll, body_height, False)
        return [[(line, "body")] for line in lines], None
    wrap_modal(state, inner, body_height)
    lines = viewport(modal.wrapped, modal.scroll, body_height, False)
    return [[(line, "body")] for line in lines], None


def modal_status(state: UiState, inner: int, body_height: int) -> tuple[str, str]:
    modal = state.modal
    if modal is None:
        return "", "muted"
    if modal.budget:
        if modal.budget_form is not None and modal.budget_form.infinite:
            status = "时间与 token 任一用尽即停；修改预算不重置已用量"
        else:
            status = "普通模式不限额；下列预算仅在无限审计模式生效"
    elif modal.compose:
        status = f"{len(modal.compose_text.encode('utf-8'))} / {MAX_BROADCAST_BYTES} bytes"
    elif modal.findings is not None and not modal.findings.detail:
        data = modal.findings
        selected = min(len(data.items), data.selected + 1)
        status = f"选中 {selected} / {len(data.items)} · 本次快照"
    else:
        total = len(modal.wrapped)
        status = (
            f"行 {min(total, modal.scroll + 1)}–"
            f"{min(total, modal.scroll + body_height)} / {total}"
        )
    if modal.err:
        return "! " + fit_line(modal.err, max(1, inner - 2)), "body"
    return fit_line(status, inner), "muted"


def modal_footer(state: UiState, inner: int) -> str:
    modal = state.modal
    if modal is None:
        return ""
    footer = DEFAULT_FOOTER
    if modal.budget:
        footer = "Tab 切换 · ←→ 模式 · Ctrl+S 确认 · Esc 取消"
    elif modal.compose:
        footer = "Ctrl+S 发送 · Enter 换行 · Esc 取消"
    elif modal.kind == "findings" and modal.findings is not None:
        if modal.findings.detail:
            footer = "↑↓ 滚动 · PgUp/PgDn 翻页 · gg / End 到底 · Esc 返回列表"
        else:
            footer = "↑↓ 选择 · gg / End 末项 · Enter 详情 · Esc 关闭"
    elif modal.kind == "agents":
        if modal.agent_details:
            footer = "↑↓ 滚动 · gg / End 到底 · Esc 返回列表"
        else:
            footer = "↑↓ 选择 · gg / End 末项 · Enter 诊断 · Esc 关闭"
    if inner < 60:
        if modal.budget:
            footer = "Tab切换 · ^S保存 · Esc取消"
        elif modal.compose:
            footer = "^S发送 · Esc取消"
        elif (
            (modal.kind == "agents" and not modal.agent_details)
            or (modal.kind == "findings" and not modal.findings.detail)
            or modal.kind == "sessions"
        ):
            footer = "↑↓选 · Enter打开 · Esc关闭"
        else:
            footer = "↑↓滚动 · gg到底 · Esc返回"
    return fit_line(footer, inner)


# ---------------------------------------------------------------------------
# Box rendering


def modal_box_rows(state: UiState) -> tuple[list[list[tuple[str, str]]], tuple[int, int] | None]:
    width, height, body_height = modal_size(state)
    inner = width - 4
    modal = state.modal
    if modal is None:
        return [], None
    body, cursor = modal_body(state, inner, body_height)
    status_text, status_kind = modal_status(state, inner, body_height)
    footer = modal_footer(state, inner)
    rows: list[list[tuple[str, str]]] = [
        [("╭" + "─" * (width - 2) + "╮", "border_frame")]
    ]
    for row in range(1, height - 1):
        if row == 1:
            content = fit_line(modal.title, inner)
            kind = "accent"
            segments: list[tuple[str, str]] = [(content, kind)]
        elif row == height - 3:
            segments = [(fit_line(status_text, inner), status_kind)]
        elif row == height - 2:
            segments = [(footer, "muted")]
        else:
            segments = body[row - 2] if row - 2 < len(body) else [("", "body")]
        text = "".join(part for part, _kind in segments)
        text = fit_line(text, inner)
        padding = " " * max(0, inner - display_width(text))
        rows.append(
            [
                ("│ ", "border_frame"),
                (text, segments[0][1] if len(segments) == 1 else "body"),
                (padding + " │", "border_frame"),
            ]
        )
    rows.append([("╰" + "─" * (width - 2) + "╯", "border_frame")])
    # Preserve per-segment styling for body rows (single-segment rows above).
    for index in range(1, height - 1):
        row_index = index - 2
        if 2 <= index <= height - 4 and row_index < len(body):
            body_row = body[row_index]
            text = "".join(part for part, _kind in body_row)
            text = fit_line(text, inner)
            padding = " " * max(0, inner - display_width(text))
            rows[index] = (
                [("│ ", "border_frame")]
                + [(part, kind) for part, kind in body_row]
                + [(padding + " │", "border_frame")]
            )
    return rows, cursor


def overlay_modal(
    state: UiState, base_rows: list[list[tuple[str, str]]]
) -> list[list[tuple[str, str]]]:
    if state.modal is None:
        return base_rows
    if state.width < 20 or state.height < 8:
        return _tiny_modal_rows(state)
    width, height, _body = modal_size(state)
    box, _cursor = modal_box_rows(state)
    x = (state.width - width) // 2
    y = (state.height - height) // 2
    background = [list(row) for row in base_rows]
    while len(background) < state.height:
        background.append([("", "plain")])
    background = background[: state.height]
    for offset, row in enumerate(box):
        target = y + offset
        if target < 0 or target >= state.height:
            continue
        base = "".join(text for text, _kind in background[target])
        left = modal_cells(base, 0, x)
        right = modal_cells(base, x + width, state.width - x - width)
        segments: list[tuple[str, str]] = []
        if left:
            segments.append((left, "muted"))
        segments.extend(row)
        if right:
            segments.append((right, "muted"))
        background[target] = segments
    return background


def _tiny_modal_rows(state: UiState) -> list[list[tuple[str, str]]]:
    modal = state.modal
    assert modal is not None
    dismiss = "请扩大终端；Esc 关闭"
    if modal.kind == "findings" and modal.findings is not None and modal.findings.detail:
        dismiss = "请扩大终端；Esc 返回列表"
    rows: list[list[tuple[str, str]]] = [
        [(fit_line(modal.title, state.width), "accent")],
        [(fit_line(dismiss, state.width), "muted")],
    ]
    if modal.compose:
        rows.append([(fit_line("Ctrl+S 发送 / Esc 取消", state.width), "muted")])
    while len(rows) < max(1, state.height):
        rows.append([("", "plain")])
    return rows[: max(1, state.height)]



def modal_lines_for_render(state: UiState) -> list[str]:
    width, _height, _body = modal_size(state)
    rows, _cursor = modal_box_rows(state)
    return ["".join(text for text, _kind in row) for row in rows]


# ---------------------------------------------------------------------------
# Open / close / keys


def open_detail(state: UiState, title: str, lines: list[str]) -> None:
    state.modal = ModalState(kind="detail", title=title, lines=list(lines))
    wrap_modal(state, max(1, modal_size(state)[0] - 4), modal_size(state)[2])


def close_modal(state: UiState) -> None:
    state.modal = None


def open_sessions(state: UiState, items: list[str]) -> None:
    state.modal = ModalState(
        kind="sessions",
        title="已保存会话 · ↑↓ 选择 Enter 恢复",
        sessions=SessionsModal(items=list(items)),
    )


def update_sessions(state: UiState, key: str, body_height: int) -> str | None:
    modal = state.modal
    if modal is None or modal.sessions is None:
        return None
    data = modal.sessions
    if not data.items:
        return None
    changed = False
    if key == "up":
        data.selected -= 1
        changed = True
    elif key == "down":
        data.selected += 1
        changed = True
    elif key == "pageup":
        data.selected -= body_height
        changed = True
    elif key == "pagedown":
        data.selected += body_height
        changed = True
    elif key == "home":
        data.selected = 0
        changed = True
    elif key == "end":
        data.selected = len(data.items) - 1
        changed = True
    elif key == "wheel_up":
        data.selected -= 1
        changed = True
    elif key == "wheel_down":
        data.selected += 1
        changed = True
    elif key == "enter":
        data.selected = min(max(0, data.selected), len(data.items) - 1)
        return "restore"
    else:
        return None
    data.selected = min(max(0, data.selected), len(data.items) - 1)
    modal.wrap_width = 0
    wrap_sessions(state, max(1, modal_size(state)[0] - 4), body_height)
    return "changed" if changed else None


def update_compose(state: UiState, key: str, char: str = "") -> str | None:
    modal = state.modal
    if modal is None or not modal.compose:
        return None
    if key == "backspace":
        modal.compose_text = modal.compose_text[:-1]
        modal.err = ""
        return "changed"
    if key == "enter":
        candidate = sanitize_text(modal.compose_text + "\n")
    elif char:
        candidate = sanitize_text(modal.compose_text + char)
    else:
        return None
    if len(candidate.encode("utf-8")) > MAX_BROADCAST_BYTES:
        modal.err = "输入超过16 KiB"
        return "changed"
    modal.compose_text = candidate
    modal.err = ""
    return "changed"


def handle_modal_key(state: UiState, key: str, char: str = "") -> str | None:
    """Return an action marker for the application to perform."""
    modal = state.modal
    if modal is None:
        return None
    if key in ("escape", "ctrl+["):
        if modal.kind == "agents" and modal.agent_details:
            agents_mod.return_to_agents(state)
            return "list"
        if modal.kind == "findings" and modal.findings is not None and modal.findings.detail:
            findings_mod.return_to_findings(state)
            return "list"
        state.pending_dir = ""
        close_modal(state)
        return "close"
    if key == "ctrl+c":
        return "ctrl_c"
    if modal.budget:
        return budget_mod.update_budget(state, key, char)
    if modal.compose:
        if key == "ctrl+s":
            text = sanitize_text(modal.compose_text)
            if not text.strip() or len(text.encode("utf-8")) > MAX_BROADCAST_BYTES:
                modal.err = "消息必须非空且不超过16 KiB"
                return "changed"
            return "send"
        return update_compose(state, key, char)
    _width, _height, body_height = modal_size(state)
    inner = max(1, modal_size(state)[0] - 4)
    if modal.kind == "agents":
        return agents_mod.update_agents(state, key, body_height)
    if modal.kind == "sessions":
        return update_sessions(state, key, body_height)
    if modal.kind == "findings" and modal.findings is not None and not modal.findings.detail:
        return findings_mod.update_findings(state, key, body_height)
    wrap_modal(state, inner, body_height)
    delta = 0
    if key == "up":
        delta = -1
    elif key == "down":
        delta = 1
    elif key == "pageup":
        delta = -body_height
    elif key == "pagedown":
        delta = body_height
    elif key == "wheel_up":
        delta = -3
    elif key == "wheel_down":
        delta = 3
    elif key == "home":
        modal.scroll = 0
        return "changed"
    elif key == "end":
        modal.scroll = max(0, len(modal.wrapped) - body_height)
        return "changed"
    if delta:
        modal.scroll = min(
            max(0, modal.scroll + delta), max(0, len(modal.wrapped) - body_height)
        )
        return "changed"
    return None


def modal_cursor(state: UiState) -> tuple[int, int] | None:
    _width, _height, _body = modal_size(state)
    _rows, cursor = modal_box_rows(state)
    return cursor


__all__ = [
    "DEFAULT_FOOTER",
    "close_modal",
    "handle_modal_key",
    "modal_cells",
    "modal_cursor",
    "modal_footer",
    "modal_lines_for_render",
    "modal_size",
    "modal_status",
    "open_detail",
    "open_sessions",
    "overlay_modal",
    "render_row",
    "safe_text",
    "update_compose",
    "update_sessions",
    "wrap_modal",
]
