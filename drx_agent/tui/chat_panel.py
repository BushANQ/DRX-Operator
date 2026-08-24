"""Chat area — per-message Widgets with real typewriter streaming via Static.update().

Handles agent/user messages, tool cards, diffs, todo snapshots and streaming bubbles."""

import json
import threading

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from drx_agent.event_bus import Event, EventBus, EventType
from drx_agent.tui.banner import build_banner


# The Textual loop thread, captured at import time; worker-thread publishes marshal back to it.
_MAIN_THREAD_ID = threading.get_ident()


class BannerBubble(Static):
    """Startup banner: DRX-OPERATOR ASCII art + author line."""

    DEFAULT_CSS = """
    BannerBubble {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.update(build_banner())


class DiffBubble(Static):
    """A unified-diff bubble with green / red / cyan line coloring."""

    DEFAULT_CSS = """
    DiffBubble {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, title: str, diff_text: str) -> None:
        super().__init__()
        self._title = title
        self._diff = diff_text
        self._refresh()

    def _refresh(self) -> None:
        rendered = Text()
        rendered.append("✎ ", style="#d2a8ff")
        rendered.append(self._title, style="bold #d2a8ff")
        rendered.append("\n")
        if not self._diff.strip():
            rendered.append("  (no textual changes)\n", style="#8b949e")
            self.update(rendered)
            return
        for line in self._diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                rendered.append(line + "\n", style="#8b949e")
            elif line.startswith("@@"):
                rendered.append(line + "\n", style="#58a6ff")
            elif line.startswith("+"):
                rendered.append(line + "\n", style="#3fb950")
            elif line.startswith("-"):
                rendered.append(line + "\n", style="#f85149")
            else:
                rendered.append(line + "\n", style="#8b949e")
        self.update(rendered)


