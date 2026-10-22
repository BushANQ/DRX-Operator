"""Findings list and finding detail modal behaviour."""

from __future__ import annotations

from drx_agent.code_audit.tui.model import FindingView, ModalState, UiState
from drx_agent.code_audit.tui.theme import fit_line, wrap_lines


def open_findings(state: UiState, items: list[FindingView], selected: int) -> None:
    items = list(items)
    if items:
        selected = min(max(0, selected), len(items) - 1)
    else:
        selected = 0
    state.modal = ModalState(
        kind="findings",
        title=f"漏洞列表 · {len(items)} 个",
        findings=None,
    )
    from drx_agent.code_audit.tui.model import FindingsModal

    state.modal.findings = FindingsModal(items=items, selected=selected)


def wrap_findings(state: UiState, width: int) -> None:
    modal = state.modal
    assert modal is not None and modal.findings is not None
    data = modal.findings
    if data.list_width != width:
        data.list_wrapped = []
        data.starts = []
        indent = min(2, max(0, width - 1))
        prefix = " " * indent
        for item in data.items:
            data.starts.append(len(data.list_wrapped))
            texts = [
                f"#{item.id} [{item.severity}] "
                + fit_line(item.title, max(1, width - 18)),
                "位置 · "
                + fit_line(f"{item.path}:{item.line}", max(1, width - indent - 7)),
            ]
            for text in texts:
                for line in wrap_lines(text, max(1, width - indent)):
                    data.list_wrapped.append(prefix + line)
            data.list_wrapped.append(prefix + "─" * max(1, width - indent))
            data.list_wrapped.append("")
        if not data.items:
            data.list_wrapped = wrap_lines(
                "尚无漏洞记录\n\n审计进行中。重新 /list 可刷新结果。", width
            )
        data.list_width = width
        mark_finding(state, data.selected, True)
    modal.wrapped = data.list_wrapped
    modal.wrap_width = width


def mark_finding(state: UiState, index: int, selected: bool) -> None:
    modal = state.modal
    if modal is None or modal.findings is None:
        return
    data = modal.findings
    if index < 0 or index >= len(data.starts) or data.list_width <= 1:
        return
    row = data.starts[index]
    if not 0 <= row < len(data.list_wrapped):
        return
    marker = ">" if selected else " "
    line = data.list_wrapped[row]
    data.list_wrapped[row] = marker + (line[1:] if line else "")


def keep_finding_visible(state: UiState, height: int) -> None:
    modal = state.modal
    if modal is None or modal.findings is None:
        return
    data = modal.findings
    if not data.starts:
        modal.scroll = 0
        return
    selected = min(max(0, data.selected), len(data.starts) - 1)
    start = data.starts[selected]
    end = len(data.list_wrapped)
    if selected + 1 < len(data.starts):
        end = data.starts[selected + 1]
    if start < modal.scroll or end - start > height:
        modal.scroll = start
    elif end > modal.scroll + height:
        modal.scroll = end - height
    modal.scroll = min(max(0, modal.scroll), max(0, len(data.list_wrapped) - height))


def findings_body(state: UiState, width: int) -> list[str]:
    modal = state.modal
    if modal is None or modal.findings is None:
        return []
    wrap_findings(state, width)
    return list(modal.findings.list_wrapped)


def finding_detail_lines(state: UiState, index: int) -> tuple[str, list[str]]:
    modal = state.modal
    if modal is None or modal.findings is None:
        return "", []
    data = modal.findings
    if not data.items:
        return modal.title, []
    index = min(max(0, index), len(data.items) - 1)
    item = data.items[index]
    title = f"漏洞详情 · {index + 1} / {len(data.items)} · #{item.id}"
    lines = [
        "── 概览 ──",
        item.title,
        "",
        f"[{item.severity}]  {item.cwe}",
        f"位置 · {item.path}:{item.line}",
        "",
        "── 证据 ──",
        "",
        item.evidence,
        "",
        "── 影响 ──",
        "",
        item.impact,
        "",
        "── 修复建议 ──",
        "",
        item.recommendation,
    ]
    return title, lines


def update_findings(state: UiState, key: str, height: int) -> str | None:
    """Handle one key in the findings list; returns an action marker if any."""
    modal = state.modal
    if modal is None or modal.findings is None:
        return None
    data = modal.findings
    if not data.items:
        return None
    selected = data.selected
    changed = False
    if key == "up":
        selected -= 1
    elif key == "down":
        selected += 1
    elif key == "home":
        selected = 0
    elif key == "end":
        selected = len(data.items) - 1
    elif key == "pageup":
        target = data.starts[selected] - height
        while selected > 0 and data.starts[selected] > target:
            selected -= 1
    elif key == "pagedown":
        target = data.starts[selected] + height
        while selected < len(data.items) - 1 and data.starts[selected] < target:
            selected += 1
    elif key == "wheel_up":
        selected -= 1
    elif key == "wheel_down":
        selected += 1
    elif key == "enter":
        data.list_scroll = modal.scroll
        data.detail = True
        title, lines = finding_detail_lines(state, selected)
        modal.title = title
        modal.lines = lines
        modal.wrap_width = 0
        modal.scroll = 0
        return "detail"
    else:
        return None

    selected = min(max(0, selected), len(data.items) - 1)
    if selected != data.selected:
        mark_finding(state, data.selected, False)
        data.selected = selected
        mark_finding(state, selected, True)
        keep_finding_visible(state, height)
        changed = True
    return "changed" if changed else None


def return_to_findings(state: UiState) -> None:
    modal = state.modal
    if modal is None or modal.findings is None:
        return
    data = modal.findings
    data.detail = False
    modal.title = f"漏洞列表 · {len(data.items)} 个"
    modal.lines = []
    modal.scroll = data.list_scroll
    modal.wrapped = data.list_wrapped
    modal.wrap_width = data.list_width
