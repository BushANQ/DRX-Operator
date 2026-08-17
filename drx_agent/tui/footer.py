"""Bottom status bar: cost/cache/rate/active targets/tokens"""

from rich.text import Text as RichText
from textual.widgets import Static

from drx_agent.event_bus import EventBus, EventType, Event

_AUTHOR = "[bold #58a6ff]BushSEC[/] [#8b949e]· github.com/BushANQ[/]"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _visible_len(markup: str) -> int:
    try:
        return RichText.from_markup(markup).cell_len
    except Exception:
        return len(markup)


class StatusFooter(Static):
    """Bottom status bar: cost/cache/rate/active targets/tokens."""

    DEFAULT_RENDER = "cost: $0.0000 | tokens: 0 in / 0 out | cache: 0 | rate: 0 r/min | targets: 0"

    def __init__(self, event_bus: EventBus):
        super().__init__(self.DEFAULT_RENDER, markup=True)
        self.event_bus = event_bus
        self._state: dict = {
            "cost": "$0.0000",
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_total": 0,
            "cache_hits": 0,
            "rate": 0,
            "active_targets": 0,
            "requests": 0,
            "mode": "act",
            "text": "",
        }

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.STATUS_UPDATE, self._on_status)

    def on_resize(self, event) -> None:
        self._render()

    def _on_status(self, event: Event) -> None:
        data = event.data
        for k in (
            "cost", "tokens_in", "tokens_out", "tokens_total",
            "cache_hits", "rate", "active_targets", "requests", "mode",
        ):
            if k in data:
                self._state[k] = data[k]
        if "text" in data:
            self._state["text"] = str(data["text"])[:48]
        self._render()

    def _build_text(self) -> str:
        s = self._state
        mode_style = "#f0883e" if s["mode"] == "plan" else "#3fb950"
        parts = [
            f"[bold {mode_style}]{s['mode'].upper()}[/]",
            f"[#3fb950]cost:[/] {s['cost']}",
            f"[#58a6ff]tokens:[/] {_fmt_tokens(s['tokens_in'])} in / "
            f"{_fmt_tokens(s['tokens_out'])} out",
            f"[#8b949e]cache:[/] {_fmt_tokens(s['cache_hits'])}",
            f"[#d2a8ff]rate:[/] {s['rate']} r/min",
            f"[#f0883e]targets:[/] {s['active_targets']}",
        ]
        if s["text"]:
            parts.insert(0, f"[italic #8b949e]{s['text']}[/]")
        status = " │ ".join(parts)
        width = self.size.width or 140
        pad = width - _visible_len(status) - _visible_len(_AUTHOR)
        if pad < 1:
            pad = 1
        return status + " " * pad + _AUTHOR

    def _render(self) -> None:
        self.update(self._build_text())

