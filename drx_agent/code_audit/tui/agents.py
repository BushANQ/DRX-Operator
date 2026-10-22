"""Agent roster modal: compact list plus one-worker diagnostics."""

from __future__ import annotations

from drx_agent.code_audit.tui.model import AgentsModal, ModalState, UiState, WorkerView
from drx_agent.code_audit.tui.theme import bounded_text, fit_line, wrap_lines
from drx_agent.code_audit.tui.view import generation_label


def agent_name(worker: WorkerView) -> str:
    if worker.name:
        return worker.name
    return "未命名（" + worker.id + "）"


def agent_value(value: str) -> str:
    return value or "暂无记录"


def agent_summary(text: str, width: int) -> str:
    flat = " ".join((text or "").split())
    flat = flat.replace("\n", " ")
    if len(flat) <= width:
        return flat
    if width <= 1:
        return "…"
    return flat[: width - 1] + "…"


def agent_detail_lines(state: UiState, modal: ModalState) -> list[str]:
    lines = [f"当前阶段：{state.phase} · Agent 数：{len(state.workers)}", ""]
    modal.agent_state.line_starts = []
    data = modal.agent_state
    if not state.workers:
        return lines + ["当前没有已创建的 Agent。"]
    if not modal.agent_details:
        for index, worker in enumerate(state.workers):
            data.line_starts.append(len(lines))
            marker = "  "
            if index == modal.agent_index:
                marker = "> "
            activity = agent_summary(worker.activity, 64) or "暂无活动"
            if worker.generation is not None:
                activity = generation_label(worker.generation) + " · " + activity
            lines.append(
                f"{marker}[{agent_summary(worker.status, 24)}] "
                f"{agent_summary(agent_name(worker), 36)} · #{worker.turn}"
            )
            lines.append("  " + activity)
            lines.append("")
        return lines
    if modal.agent_index < 0 or modal.agent_index >= len(state.workers):
        return lines + ["该 Agent 已不在当前团队中。"]
    worker = state.workers[modal.agent_index]
    lines += [
        "── 状态 ──",
        "名称：" + agent_name(worker),
        "ID：" + worker.id,
        f"[{worker.status}] 阶段 {worker.phase} · 回合 #{worker.turn}",
        "活动：" + agent_value(worker.activity),
        "",
        "── 当前请求输出 ──",
    ]
    progress = worker.generation
    if progress is not None:
        lines.append(generation_label(progress) + f" · reasoning {progress.reasoning_tokens} tok")
        if progress.estimated:
            lines.append(f"tool≈{progress.tool_tokens} tok · UTF-8 字节估算")
        else:
            lines.append("提供方用量；不单独提供工具 token 数。")
        lines.append("以上为当前请求输出，非上下文 token 数。")
        if not progress.received_at:
            lines.append("当前请求等待首段输出；尚无输出数据时间。")
        else:
            lines.append("当前请求最后输出数据：" + progress.received_at)
    else:
        lines.append("当前没有请求输出计数；不表示上下文为空。")
    lines += [
        "",
        "── 活动时钟 ──",
        "最近模型输出：" + agent_value(worker.last_model_activity),
        "最近工具：" + agent_value(worker.last_tool),
        "最近工具时间：" + agent_value(worker.last_tool_activity),
        "",
        "── 最近输出摘录 ──",
    ]
    if worker.output_partial:
        lines.append("[partial] 未完成的输出摘录，不代表已执行工具或完整结论。")
    lines.append(agent_value(worker.output_excerpt))
    for index, line in enumerate(lines):
        lines[index] = line.replace("\x1b", "")
    return lines


def refresh_agents_modal(state: UiState) -> None:
    modal = state.modal
    if modal is None or modal.kind != "agents":
        return
    if modal.agent_state is None:
        modal.agent_state = AgentsModal()
    data = modal.agent_state
    found = False
    for index, worker in enumerate(state.workers):
        if worker.id == data.selected_id:
            modal.agent_index = index
            found = True
            break
    if not found:
        if modal.agent_details:
            modal.agent_details = False
            modal.scroll = data.list_scroll
        limit = max(0, len(state.workers) - 1)
        modal.agent_index = min(max(0, modal.agent_index), limit)
    data.selected_id = state.workers[modal.agent_index].id if state.workers else ""
    modal.title = f"Agent 列表 · {len(state.workers)} 个"
    if modal.agent_details:
        name = agent_name(state.workers[modal.agent_index]) if state.workers else ""
        modal.title = "Agent 详情 · " + agent_summary(name, 48)
    modal.lines = agent_detail_lines(state, modal)
    modal.wrap_width = 0
    wrap_agents(state, 0)


def wrap_agents(state: UiState, width: int) -> None:
    modal = state.modal
    if modal is None:
        return
    if width <= 0:
        modal.wrapped = list(modal.lines)
        return
    if modal.wrap_width == width:
        return
    modal.wrapped = []
    modal.agent_starts = []
    starts = modal.agent_state.line_starts if modal.agent_state else []
    next_start = 0
    for index, line in enumerate(modal.lines):
        if (
            not modal.agent_details
            and next_start < len(starts)
            and index == starts[next_start]
        ):
            modal.agent_starts.append(len(modal.wrapped))
            next_start += 1
        modal.wrapped.extend(wrap_lines(line, width))
    modal.wrap_width = width


