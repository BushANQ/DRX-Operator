"""Textual application for the multi-agent code audit terminal interface."""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any

from rich.console import Group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.events import Click, Key, MouseScrollDown, MouseScrollUp, Resize
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from drx_agent.code_audit.text import sanitize_text
from drx_agent.code_audit.tui import agents as agents_mod
from drx_agent.code_audit.tui import budget as budget_mod
from drx_agent.code_audit.tui import commands as commands_mod
from drx_agent.code_audit.tui import findings as findings_mod
from drx_agent.code_audit.tui import modal as modal_mod
from drx_agent.code_audit.tui import view as view_mod
from drx_agent.code_audit.tui.model import (
    MAX_BROADCAST_BYTES,
    BudgetView,
    EventMailbox,
    ModalState,
    PageView,
    UiState,
    apply_event,
    norm_budget,
    norm_snapshot,
    session_filename,
)
from drx_agent.code_audit.tui.theme import DEFAULT_BG, render_row

PLACEHOLDER = "audit directory / go /budget /help"


def _cfg_agent(cfg: Any, name: str, default: Any = None) -> Any:
    agent = getattr(cfg, "agent", None)
    if agent is None:
        return default
    return getattr(agent, name, default)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class HeaderBar(Widget):
    """One-line application header."""

    can_focus = False

    def render(self) -> Text:
        state = self.app.ui
        line = (
            view_mod.narrow_header_line(state)
            if state.layout().narrow
            else view_mod.header_line(state)
        )
        return render_row([(line, "accent")])


