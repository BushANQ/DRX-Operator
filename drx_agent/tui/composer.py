"""Input box — user messages/commands/approval responses"""

from textual.widgets import Input
from textual.binding import Binding

from drx_agent.event_bus import EventBus, EventType, Event


class Composer(Input):
    """Input box — user messages/commands/approval responses"""

    BINDINGS = [
        Binding("ctrl+k", "command_palette", "Command palette"),
    ]

    def __init__(self, event_bus: EventBus):
        super().__init__(placeholder="Enter message or command... (/help for commands)")
        self.event_bus = event_bus

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        if text.startswith("/"):
            self._handle_command(text)
        elif text.lower() in ("y", "n", "v", "a"):
            self._handle_approval(text.lower())
        else:
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={"text": text, "source": "user"}
            ))
        self.value = ""

    def _handle_command(self, cmd: str) -> None:
        if cmd == "/help":
            self.event_bus.publish(Event(type=EventType.AGENT_MESSAGE, data={
                "text": (
                    "Commands: /save /resume /target <host> /plan /act /mode "
                    "/memory /memory reload /image <path> [prompt] /scan <host> "
                    "/exploit <host> /status /stop /context /progress /dream"
                ),
                "source": "system"
            }))
        elif cmd == "/save":
            self.event_bus.publish(Event(type=EventType.SESSION_SAVE, data={}))
        elif cmd == "/resume":
            self.event_bus.publish(Event(type=EventType.SESSION_RESTORE, data={}))
        elif cmd.startswith("/image "):
            rest = cmd[len("/image "):].strip()
            path, _, prompt = rest.partition(" ")
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={
                    "source": "user",
                    "text": prompt or "(image attached)",
                    "image_path": path,
                },
            ))
        else:
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={"text": cmd, "source": "user"},
            ))

    def _handle_approval(self, response: str) -> None:
        self.event_bus.publish(Event(
            type=EventType.APPROVAL_RESPONSE,
            data={"response": response}
        ))

    def action_command_palette(self) -> None:
        self.app.push_screen("command_palette")
