"""DRX-Operator main TUI application — thin presentation shell over EventBus"""

import logging
import platform
import subprocess

from textual.app import App, ComposeResult, SkipAction
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Header

from drx_agent.event_bus import EventBus, EventType, Event
from drx_agent.tui.chat_panel import ChatPanel
from drx_agent.tui.sidebar import Sidebar
from drx_agent.tui.composer import Composer
from drx_agent.tui.footer import StatusFooter
from drx_agent.tui.transcript_screen import TranscriptScreen
from drx_agent.tui.command_palette import CommandPalette

logger = logging.getLogger(__name__)

_CLIPBOARD_COMMANDS = {
    "Darwin": ["pbcopy"],
    "Linux": ["xclip", "-selection", "clipboard"],
}


def _copy_to_system_clipboard(text: str, _run=None) -> bool:
    """Write text to the OS clipboard via a native tool.

    Textual's built-in copy uses OSC 52, which macOS Terminal.app does not
    support — so we fall back to pbcopy / xclip for terminals without it.
    """
    run = _run or subprocess.run
    command = _CLIPBOARD_COMMANDS.get(platform.system())
    if command is None:
        return False
    try:
        run(command, input=text, text=True, check=True, timeout=5)
        return True
    except Exception:
        return False


class DrxAgentApp(App):
    """DRX-Operator main TUI application"""

    CSS = """
    #main-container {
        layout: horizontal;
        height: 1fr;
    }
    #chat-container {
        width: 1fr;
        border-right: solid $surface;
    }
    ChatPanel {
        height: 1fr;
    }
    Sidebar {
        width: 30;
    }
    Composer {
        height: 3;
        border-top: solid $surface;
    }
    StatusFooter {
        height: 1;
        border-top: solid $surface;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "interrupt", "Stop current task", show=True),
        Binding("ctrl+t", "toggle_transcript", "Full transcript", show=False),
        Binding("super+c,ctrl+shift+c", "copy_selection", "Copy selected text", show=False),
        Binding("ctrl+shift+a", "copy_last_message", "Copy last reply", show=False),
        Binding("ctrl+shift+t", "copy_transcript", "Copy transcript", show=False),
    ]

    def __init__(self, event_bus: EventBus, drx_agent=None):
        super().__init__()
        self.event_bus = event_bus
        self.drx_agent = drx_agent
        self.title = "DRX-Operator"
        self._main_screen = None

    def _transcript_texts(self) -> list[str]:
        master = getattr(self.drx_agent, "master", None)
        messages = getattr(master, "messages", None) or []
        texts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = msg.get("content")
            if isinstance(content, str):
                body = content
            elif isinstance(content, list):
                body = "\n".join(
                    str(b.get("text", ""))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                body = ""
            if body:
                texts.append(f"[{role}] {body}")
        return texts

    def action_interrupt(self) -> None:
        self.event_bus.publish(Event(
            type=EventType.AGENT_MESSAGE,
            data={"text": "/stop", "source": "user"},
        ))

    def action_toggle_transcript(self) -> None:
        self.push_screen("transcript_view")

    def on_click(self, event) -> None:
        if self.screen is not self._main_screen:
            return
        try:
            composer = self.query_one(Composer)
        except Exception:
            return
        if event.widget is composer:
            return
        self.set_focus(composer, scroll_visible=False)

    def on_text_selected(self, event) -> None:
        if self.screen is not self._main_screen:
            return
        from textual.widgets import Input

        selections = getattr(self.screen, "selections", {}) or {}
        if any(isinstance(w, Input) for w, sel in selections.items() if sel):
            return
        try:
            text = self.screen.get_selected_text()
        except Exception:
            return
        if text:
            self._copy_text(text, "复制")

    def _copy_text(self, text: str, title: str) -> None:
        self.copy_to_clipboard(text)
        if _copy_to_system_clipboard(text):
            self.notify(f"已复制 {len(text)} 字符", title=title)
        else:
            self.notify(
                "未找到系统剪贴板工具（macOS 需 pbcopy，Linux 需 xclip）",
                title="复制",
                severity="warning",
            )

    def action_copy_selection(self) -> None:
        try:
            text = self.screen.get_selected_text()
        except Exception:
            text = None
        if not text:
            raise SkipAction()
        self._copy_text(text, "复制")

    def action_copy_last_message(self) -> None:
        texts = self._transcript_texts()
        assistant = [t for t in texts if t.startswith("[assistant]")]
        if not assistant:
            self.notify("还没有 Agent 回复可复制", title="复制")
            return
        self._copy_text(assistant[-1][len("[assistant] "):], "复制最近回复")

    def action_copy_transcript(self) -> None:
        texts = self._transcript_texts()
        if not texts:
            self.notify("会话记录为空", title="复制")
            return
        self._copy_text("\n".join(texts), "复制完整记录")

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-container"):
            with Container(id="chat-container"):
                yield ChatPanel(self.event_bus)
            yield Sidebar(self.event_bus)
        yield Composer(self.event_bus)
        yield StatusFooter(self.event_bus)

    async def on_mount(self) -> None:
        self._main_screen = self.screen
        self.install_screen(TranscriptScreen(self), name="transcript_view")
        self.install_screen(CommandPalette(self.event_bus), name="command_palette")
        try:
            self.set_focus(self.query_one(Composer), scroll_visible=False)
        except Exception:
            pass
        self.event_bus.publish(
            Event(type=EventType.STATUS_UPDATE, data={"text": "DRX-Operator ready"})
        )
        if self.drx_agent is not None and hasattr(self.drx_agent, "async_setup"):
            try:
                await self.drx_agent.async_setup()
            except Exception as exc:
                self.event_bus.publish(
                    Event(type=EventType.ERROR, data={"message": f"MCP setup failed: {exc}"})
                )

    async def on_unmount(self) -> None:
        self._auto_save()
        if self.drx_agent is not None and hasattr(self.drx_agent, "async_teardown"):
            try:
                await self.drx_agent.async_teardown()
            except Exception:
                pass

    def _auto_save(self) -> None:
        try:
            self.event_bus.publish(Event(type=EventType.SESSION_SAVE, data={}))
        except Exception:
            logger.exception("Session auto-save failed during shutdown")
