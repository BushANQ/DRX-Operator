"""Transcript screen — read-only view of the full user/agent conversation (Ctrl+T).
Renders user/assistant turns from master.messages — the readable record, not the tool/system plumbing."""

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, Static


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
            elif block.get("type") == "image_url":
                parts.append("[image attached]")
            else:
                parts.append(str(block.get("text", "")))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


class TranscriptScreen(Screen[None]):
    """Full, read-only conversation transcript (Ctrl+T)."""

    BINDINGS = [
        Binding("escape", "pop_screen", "返回", show=True),
    ]

    DEFAULT_CSS = """
    TranscriptScreen {
        background: $surface;
    }
    TranscriptScreen #transcript-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
        background: $boost;
    }
    TranscriptScreen #transcript-scroll {
        height: 1fr;
        padding: 0 1;
    }
    TranscriptScreen #transcript-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $boost;
    }
    """

    def __init__(self, app: object) -> None:
        super().__init__()
        self._app = app
        self.transcript_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("完整会话记录", id="transcript-title")
        yield VerticalScroll(
            Static(
                self._build_transcript_text(self._collect_messages()),
                id="transcript-content",
            ),
            id="transcript-scroll",
        )
        yield Static("ESC 返回 · 只读", id="transcript-hint")

    def on_mount(self) -> None:
        self._refresh()

    def action_pop_screen(self) -> None:
        """Esc — return to the previous screen."""
        self.dismiss()

    def _on_screen_resume(self, event: events.ScreenResume) -> None:
        super()._on_screen_resume(event)
        self._refresh()

    def _collect_messages(self) -> list[dict[str, object]]:
        agent = getattr(self._app, "drx_agent", None)
        master = getattr(agent, "master", None)
        return list(getattr(master, "messages", None) or [])

    def _build_transcript_text(self, messages: list[dict[str, object]]) -> str:
        lines: list[str] = []
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "")).strip().lower()
            if role == "user":
                prefix = "▸ 用户"
            elif role == "assistant":
                prefix = "◆ Agent"
            else:
                continue
            text = _message_text(msg.get("content")).strip()
            if not text:
                continue
            lines.append(f"{prefix}: {text}")
            lines.append("")
        if not lines:
            return "（暂无会话记录）"
        return "\n".join(lines).rstrip()

    def _refresh(self) -> None:
        self.transcript_text = self._build_transcript_text(self._collect_messages())
        self.query_one("#transcript-content", Static).update(self.transcript_text)

