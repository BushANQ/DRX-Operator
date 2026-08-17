"""DRX-AGENT main TUI application — thin presentation shell over EventBus"""

import logging

from textual.app import App, ComposeResult
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


class DrxAgentApp(App):
    """DRX-AGENT main TUI application"""

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
    ]

    def __init__(self, event_bus: EventBus, drx_agent=None):
        super().__init__()
        self.event_bus = event_bus
        self.drx_agent = drx_agent
        self.title = "DRX-AGENT"

    def action_interrupt(self) -> None:
        self.event_bus.publish(Event(
            type=EventType.AGENT_MESSAGE,
            data={"text": "/stop", "source": "user"},
        ))

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-container"):
            with Container(id="chat-container"):
                yield ChatPanel(self.event_bus)
            yield Sidebar(self.event_bus)
        yield Composer(self.event_bus)
        yield StatusFooter(self.event_bus)

    async def on_mount(self) -> None:
        self.install_screen(TranscriptScreen(self), name="transcript_view")
        self.install_screen(CommandPalette(self.event_bus), name="command_palette")
        self.event_bus.publish(
            Event(type=EventType.STATUS_UPDATE, data={"text": "DRX-AGENT ready"})
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