def keep_agent_visible(state: UiState, height: int) -> None:
    modal = state.modal
    if modal is None or modal.agent_details:
        return
    if modal.agent_index < 0 or modal.agent_index >= len(modal.agent_starts):
        modal.scroll = 0
        return
    start = modal.agent_starts[modal.agent_index]
    end = len(modal.wrapped)
    if modal.agent_index + 1 < len(modal.agent_starts):
        end = modal.agent_starts[modal.agent_index + 1]
    if end > start and end - 1 < len(modal.wrapped) and modal.wrapped[end - 1] == "":
        end -= 1
    if start < modal.scroll or end - start > height:
        modal.scroll = start
    elif end > modal.scroll + height:
        modal.scroll = end - height
    modal.scroll = min(max(0, modal.scroll), max(0, len(modal.wrapped) - height))


def update_agents(state: UiState, key: str, height: int) -> str | None:
    modal = state.modal
    if modal is None:
        return None
    if modal.agent_details:
        delta = 0
        if key == "up":
            delta = -1
        elif key == "down":
            delta = 1
        elif key == "pageup":
            delta = -height
        elif key == "pagedown":
            delta = height
        elif key == "home":
            modal.scroll = 0
            return "changed"
        elif key == "end":
            modal.scroll = len(modal.wrapped)
            return "changed"
        elif key == "wheel_up":
            delta = -3
        elif key == "wheel_down":
            delta = 3
        else:
            return None
        modal.scroll = min(
            max(0, modal.scroll + delta), max(0, len(modal.wrapped) - height)
        )
        return "changed"
    if not state.workers or len(modal.agent_starts) != len(state.workers):
        return None
    selected = modal.agent_index
    if key == "up":
        selected -= 1
    elif key == "down":
        selected += 1
    elif key == "home":
        selected = 0
    elif key == "end":
        selected = len(state.workers) - 1
    elif key == "pageup":
        target = modal.agent_starts[selected] - height
        while selected > 0 and modal.agent_starts[selected] > target:
            selected -= 1
    elif key == "pagedown":
        target = modal.agent_starts[selected] + height
        while selected < len(state.workers) - 1 and modal.agent_starts[selected] < target:
            selected += 1
    elif key == "wheel_up":
        selected -= 1
    elif key == "wheel_down":
        selected += 1
    elif key == "enter":
        if 0 <= selected < len(state.workers):
            if modal.agent_state is None:
                modal.agent_state = AgentsModal()
            modal.agent_state.list_scroll = modal.scroll
            modal.agent_details = True
            modal.scroll = 0
            refresh_agents_modal(state)
            return "detail"
        return None
    else:
        return None
    selected = min(max(0, selected), len(state.workers) - 1)
    if selected != modal.agent_index:
        modal.agent_index = selected
        if modal.agent_state is None:
            modal.agent_state = AgentsModal()
        modal.agent_state.selected_id = state.workers[selected].id
        refresh_agents_modal(state)
        return "changed"
    return None


def return_to_agents(state: UiState) -> None:
    modal = state.modal
    if modal is None:
        return
    modal.agent_details = False
    if modal.agent_state is not None:
        modal.scroll = modal.agent_state.list_scroll
    refresh_agents_modal(state)


def open_agents(state: UiState) -> None:
    state.modal = ModalState(kind="agents", agent_state=AgentsModal(), agent_index=0)
    refresh_agents_modal(state)


def agents_body(state: UiState, width: int) -> list[str]:
    modal = state.modal
    if modal is None:
        return []
    wrap_agents(state, width)
    return list(modal.wrapped)


def click_agents_row(state: UiState, x: int, y: int) -> None:
    modal = state.modal
    if modal is None or modal.agent_details:
        return
    width, height, body = modal_size_for_click(state)
    if state.width < 20 or state.height < 8:
        return
    left, top = (state.width - width) // 2, (state.height - height) // 2
    if x < left + 2 or x >= left + width - 2:
        return
    if y < top + 2 or y >= top + 2 + body:
        return
    row = modal.scroll + y - top - 2
    if not modal.agent_starts or row < modal.agent_starts[0] or row >= len(modal.wrapped):
        return
    selected = 0
    for index, start in enumerate(modal.agent_starts):
        if start > row:
            break
        selected = index
    if selected != modal.agent_index:
        modal.agent_index = selected
        if modal.agent_state is None:
            modal.agent_state = AgentsModal()
        modal.agent_state.selected_id = state.workers[selected].id
        refresh_agents_modal(state)


def modal_size_for_click(state: UiState) -> tuple[int, int, int]:
    width = min(96, max(4, state.width - 4))
    height = min(24, max(4, state.height - 2))
    return width, height, max(1, height - 5)


__all__ = [
    "agent_detail_lines",
    "agents_body",
    "click_agents_row",
    "open_agents",
    "refresh_agents_modal",
    "return_to_agents",
    "update_agents",
    "wrap_agents",
]
