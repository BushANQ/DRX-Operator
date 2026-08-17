"""Sidebar: task board + active sub-agents"""

from textual.widgets import Static
from textual.containers import Container

from drx_agent.event_bus import EventBus, EventType, Event


class Sidebar(Container):
    """Sidebar: task board + active sub-agents"""

    def __init__(self, event_bus: EventBus):
        super().__init__()
        self.event_bus = event_bus
        self._tasks: list[dict] = []
        self._active_agents: list[dict] = []

    def compose(self):
        yield Static("Tasks", id="sidebar-task-header")
        yield Static("", id="sidebar-task-list")
        yield Static("Sub-Agents", id="sidebar-agent-header")
        yield Static("", id="sidebar-agent-list")

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.STATUS_UPDATE, self._on_status)
        self.event_bus.subscribe(EventType.SUB_AGENT_DISPATCH, self._on_agent_dispatch)
        self.event_bus.subscribe(EventType.SUB_AGENT_RESULT, self._on_agent_result)

    def _on_status(self, event: Event) -> None:
        if "tasks" in event.data:
            self._tasks = event.data.get("tasks") or []
            self._render_tasks()

    def _on_agent_dispatch(self, event: Event) -> None:
        self._active_agents.append({
            "name": event.data.get("agent_id", "?"),
            "type": event.data.get("type", "?"),
            "target": event.data.get("target", "?"),
            "status": "running"
        })
        self._render_agents()

    def _on_agent_result(self, event: Event) -> None:
        agent_id = event.data.get("agent_id", "")
        for a in self._active_agents:
            if a["name"] == agent_id:
                a["status"] = event.data.get("status", "done")
        self._render_agents()

    def _render_tasks(self) -> None:
        lines = []
        for t in self._tasks:
            status = t.get("status", "pending")
            icon = {
                "completed": "[#3fb950]☑[/]",
                "done": "[#3fb950]☑[/]",
                "in_progress": "[#f0883e]▶[/]",
                "running": "[#f0883e]▶[/]",
                "pending": "[#8b949e]☐[/]",
            }.get(status, "[#8b949e]·[/]")
            label = t.get("content") or t.get("name") or "?"
            if status in ("completed", "done"):
                lines.append(f"{icon} [#8b949e strike]{label}[/]")
            else:
                lines.append(f"{icon} {label}")
        sidebar_list = self.query_one("#sidebar-task-list", Static)
        sidebar_list.update("\n".join(lines) if lines else "")

    def _render_agents(self) -> None:
        lines = []
        for a in self._active_agents:
            icon = "*" if a["status"] == "running" else "-"
            lines.append(f"{icon} {a['name']} {a['target']}")
        self.query_one("#sidebar-agent-list", Static).update("\n".join(lines))
