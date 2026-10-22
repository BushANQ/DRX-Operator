"""Budget form modal: normal versus infinite mode with three numeric limits."""

from __future__ import annotations

from drx_agent.code_audit.tui.model import (
    BudgetForm,
    BudgetView,
    ModalState,
    UiState,
    format_duration,
)
from drx_agent.code_audit.tui.theme import fit_line, wrap_lines

LABELS = ("运行模式", "小时", "分钟", "Token 上限")


def new_budget_form(budget: BudgetView) -> BudgetForm:
    return BudgetForm(
        infinite=budget.infinite_mode,
        selected=0,
        fields=[str(budget.hours), str(budget.minutes), str(budget.token_limit)],
    )


def open_budget_prompt(state: UiState) -> None:
    form = new_budget_form(state.budget)
    state.modal = ModalState(kind="budget", title="审计预算 · /budget", budget=True, budget_form=form)


def budget_lines(
    state: UiState, width: int, height: int
) -> tuple[list[tuple[str, str]], tuple[int, int] | None]:
    """Return painted budget body rows plus the active field cursor position."""
    modal = state.modal
    form = modal.budget_form if modal is not None else None
    if form is None:
        return [], None
    width = max(1, width)
    rows: list[tuple[str, str]] = [
        (fit_line("设置整个团队的运行上限", width), "muted"),
        ("", "body"),
    ]
    starts: list[int] = []
    mode = "普通  [无限审计]" if form.infinite else "[普通]  无限审计"
    note = (
        "无限审计：不自主结束，仍受预算约束"
        if form.infinite
        else "普通：允许共识结束，不启用预算限额"
    )
    cursor: tuple[int, int] | None = None
    for index, label in enumerate(LABELS):
        starts.append(len(rows))
        marker = "> " if form.selected == index else "  "
        rows.append((fit_line(marker + label, width), "body"))
        if index == 0:
            rows.append(("  " + fit_line(mode, max(1, width - 2)), "accent"))
        else:
            value = form.fields[index - 1]
            rows.append(("  " + fit_line(value, max(1, width - 2)), "plain"))
            if form.selected == index:
                cursor = (len(rows) - 1, min(len(value) + 2, max(1, width - 1)))
        rows.append(("", "body"))
    notes = [
        note,
        "0 = 不限 · 小时与分钟相加",
        f"已用 {format_duration(state.budget_cfg.elapsed)} · "
        f"{state.budget_cfg.used_tokens} token（保存后继续累计）",
    ]
    for text in notes:
        for line in wrap_lines(text, width):
            rows.append((line, "muted"))
    start = 0
    if starts[form.selected] + 2 > height:
        start = starts[form.selected] + 2 - height
    visible = rows[start : start + max(0, height)]
    while len(visible) < height:
        visible.append(("", "body"))
    if cursor is not None:
        row, col = cursor
        row -= start
        if row < 0 or row >= height:
            cursor = None
        else:
            cursor = (row, col)
    return visible, cursor


def update_budget(state: UiState, key: str, char: str = "") -> str | None:
    """Handle one key press in the budget form."""
    modal = state.modal
    if modal is None or modal.budget_form is None:
        return None
    form = modal.budget_form
    if key in ("escape", "ctrl+["):
        return "cancel"
    if key == "ctrl+c":
        return "ctrl_c"
    if key in ("tab", "down", "shift+tab", "up"):
        step = -1 if key in ("shift+tab", "up") else 1
        form.selected = (form.selected + step + 4) % 4
        modal.err = ""
        return "changed"
    if key == "ctrl+s":
        try:
            hours = int(form.fields[0].strip())
            minutes = int(form.fields[1].strip())
            tokens = int(form.fields[2].strip())
            if hours < 0 or minutes < 0 or tokens < 0:
                raise ValueError
        except ValueError:
            modal.err = "请输入非负整数；0 表示不限"
            return "changed"
        return "save"
    if key in ("left", "right", "space", "enter") and form.selected == 0:
        form.infinite = not form.infinite
        modal.err = ""
        return "changed"
    if form.selected > 0:
        field = form.selected - 1
        if key == "backspace":
            form.fields[field] = form.fields[field][:-1]
            modal.err = ""
            return "changed"
        if key in ("left", "right", "home", "end", "up", "down", "tab", "shift+tab"):
            return None
        text = char or (key if len(key) == 1 and key.isprintable() else "")
        if text:
            form.fields[field] = (form.fields[field] + text)[:20]
            modal.err = ""
            return "changed"
    return None


def budget_body(
    state: UiState, width: int, height: int
) -> tuple[list[tuple[str, str]], tuple[int, int] | None]:
    return budget_lines(state, width, height)
