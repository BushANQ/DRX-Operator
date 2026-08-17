"""Bottom status bar: cost/cache/rate/active targets/tokens"""

from textual.widgets import Static

from drx_agent.event_bus import EventBus, EventType, Event


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


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
        }

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.STATUS_UPDATE, self._on_status)

    def _on_status(self, event: Event) -> None:
        data = event.data
        for k in (
            "cost", "tokens_in", "tokens_out", "tokens_total",
            "cache_hits", "rate", "active_targets", "requests", "mode",
        ):
            if k in data:
                self._state[k] = data[k]

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
        self.update(" │ ".join(parts))