class TodoBubble(Static):
    """A rendered snapshot of the current todo list."""

    DEFAULT_CSS = """
    TodoBubble {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    _STATUS_GLYPH = {
        "pending": "☐",
        "in_progress": "▶",
        "completed": "☑",
    }
    _STATUS_STYLE = {
        "pending": "#8b949e",
        "in_progress": "#f0883e",
        "completed": "#3fb950",
    }

    def __init__(self, todos: list) -> None:
        super().__init__()
        self._todos = todos or []
        self._refresh()

    def _refresh(self) -> None:
        rendered = Text()
        rendered.append("☰ ", style="#d2a8ff")
        rendered.append(f"Todos ({len(self._todos)})\n", style="bold #d2a8ff")
        if not self._todos:
            rendered.append("  (empty)\n", style="#8b949e")
            self.update(rendered)
            return
        for t in self._todos:
            status = t.get("status", "pending")
            glyph = self._STATUS_GLYPH.get(status, "·")
            style = self._STATUS_STYLE.get(status, "#8b949e")
            rendered.append(f"  {glyph} ", style=style)
            text_style = "strike #8b949e" if status == "completed" else None
            rendered.append(
                t.get("content", "") + "\n",
                style=text_style if text_style else "",
            )
        self.update(rendered)


class ToolCard(Collapsible):
    """A foldable tool-call card."""

    DEFAULT_CSS = """
    ToolCard {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
    }
    ToolCard > CollapsibleTitle {
        background: $surface;
    }
    ToolCard Contents {
        padding: 0 0 0 2;
    }
    """

    _STATUS_GLYPH = {
        "running": "⚙",
        "done":    "✓",
        "error":   "✗",
        "pending": "·",
    }
    _STATUS_STYLE = {
        "running": "#f0883e",
        "done":    "#3fb950",
        "error":   "#f85149",
        "pending": "#d2a8ff",
    }

    def __init__(self, tool_name: str, preview: str) -> None:
        self._tool_name = tool_name
        self._preview = preview or ""
        self._status = "running"
        self._body_widget = Static("", id=None)
        super().__init__(self._body_widget, title=self._format_title(), collapsed=True)

    def _format_title(self) -> str:
        glyph = self._STATUS_GLYPH.get(self._status, "·")
        style = self._STATUS_STYLE.get(self._status, "#8b949e")
        line = self._preview.split("\n", 1)[0]
        if len(line) > 80:
            line = line[:80] + "…"
        return f"[{style}]{glyph}[/] [bold]{self._tool_name}[/]  [#8b949e]{line}[/]"

    def update_status(self, status: str) -> None:
        if status:
            self._status = status
            try:
                self.title = self._format_title()
            except Exception:
                pass

    def apply_result(self, output: str, status: str = "done") -> None:
        self.update_status(status)
        body = Text()
        body.append(self._preview + "\n", style="#8b949e")
        body.append("─" * 6 + "\n", style="#30363d")
        rendered_output = self._render_output(output, status)
        body.append(rendered_output)
        self._body_widget.update(body)
        if status == "error":
            try:
                self.collapsed = False
            except Exception:
                pass

    def _render_output(self, output: str, status: str) -> Text:
        if not output:
            t = Text()
            t.append(f"(no output, status={status})", style="#8b949e")
            return t
        text = output
        try:
            parsed = json.loads(output)
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            pass
        t = Text()
        lines = text.splitlines()
        shown = lines[:80]
        body = "\n".join(shown)
        if len(body) > 4000:
            body = body[:4000] + "\n…(truncated for display)"
        elif len(lines) > 80:
            body = body + f"\n…({len(lines) - 80} more lines)"
        t.append(body, style="#c9d1d9" if status != "error" else "#f85149")
        return t


class MessageBubble(Static):
    """One chat message. Body can grow incrementally for streaming."""

    DEFAULT_CSS = """
    MessageBubble {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    def __init__(
        self,
        marker: str = "◆",
        marker_style: str = "#58a6ff",
        text: str = "",
        body_style: str | None = None,
        markdown: bool = False,
    ) -> None:
        super().__init__()
        self._marker = marker
        self._marker_style = marker_style
        self._body_style = body_style
        # Markdown renders only once finalized; streaming stays plain text for a fast typewriter effect (no half-parsed fences).
        self._markdown = markdown
        self._md_ready = False
        self._text = text
        self._refresh_render()

    def append(self, delta: str) -> None:
        """Append text and re-render. Textual diffs cells under the hood."""
        if not delta:
            return
        self._text += delta
        self._refresh_render()

    def set_text(self, text: str) -> None:
        """Replace text wholesale (used for final-stream reconciliation)."""
        self._text = text
        self._refresh_render()

    def finalize(self) -> None:
        """Stream finished — switch to rendered Markdown if enabled."""
        if self._markdown and not self._md_ready:
            self._md_ready = True
            self._refresh_render()

    def _refresh_render(self) -> None:
        if self._markdown and self._md_ready and self._text.strip():
            try:
                from rich.console import Group
                from rich.markdown import Markdown

                marker = Text(self._marker, style=self._marker_style)
                md = Markdown(self._text, code_theme="github-dark")
                self.update(Group(marker, md))
                return
            except Exception:
                pass

        rendered = Text()
        rendered.append(f"{self._marker} ", style=self._marker_style)
        if self._body_style:
            rendered.append(self._text, style=self._body_style)
        else:
            rendered.append(self._text)
        self.update(rendered)


class ChatPanel(VerticalScroll):
    """Scrollable column of MessageBubble widgets."""

    DEFAULT_CSS = """
    ChatPanel {
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(self, event_bus: EventBus):
        super().__init__()
        self.event_bus = event_bus
        self._streams: dict[str, MessageBubble] = {}
        self._open_tools: dict[int, ToolCard] = {}
        self._setup_subscriptions()

    def on_mount(self) -> None:
        self._append(BannerBubble())

    def _setup_subscriptions(self) -> None:
        # Thread-marshal every handler: worker-thread publishes reroute to the Textual loop thread, where widget mounts are safe.
        self.event_bus.subscribe(
            EventType.AGENT_MESSAGE, self._safe(self._on_agent_message)
        )
        self.event_bus.subscribe(
            EventType.TOOL_CALL, self._safe(self._on_tool_call)
        )
        self.event_bus.subscribe(
            EventType.TOOL_RESULT, self._safe(self._on_tool_result)
        )
        self.event_bus.subscribe(
            EventType.SUB_AGENT_DISPATCH, self._safe(self._on_sub_dispatch)
        )
        self.event_bus.subscribe(
            EventType.SUB_AGENT_RESULT, self._safe(self._on_sub_result)
        )
        self.event_bus.subscribe(
            EventType.APPROVAL_REQUEST, self._safe(self._on_approval)
        )
        self.event_bus.subscribe(EventType.ERROR, self._safe(self._on_error))

    def _safe(self, handler):
        

        def wrapper(event: Event) -> None:
            if threading.get_ident() == _MAIN_THREAD_ID:
                handler(event)
                return
            try:
                self.app.call_from_thread(handler, event)
            except Exception:
                handler(event)

        return wrapper


    def _append(self, bubble: MessageBubble) -> None:
        try:
            self.mount(bubble)
        except Exception:
            return
        if self._user_is_near_bottom():
            self.call_after_refresh(self.scroll_end, animate=False)

    def _user_is_near_bottom(self) -> bool:
        try:
            return self.scroll_y >= self.max_scroll_y - 1
        except Exception:
            return True


    def _on_agent_message(self, event: Event) -> None:
        data = event.data

        if data.get("streaming"):
            self._handle_stream(data)
            return

        text = data.get("text") or data.get("content", "")
        if not text:
            return

        source = data.get("source", "")
        role = data.get("role", "")
        is_agent = role == "assistant" or source == "agent"
        if is_agent:
            kind = data.get("type", "think")
            marker, style = (
                ("▶", "#3fb950") if kind == "action" else ("◆", "#58a6ff")
            )
        elif source == "system":
            marker, style = "·", "#8b949e"
        else:
            marker, style = ">", "#d2a8ff"

        bubble = MessageBubble(
            marker=marker, marker_style=style, text=text, markdown=is_agent
        )
        self._append(bubble)
        if is_agent:
            bubble.finalize()

    def _handle_stream(self, data: dict) -> None:
        sid = data.get("stream_id")
        if not sid:
            return

        if data.get("final"):
            bubble = self._streams.pop(sid, None)
            if bubble is not None:
                # Snap to the agent's full text if provided (defends against delta loss).
                full = data.get("text") or data.get("content")
                if full and full != bubble._text:
                    bubble.set_text(full)
                bubble.finalize()
                if self._user_is_near_bottom():
                    self.call_after_refresh(self.scroll_end, animate=False)
            return

        delta = data.get("delta", "")
        if not delta:
            return

        bubble = self._streams.get(sid)
        if bubble is None:
            kind = data.get("type", "think")
            marker, style = (
                ("▶", "#3fb950") if kind == "action" else ("◆", "#58a6ff")
            )
            bubble = MessageBubble(
                marker=marker, marker_style=style, text="", markdown=True
            )
            self._streams[sid] = bubble
            self._append(bubble)
        bubble.append(delta)
        if self._user_is_near_bottom():
            self.call_after_refresh(self.scroll_end, animate=False)


    def _on_tool_call(self, event: Event) -> None:
        data = event.data
        tool_name = data.get("tool")
        if not tool_name:
            lang = data.get("language", "script")
            num = data.get("script_num", "?")
            tool_name = f"execute_{lang}_script#{num}"
        code = str(data.get("code", ""))[:600]
        status = data.get("status", "running")
        call_seq = data.get("call_seq")

        card = ToolCard(tool_name=tool_name, preview=code)
        card.update_status(status)
        if call_seq is not None:
            self._open_tools[call_seq] = card
        self._append(card)

    _DIFF_TOOLS = {"write_file", "edit_file", "multi_edit_file"}

    def _on_tool_result(self, event: Event) -> None:
        data = event.data
        tool = data.get("tool", "")
        output = data.get("output", "")
        status = data.get("status", "done")
        call_seq = data.get("call_seq")

        if tool in self._DIFF_TOOLS and output:
            try:
                parsed = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and not parsed.get("error"):
                diff_text = parsed.get("diff") or ""
                summary = parsed.get("summary") or parsed.get("path") or tool
                card = self._open_tools.pop(call_seq, None) if call_seq is not None else None
                if card is not None:
                    card.apply_result(summary, status="done")
                self._append(DiffBubble(title=summary, diff_text=diff_text))
                return

        if tool == "todo_write" and output:
            try:
                parsed = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("ok"):
                count = parsed.get("todo_count", 0)
                done = parsed.get("completed", 0)
                msg = f"todos updated · {done}/{count} completed"
                card = self._open_tools.pop(call_seq, None) if call_seq is not None else None
                if card is not None:
                    card.apply_result(msg, status="done")
                else:
                    self._append(MessageBubble(
                        marker="☰", marker_style="#d2a8ff",
                        text=msg, body_style="#8b949e",
                    ))
                return

        if not output:
            stdout = data.get("stdout", "")
            stderr = data.get("stderr", "")
            output = stdout
            if stderr:
                output = (output + "\n" + stderr) if output else stderr
        if not output and status:
            output = f"(no output, status={status})"

        card = self._open_tools.pop(call_seq, None) if call_seq is not None else None
        if card is not None:
            card.apply_result(output, status=status)
            return

        self._append(MessageBubble(
            marker=" ", marker_style="#8b949e",
            text=str(output)[:600], body_style="#8b949e",
        ))

    def _on_sub_dispatch(self, event: Event) -> None:
        d = event.data
        text = f"Sub-Agent [{d.get('type', '?')}] → {d.get('target', '?')}"
        self._append(MessageBubble(marker="●", marker_style="#3fb950", text=text))

    def _on_sub_result(self, event: Event) -> None:
        d = event.data
        text = f"  └─ {d.get('status', '')}"
        self._append(MessageBubble(
            marker=" ", marker_style="#8b949e",
            text=text, body_style="#8b949e",
        ))

    def _on_approval(self, event: Event) -> None:
        d = event.data
        op = d.get("operation", "")
        risk = d.get("risk_level", "L2")
        color = {
            "L0": "#3fb950", "L1": "#3fb950",
            "L2": "#f0883e", "L3": "#f85149", "L4": "#f85149",
        }.get(risk, "#f0883e")
        text = f"[{risk}] Confirm: {op}\n  [y]approve [n]deny [v]view details"
        self._append(MessageBubble(marker="⚠", marker_style=color, text=text))

    def _on_error(self, event: Event) -> None:
        self._append(MessageBubble(
            marker="✗", marker_style="#f85149",
            text=event.data.get("message", ""), body_style="#f85149",
        ))