class PaneCanvas(Widget):
    """Bordered pane drawn as an exact grid of cells."""

    can_focus = False

    def __init__(self, pane: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pane = pane

    def render(self) -> Any:
        state = self.app.ui
        width = max(4, self.size.width)
        height = max(3, self.size.height)
        if self.pane == "left":
            rows = view_mod.left_pane_lines(state, width, height)
        else:
            rows = view_mod.forum_pane_lines(state, width, height)
        group = []
        for row in rows:
            group.append(render_row([row]))
        return Group(*group)

    def on_click(self, event: Click) -> None:
        if self.pane == "forum":
            self.app.forum_click(event.screen_x, event.screen_y)


class HelpBar(Widget):
    can_focus = False

    def render(self) -> Text:
        return render_row([(view_mod.help_line(self.app.ui), "muted")])


class TinyCanvas(Widget):
    """Three-line fallback used when the terminal is too small."""

    can_focus = False

    def render(self) -> Any:
        state = self.app.ui
        rows = view_mod.frame_rows(state, self.app.input_value())
        return Group(*[render_row(row) for row in rows])


class CommandInput(Input):
    """Bottom command line; routes every key through the screen handler first."""

    async def _on_key(self, event: Key) -> None:
        if self.app.handle_main_key(event):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class ModalBox(Widget):
    """Fixed-size modal box painted cell by cell."""

    can_focus = True

    def render(self) -> Any:
        state = self.app.ui
        rows, cursor = modal_mod.modal_box_rows(state)
        group = []
        for index, row in enumerate(rows):
            text = render_row(row)
            if cursor is not None and index == cursor[0]:
                text.append(" ", style="reverse")
            group.append(text)
        return Group(*group)

    def _on_key(self, event: Key) -> None:
        if self.app.handle_modal_key(event):
            event.stop()
            event.prevent_default()

    def on_click(self, event: Click) -> None:
        self.app.modal_click(event.screen_x, event.screen_y)


class AuditModalScreen(ModalScreen[None]):
    """Overlay screen holding the single active modal box."""

    def compose(self) -> ComposeResult:
        yield ModalBox(id="modal-box")

    def on_mount(self) -> None:
        self.app.apply_modal_layout()
        self.query_one("#modal-box", ModalBox).focus()

    def on_resize(self, event: Resize) -> None:
        self.app.apply_modal_layout()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        if self.app.handle_modal_key_name("wheel_up"):
            event.stop()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        if self.app.handle_modal_key_name("wheel_down"):
            event.stop()


class CodeAuditApp(App):
    """Alternate-screen audit console.

    ``team`` is the audit engine (see ``drx_agent.code_audit.agent``); the app
    only calls its documented API.  ``notices`` become the first log lines.
    """

    CSS = f"""
    Screen {{
        background: {DEFAULT_BG};
        color: #d0d0d0;
        overflow: hidden;
    }}
    HeaderBar, HelpBar, TinyCanvas {{
        background: transparent;
        overflow: hidden;
    }}
    HeaderBar {{ height: 1; width: 100%; }}
    HelpBar {{ height: 1; width: 100%; }}
    #panes {{ height: 1fr; width: 100%; }}
    #left {{ width: 43; height: 100%; background: transparent; overflow: hidden; }}
    #gap {{ width: 1; height: 100%; background: transparent; }}
    #forum {{ width: 76; height: 100%; background: transparent; overflow: hidden; }}
    #command-row {{ height: 1; width: 100%; }}
    #prompt {{ width: 2; height: 1; color: #d0d0d0; background: transparent; }}
    #command {{
        width: 1fr;
        height: 1;
        background: transparent;
    }}
    #tiny {{ height: 100%; width: 100%; display: none; }}
    AuditModalScreen {{
        align: center middle;
        background: $background 60%;
    }}
    ModalBox {{
        width: 60;
        height: 10;
        background: transparent;
        overflow: hidden;
    }}
    """

    def __init__(
        self,
        team: Any,
        cfg: Any,
        auto_dir: str = "",
        notices: tuple[str, ...] | list[str] | None = (),
    ) -> None:
        super().__init__()
        self.team = team
        self.cfg = cfg
        budget_cfg = BudgetView(
            infinite_mode=bool(_cfg_agent(cfg, "infinite_mode", False)),
            hours=int(_cfg_agent(cfg, "budget_hours", 0) or 0),
            minutes=int(_cfg_agent(cfg, "budget_minutes", 0) or 0),
            token_limit=int(_cfg_agent(cfg, "budget_tokens", 0) or 0),
        )
        phase = "recon"
        try:
            phase = team.phase() or "recon"
        except Exception:
            pass
        self.ui = UiState(
            auto_dir=auto_dir or "",
            session_dir=str(_cfg_agent(cfg, "session_dir", "") or "sessions"),
            budget_cfg=budget_cfg,
            budget=budget_cfg,
            phase=phase,
        )
        self.notices = list(notices or ())
        self._mailbox: EventMailbox | None = None
        self._run_task: asyncio.Task | None = None
        self._drain_pending = False

    # -- lifecycle -------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Horizontal(id="panes"):
            yield PaneCanvas("left", id="left")
            yield Static(" ", id="gap")
            yield PaneCanvas("forum", id="forum")
        yield HelpBar(id="help")
        with Horizontal(id="command-row"):
            yield Static("> ", id="prompt")
            yield CommandInput(
                placeholder=PLACEHOLDER,
                id="command",
                max_length=4000,
                compact=True,
            )
        yield TinyCanvas(id="tiny")

    def on_mount(self) -> None:
        state = self.ui
        state.width, state.height = self.size.width, self.size.height
        startup_errors = self.sync_team_basics()
        if startup_errors:
            state.add_event("初始化审计团队失败：" + startup_errors[0])
        for notice in self.notices:
            state.add_event(notice)
        state.add_event("请输入要审计的目录；论坛只显示真实通信。")
        self.apply_layout()
        self.refresh_all()
        self.query_one("#command", CommandInput).focus()
        self.set_interval(0.25, self._tick)
        if state.auto_dir.strip():
            try:
                if state.budget_cfg.infinite_mode:
                    state.pending_dir = state.auto_dir
                    self.open_budget_prompt()
                else:
                    self.start_directory(state.auto_dir)
            except Exception as exc:  # pragma: no cover - defensive
                state.add_event("启动审计失败：" + str(exc))
                self.refresh_all()

    def on_unmount(self) -> None:
        cancel = getattr(self.team, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    def on_resize(self, event: Resize) -> None:
        state = self.ui
        state.width, state.height = event.size.width, event.size.height
        for pane in (0, 1):
            _lines, limit = view_mod.scroll_content(state, pane)
            state.scroll[pane] = min(state.scroll[pane], limit)
        self.apply_layout()
        self.refresh_all()

    def _tick(self) -> None:
        state = self.ui
        view_mod.refresh_budget(state, self.team)
        if state.busy or state.stopping or state.saving:
            self.refresh_all()

    # -- layout ----------------------------------------------------------

    def apply_layout(self) -> None:
        state = self.ui
        tiny = view_mod.is_tiny(state)
        for selector in ("#header", "#panes", "#help", "#command-row"):
            self.query_one(selector).display = not tiny
        self.query_one("#tiny").display = tiny
        if tiny:
            return
        layout = state.layout()
        self.query_one("#left").styles.width = layout.left
        self.query_one("#forum").styles.width = layout.right
        self.query_one("#gap").display = not layout.narrow
        if layout.narrow:
            self.query_one("#left").display = state.focus == 0
            self.query_one("#forum").display = state.focus == 1
        else:
            self.query_one("#left").display = True
            self.query_one("#forum").display = True

    def apply_modal_layout(self) -> None:
        if not isinstance(self.screen, AuditModalScreen):
            return
        width, height, _body = modal_mod.modal_size(self.ui)
        try:
            box = self.screen.query_one("#modal-box", ModalBox)
        except Exception:
            return
        box.styles.width = width
        box.styles.height = height

    def refresh_all(self) -> None:
        for selector in ("#header", "#panes", "#left", "#forum", "#help", "#tiny"):
            try:
                self.query_one(selector).refresh()
            except Exception:
                pass
        self.refresh_modal()

    def refresh_modal(self) -> None:
        if isinstance(self.screen, AuditModalScreen):
            try:
                self.screen.query_one("#modal-box", ModalBox).refresh()
            except Exception:
                pass

    # -- snapshot --------------------------------------------------------

    def input_value(self) -> str:
        try:
            return self.query_one("#command", CommandInput).value
        except Exception:
            return ""

    def set_input(self, value: str) -> None:
        widget = self.query_one("#command", CommandInput)
        widget.value = value
        try:
            widget.cursor_position = len(value)
        except Exception:
            pass

    def render_snapshot(self) -> list[str]:
        """Plain-text screen lines (for tests and layout comparison)."""
        return view_mod.snapshot(self.ui, self.input_value())

    def snapshot_text(self) -> str:
        return "\n".join(self.render_snapshot())

    # -- engine synchronisation -----------------------------------------

    def sync_team_basics(self) -> list[str]:
        state = self.ui
        errors: list[str] = []
        try:
            state.budget = norm_budget(self.team.budget_status())
        except Exception as exc:
            errors.append(str(exc))
        try:
            state.snapshot = norm_snapshot(self.team.snapshot())
        except Exception as exc:
            errors.append(str(exc))
        try:
            state.workers = [
                view_mod.norm_worker(item) for item in (self.team.statuses() or [])
            ]
        except Exception as exc:
            errors.append(str(exc))
        try:
            phase = self.team.phase()
            if isinstance(phase, str) and phase:
                state.phase = phase
        except Exception as exc:
            errors.append(str(exc))
        try:
            self.refresh_forum()
        except Exception as exc:
            errors.append(str(exc))
        return errors

    def sync_team(self) -> None:
        self.sync_team_basics()
        self.refresh_all()

    def refresh_forum(self) -> None:
        try:
            view_mod.refresh_forum_page(self.ui, self.team)
        except Exception:
            pass

    # -- operation log ---------------------------------------------------

    def add_log(self, text: str, agent_id: str = "") -> None:
        self.ui.add_event(text, agent_id)

    # -- run lifecycle ---------------------------------------------------

    def run_query(self, query: str) -> None:
        state = self.ui
        if state.busy or state.saving:
            state.add_event("请等待当前运行或保存完成后再开始。")
            self.refresh_all()
            return
        state.busy = True
        state.stopping = False
        state.session += 1
        state.last_query = query
        box = EventMailbox()
        box.notify = self._schedule_drain
        self._mailbox = box
        session = state.session
        state.add_event("启动团队审计；Esc 暂停，/say 打开广播草稿，Ctrl+S 发送。")
        self._run_task = asyncio.create_task(self._run_team(query, box, session))
        self.refresh_all()

    async def _run_team(self, query: str, box: EventMailbox, session: int) -> None:
        failure = ""
        try:
            await _maybe_await(self.team.run(query, box.post))
        except asyncio.CancelledError:
            failure = ""
        except Exception as exc:  # pragma: no cover - defensive
            failure = str(exc) or type(exc).__name__
        finally:
            box.finish(failure)
            asyncio.create_task(self._finish_run(session, failure))

    def _schedule_drain(self) -> None:
        if self._drain_pending:
            return
        self._drain_pending = True
        try:
            self.call_later(self._drain_events)
        except Exception:
            self._drain_pending = False

    def _drain_events_now(self) -> bool:
        box = self._mailbox
        if box is None:
            return False
        events, done, failure = box.drain()
        changed = False
        for event in events:
            apply_event(self.ui, event)
            changed = True
        if changed and self.ui.forum_dirty and not done:
            self.refresh_forum()
        return changed

    def _drain_events(self) -> None:
        self._drain_pending = False
        if self._drain_events_now():
            self.refresh_all()

    async def _finish_run(self, session: int, failure: str) -> None:
        self._drain_events_now()
        state = self.ui
        if session != state.session:
            return
        state.busy = False
        state.stopping = False
        self._mailbox = None
        self._ctx = None
        self.sync_team_basics()
        if failure:
            state.add_event("运行异常：" + failure)
        elif state.phase == "completed":
            state.add_event("全部审计 Agent 已完成；/report 查看摘要，/export 导出报告。")
        else:
            state.add_event("本轮已停止；输入 go 继续，或 /export 导出报告。")
        self.refresh_all()
        if state.saving:
            state.final_save_pending = True
            return
        await self.save_session("")
        if state.quitting and not state.busy:
            self.exit()

    def request_stop(self) -> None:
        state = self.ui
        if not state.busy:
            state.add_event("当前没有运行中的审计。")
            self.refresh_all()
            return
        if state.stopping:
            return
        state.stopping = True
        cancel = getattr(self.team, "cancel", None)
        stopped = False
        if callable(cancel):
            try:
                cancel()
                stopped = True
            except Exception:
                stopped = False
        if not stopped and self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        state.add_event("正在停止；等待所有 worker 退出后才能继续。")
        self.refresh_all()

    def ctrl_c(self) -> None:
        state = self.ui
        if state.busy:
            state.quitting = True
            self.request_stop()
            return
        if state.saving:
            state.quitting = True
            state.add_event("等待保存完成后退出。")
            self.refresh_all()
            return
        self.exit()

    def _handle_escape(self) -> None:
        state = self.ui
        if not state.busy or state.stopping:
            state.escape_armed = False
            return
        if not state.escape_armed:
            state.escape_armed = True
            state.add_event("再按一次 Esc 暂停审计；其他按键取消确认。")
            self.refresh_all()
            return
        state.escape_armed = False
        self.request_stop()

    # -- keyboard --------------------------------------------------------

    def handle_main_key(self, event: Key) -> bool:
        state = self.ui
        if state.modal is not None:
            return False
        key = event.key
        if key not in ("escape", "ctrl+["):
            state.escape_armed = False
        value = self.input_value()
        if not value:
            shortcut = self._bottom_shortcut(key, reading=False)
            if shortcut == "end":
                self._jump_bottom(0)
                return True
            if shortcut == "swallow":
                return True
        else:
            state.bottom_key = ""
        if key == "ctrl+c":
            self.ctrl_c()
            return True
        if key in ("escape", "ctrl+["):
            self._handle_escape()
            return True
        if key == "tab":
            self._handle_tab()
            return True
        if key in ("up", "down"):
            if value:
                return False
            delta = -1 if key == "up" else 1
            if state.focus == 1:
                view_mod.select_forum_post(state, delta)
            else:
                view_mod.move_scroll(state, delta)
            self.refresh_all()
            return True
        if key in ("left", "right"):
            if value or state.focus != 1:
                return False
            view_mod.change_forum_page(state, -1 if key == "left" else 1, self.team)
            self.refresh_all()
            return True
        if key in ("pageup", "pagedown"):
            view_mod.scroll_by_page(state, -1 if key == "pageup" else 1)
            self.refresh_all()
            return True
        if key in ("home", "end"):
            if value:
                return False
            if key == "home":
                state.scroll[state.focus] = 0
                state.follow[state.focus] = state.focus == 1
            else:
                self._jump_bottom(state.focus)
            self.refresh_all()
            return True
        if key == "enter":
            if not value and state.focus == 1:
                self._toggle_selected_post()
            else:
                text = value.strip()
                self.set_input("")
                if text:
                    asyncio.create_task(commands_mod.submit(self, text))
            self.refresh_all()
            return True
        return False

    def _jump_bottom(self, pane: int) -> None:
        state = self.ui
        if pane == 1:
            _lines, limit = view_mod.scroll_content(state, 1)
            state.scroll[1] = limit
            state.follow[1] = False
        else:
            state.follow[0] = True

    def _bottom_shortcut(self, key: str, reading: bool) -> str | None:
        state = self.ui
        name = key
        if name != "ctrl+g" and not (reading and name == "g"):
            state.bottom_key = ""
            return None
        now = asyncio.get_event_loop().time()
        if (
            state.bottom_key == name
            and state.bottom_modal is state.modal
            and state.bottom_focus == state.focus
            and now - state.bottom_at <= 0.8
        ):
            state.bottom_key = ""
            return "end"
        state.bottom_key = name
        state.bottom_at = now
        state.bottom_modal = state.modal
        state.bottom_focus = state.focus
        return "swallow"

    def _handle_tab(self) -> None:
        state = self.ui
        value = self.input_value()
        command, arg = commands_mod.parse_command(value)
        if command == "restore":
            query = commands_mod.clean_input_dir(arg)
            exact = commands_mod.resolve_session_path(state.session_dir, query)
            matches = commands_mod.session_matches(state.session_dir, query)
            if query and exact:
                matches = [exact]
            if len(matches) == 1:
                self.set_input('/restore "' + matches[0] + '"')
            elif len(matches) > 1:
                self.open_session_picker(matches)
            else:
                state.add_event("未找到会话：" + query)
            self.refresh_all()
            return
        state.focus = 1 - state.focus
        self.apply_layout()
        self.refresh_all()

    def _toggle_selected_post(self) -> None:
        state = self.ui
        post_id = state.selected_post
        if not post_id:
            return
        result = view_mod.toggle_forum_post(state, post_id)
        if result is not None:
            title, lines = result
            self.open_detail(title, lines)
        else:
            self.refresh_all()

    # -- modal keys ------------------------------------------------------

    def handle_modal_key(self, event: Key) -> bool:
        character = getattr(event, "character", "") or ""
        if not character.isprintable():
            character = ""
        return self.handle_modal_key_name(event.key, character)

    def handle_modal_key_name(self, key: str, char: str = "") -> bool:
        state = self.ui
        if state.modal is None:
            return False
        reading = not state.modal.compose
        if key in ("ctrl+g", "g") and reading:
            shortcut = self._bottom_shortcut(key, reading=True)
            if shortcut == "end":
                _width, _height, body = modal_mod.modal_size(state)
                state.modal.scroll = max(0, len(state.modal.wrapped) - body)
                self.refresh_modal()
                return True
            if shortcut == "swallow":
                return True
        else:
            state.bottom_key = ""
        action = modal_mod.handle_modal_key(state, key, char)
        self._apply_modal_action(action)
        return True

    def _apply_modal_action(self, action: str | None) -> None:
        state = self.ui
        if action is None:
            return
        if action == "close":
            self.close_modal_screen()
            state.modal = None
            self.refresh_all()
        elif action in ("list", "detail", "changed"):
            self.refresh_modal()
            self.refresh_all()
        elif action == "ctrl_c":
            self.ctrl_c()
        elif action == "cancel":
            state.pending_dir = ""
            self.close_modal_screen()
            state.modal = None
            self.refresh_all()
        elif action == "save":
            self._confirm_budget()
        elif action == "send":
            asyncio.create_task(self._send_broadcast())
        elif action == "restore":
            modal = state.modal
            items = modal.sessions.items if modal is not None and modal.sessions else []
            if items:
                selected = min(max(0, modal.sessions.selected), len(items) - 1)
                asyncio.create_task(self.restore_session(items[selected]))

    def _confirm_budget(self) -> None:
        state = self.ui
        modal = state.modal
        form = modal.budget_form if modal is not None else None
        if form is None:
            return
        try:
            hours = int(form.fields[0].strip())
            minutes = int(form.fields[1].strip())
            tokens = int(form.fields[2].strip())
            if hours < 0 or minutes < 0 or tokens < 0:
                raise ValueError
        except ValueError:
            modal.err = "请输入非负整数；0 表示不限"
            self.refresh_modal()
            return
        try:
            self.team.configure_budget(form.infinite, hours, minutes, tokens)
        except Exception as exc:  # pragma: no cover - defensive
            modal.err = str(exc)
            self.refresh_modal()
            return
        try:
            state.budget_cfg = norm_budget(self.team.budget_status())
        except Exception:
            pass
        directory = state.pending_dir
        state.pending_dir = ""
        self.close_modal_screen()
        state.modal = None
        self.refresh_all()
        if directory:
            self.start_directory(directory)

    async def _send_broadcast(self) -> None:
        state = self.ui
        modal = state.modal
        if modal is None or not modal.compose:
            return
        text = sanitize_text(modal.compose_text)
        if not text.strip() or len(text.encode("utf-8")) > MAX_BROADCAST_BYTES:
            modal.err = "消息必须非空且不超过16 KiB"
            self.refresh_modal()
            return
        try:
            await _maybe_await(self.team.post_message(text))
        except Exception as exc:  # pragma: no cover - defensive
            modal.err = str(exc)
            self.refresh_modal()
            return
        state.add_event(
            "用户广播了：" + text + "（完整正文将在各 Agent 下一轮优先读取）", "user"
        )
        self.refresh_forum()
        self.close_modal_screen()
        state.modal = None
        self.refresh_all()

    # -- mouse -----------------------------------------------------------

    def forum_click(self, x: int, y: int) -> None:
        state = self.ui
        if state.modal is not None:
            return
        if y >= state.height - 2:
            return
        view_mod.focus_from_pointer(state, x)
        post_id = view_mod.click_forum_row(state, x, y)
        if post_id is not None:
            result = view_mod.toggle_forum_post(state, post_id)
            if result is not None:
                title, lines = result
                self.open_detail(title, lines)
                return
        self.apply_layout()
        self.refresh_all()

    def modal_click(self, x: int, y: int) -> None:
        state = self.ui
        if state.modal is not None and state.modal.kind == "agents":
            agents_mod.click_agents_row(state, x, y)
            self.refresh_modal()
            self.refresh_all()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        if self.ui.modal is not None:
            self.handle_modal_key_name("wheel_up")
            return
        self._wheel(event, -3)

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        if self.ui.modal is not None:
            self.handle_modal_key_name("wheel_down")
            return
        self._wheel(event, 3)

    def _wheel(self, event: Any, delta: int) -> None:
        state = self.ui
        x = int(getattr(event, "screen_x", getattr(event, "x", 0)))
        y = int(getattr(event, "screen_y", getattr(event, "y", 0)))
        state.bottom_key = ""
        if y >= state.height - 2:
            return
        view_mod.focus_from_pointer(state, x)
        view_mod.move_scroll(state, delta)
        self.apply_layout()
        self.refresh_all()

    def on_click(self, event: Click) -> None:
        state = self.ui
        if state.modal is not None:
            self.modal_click(event.screen_x, event.screen_y)
            return
        self.forum_click(event.screen_x, event.screen_y)

    # -- modal screens ---------------------------------------------------

    def push_modal(self) -> None:
        if not isinstance(self.screen, AuditModalScreen):
            self.push_screen(AuditModalScreen())
        self.apply_modal_layout()

    def close_modal_screen(self) -> None:
        if isinstance(self.screen, AuditModalScreen):
            self.pop_screen()
            self.call_after_refresh(self._restore_input_focus)

    def _restore_input_focus(self) -> None:
        try:
            self.query_one("#command", CommandInput).focus()
        except Exception:
            pass

    # -- command helpers -------------------------------------------------

    def open_detail(self, title: str, lines: list[str]) -> None:
        modal_mod.open_detail(self.ui, title, lines)
        self.push_modal()
        self.refresh_modal()

    def open_budget_prompt(self) -> None:
        state = self.ui
        if state.busy or state.saving:
            state.pending_dir = ""
            state.add_event("请先停止审计并等待保存完成，再修改预算。")
            self.refresh_all()
            return
        try:
            state.budget_cfg = norm_budget(self.team.budget_status())
        except Exception:
            pass
        budget_mod.open_budget_prompt(state)
        self.push_modal()
        self.apply_modal_layout()
        self.refresh_modal()

    def open_broadcast(self, value: str) -> None:
        state = self.ui
        text = sanitize_text(value or "")
        err = ""
        if len(text.encode("utf-8")) > MAX_BROADCAST_BYTES:
            raw = text.encode("utf-8")[:MAX_BROADCAST_BYTES]
            while raw:
                try:
                    text = raw.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    raw = raw[:-1]
            err = "草稿超过 16 KiB；已截至上限，请检查后再发送。"
        state.modal = ModalState(
            kind="compose",
            title="广播草稿 /say",
            compose=True,
            compose_text=text,
            err=err,
        )
        self.push_modal()
        self.apply_modal_layout()
        self.refresh_modal()

    def open_agents(self) -> None:
        agents_mod.open_agents(self.ui)
        self.push_modal()
        self.apply_modal_layout()
        self.refresh_modal()

    def show_findings(self, arg: str) -> None:
        commands_mod.refresh_snapshot(self.ui, self.team)
        start, _end, _page = commands_mod.findings_for_page(self.ui, arg)
        findings_mod.open_findings(
            self.ui, list(self.ui.snapshot.findings), start
        )
        self.push_modal()
        self.apply_modal_layout()
        self.refresh_modal()

    def show_report(self) -> None:
        commands_mod.refresh_snapshot(self.ui, self.team)
        self.open_detail("审计报告摘要", commands_mod.report_lines(self.ui))

    def show_files(self, arg: str) -> None:
        commands_mod.refresh_snapshot(self.ui, self.team)
        self.open_detail("文件覆盖详情", commands_mod.files_lines(self.ui, arg))

    def export_report(self, arg: str) -> None:
        commands_mod.refresh_snapshot(self.ui, self.team)
        path = commands_mod.clean_input_dir(arg) if arg else "report.json"
        revocations: list[Any] = []
        try:
            getter = getattr(self.team, "revocations", None)
            if callable(getter):
                revocations = list(getter() or [])
        except Exception:
            revocations = []
        ok, message = commands_mod.write_report(self.ui, path, revocations)
        self.ui.add_event(message if ok else "导出失败：" + message)
        self.refresh_all()

    def list_sessions(self, arg: str) -> None:
        state = self.ui
        try:
            commands_mod.list_session_files(state.session_dir)
        except OSError as exc:
            state.add_event("读取会话目录失败：" + str(exc))
            self.refresh_all()
            return
        matches = commands_mod.session_matches(state.session_dir, arg)
        if (arg or "").strip() and not matches:
            state.add_event("未找到会话：" + arg)
            self.refresh_all()
            return
        self.open_session_picker(matches)

    def open_session_picker(self, items: list[str]) -> None:
        modal_mod.open_sessions(self.ui, list(items))
        self.push_modal()
        self.apply_modal_layout()
        self.refresh_modal()

    async def restore_session(self, path: str) -> None:
        state = self.ui
        if state.busy or state.saving:
            state.add_event("请等待当前运行或保存完成后再恢复会话。")
            self.refresh_all()
            return
        target = commands_mod.clean_input_dir(path)
        if not target:
            self.list_sessions("")
            return
        resolved = commands_mod.resolve_session_path(state.session_dir, target)
        if resolved:
            target = resolved
        else:
            matches = commands_mod.session_matches(state.session_dir, target)
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                self.open_session_picker(matches)
                return
            else:
                state.add_event("未找到会话：" + target)
                self.refresh_all()
                return
        try:
            await _maybe_await(self.team.load_session(target))
        except Exception as exc:
            state.add_event("恢复会话失败：" + str(exc))
            if state.modal is not None:
                state.modal.err = "恢复失败：" + str(exc)
            self.refresh_modal()
            self.refresh_all()
            return
        state.session += 1
        state.session_path = target
        state.workspace_ready = True
        state.thread_id = 0
        state.forum_search = ""
        state.forum_page = PageView()
        state.selected_post = 0
        state.expanded_posts = set()
        state.workspace = "已恢复会话"
        state.last_query = ""
        state.events = []
        state.scroll = [0, 0]
        state.follow = [True, True]
        if state.modal is not None:
            self.close_modal_screen()
            state.modal = None
        self.sync_team_basics()
        state.add_event("已恢复会话：" + target + "；输入 go 继续。")
        self.apply_layout()
        self.refresh_all()

    async def save_session(self, path: str) -> None:
        state = self.ui
        if not state.workspace_ready:
            state.add_event("尚未打开工作区，无法保存。")
            self.refresh_all()
            return
        if state.saving:
            state.add_event("会话正在保存，请稍候。")
            self.refresh_all()
            return
        if path:
            target = commands_mod.clean_input_dir(path)
        else:
            target = state.session_path
        if not target:
            target = os.path.join(state.session_dir, session_filename())
        if state.session_dir:
            try:
                os.makedirs(state.session_dir, exist_ok=True)
            except OSError:
                pass
        state.session_path = target
        state.saving = True
        self.refresh_all()
        try:
            await _maybe_await(self.team.save_session(target))
        except Exception as exc:
            state.add_event("保存会话失败：" + str(exc))
        else:
            state.add_event("已保存会话：" + target)
        state.saving = False
        if state.final_save_pending:
            state.final_save_pending = False
            await self.save_session("")
            return
        self.refresh_all()
        if state.quitting and not state.busy:
            self.exit()

    # -- directory start -------------------------------------------------

    def start_directory(self, directory: str) -> None:
        state = self.ui
        if state.busy or state.saving:
            state.add_event("请等待当前运行或保存完成后再切换目录。")
            self.refresh_all()
            return
        target = commands_mod.clean_input_dir(directory)
        if not target:
            state.add_event("请输入审计目录。")
            self.refresh_all()
            return
        try:
            self.team.set_workspace(target)
        except Exception as exc:
            state.add_event("目录打开失败：" + str(exc))
            self.refresh_all()
            return
        if state.modal is not None:
            self.close_modal_screen()
            state.modal = None
        state.workspace_ready = True
        state.thread_id = 0
        state.forum_search = ""
        state.forum_page = PageView()
        state.selected_post = 0
        state.expanded_posts = set()
        state.workspace = target
        state.session_path = ""
        state.events = []
        state.scroll = [0, 0]
        state.follow = [True, True]
        self.sync_team_basics()
        self.apply_layout()
        self.run_query(
            "请对当前工作区进行安全审计。先独立侦察并通过论坛交接，"
            "再由审计团队验证和报告有证据的问题。工作区：" + target
        )
        self.refresh_all()
