"""Screen state for the audit terminal interface.

The engine is queried for immutable snapshots; everything the interface draws
lives in :class:`UiState`.  Normalisers accept either the engine dataclasses or
mapping-shaped values so the interface stays decoupled from transport details.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from drx_agent.code_audit.tui.theme import bounded_text

MAX_UI_EVENTS = 120
MAX_BROADCAST_BYTES = 16 * 1024
DEFAULT_PAGE_SIZE = 60
ROW_REFRESH_SECONDS = 0.25
BOTTOM_KEY_WINDOW = 0.8

PLACEHOLDER = "audit directory / go /budget /help"
BROADCAST_PLACEHOLDER = "Message to all agents..."

OPERATIONAL_KINDS = frozenset(
    {
        "state",
        "model_progress",
        "worker",
        "name",
        "forum",
        "tool",
        "waiting",
        "info",
        "error",
        "verify_progress",
        "verify_done",
    }
)

PHASE_LABELS = {
    "recon": "侦察",
    "audit": "审计",
    "completed": "完成",
    "failed": "失败",
    "idle": "待命",
    "": "待命",
}

STATUS_LABELS = {
    "running": "运行",
    "waiting": "等待",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "停止",
    "pending": "待开始",
}


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read *name* from a dataclass-like object or a mapping."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first_field(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = get_field(obj, name, _MISSING)
        if value is not _MISSING:
            return value
    return default


_MISSING = object()


def phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


# ---------------------------------------------------------------------------
# Value views


@dataclass
class GenerationView:
    output_tokens: int = 0
    reasoning_tokens: int = 0
    tool_tokens: int = 0
    estimated: bool = False
    received_at: str = ""


@dataclass
class WorkerView:
    id: str = ""
    name: str = ""
    phase: str = ""
    status: str = "pending"
    activity: str = ""
    turn: int = 0
    last_model_activity: str = ""
    last_tool_activity: str = ""
    generation: GenerationView | None = None
    last_tool: str = ""
    output_excerpt: str = ""
    output_partial: bool = False


@dataclass
class BudgetView:
    infinite_mode: bool = False
    hours: int = 0
    minutes: int = 0
    token_limit: int = 0
    used_tokens: int = 0
    elapsed: float = 0.0
    running: bool = False
    stop_reason: str = ""
    estimated: bool = False


@dataclass
class MessageView:
    id: int = 0
    thread_id: int = 0
    agent_id: str = ""
    agent_name: str = ""
    stage: str = ""
    reply_to: int = 0
    topic: str = ""
    content: str = ""
    created_at: Any = None
    closed: bool = False
    pinned: bool = False
    announcement: bool = False


@dataclass
class PostView:
    id: int = 0
    root: MessageView = field(default_factory=MessageView)
    replies: list[MessageView] = field(default_factory=list)
    updated_at: Any = None
    last_id: int = 0
    root_missing: bool = False


@dataclass
class PageView:
    posts: list[PostView] = field(default_factory=list)
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    total_posts: int = 0
    total_pages: int = 0
    has_more: bool = False
    gap: bool = False


@dataclass
class TodoView:
    id: int = 0
    title: str = ""
    status: str = ""
    priority: str = ""


@dataclass
class FindingView:
    id: int = 0
    severity: str = ""
    title: str = ""
    path: str = ""
    line: int = 0
    evidence: str = ""
    impact: str = ""
    recommendation: str = ""
    cwe: str = ""


@dataclass
class FileView:
    path: str = ""
    status: str = ""
    note: str = ""


@dataclass
class VariableView:
    name: str = ""
    path: str = ""
    status: str = ""
    note: str = ""


@dataclass
class FlowView:
    name: str = ""
    status: str = ""
    note: str = ""


@dataclass
class AuditView:
    ended: bool = False
    summary: str = ""
    next_steps: str = ""


@dataclass
class ProjectView:
    note: str = ""


@dataclass
class SnapshotView:
    todos: list[TodoView] = field(default_factory=list)
    findings: list[FindingView] = field(default_factory=list)
    project: ProjectView = field(default_factory=ProjectView)
    files: list[FileView] = field(default_factory=list)
    variables: list[VariableView] = field(default_factory=list)
    flows: list[FlowView] = field(default_factory=list)
    audit: AuditView = field(default_factory=AuditView)


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def norm_generation(value: Any) -> GenerationView | None:
    if value is None:
        return None
    return GenerationView(
        output_tokens=_int(get_field(value, "output_tokens")),
        reasoning_tokens=_int(get_field(value, "reasoning_tokens")),
        tool_tokens=_int(get_field(value, "tool_tokens")),
        estimated=_bool(get_field(value, "estimated")),
        received_at=_str(get_field(value, "received_at")),
    )


def norm_worker(value: Any) -> WorkerView:
    return WorkerView(
        id=_str(get_field(value, "id")),
        name=_str(get_field(value, "name")),
        phase=_str(get_field(value, "phase")),
        status=_str(get_field(value, "status"), "pending"),
        activity=_str(get_field(value, "activity")),
        turn=_int(get_field(value, "turn")),
        last_model_activity=_str(get_field(value, "last_model_activity")),
        last_tool_activity=_str(get_field(value, "last_tool_activity")),
        generation=norm_generation(get_field(value, "generation")),
        last_tool=_str(get_field(value, "last_tool")),
        output_excerpt=_str(get_field(value, "output_excerpt")),
        output_partial=_bool(get_field(value, "output_partial")),
    )


def norm_budget(value: Any) -> BudgetView:
    defaults = BudgetView()
    if value is None:
        return defaults
    return BudgetView(
        infinite_mode=_bool(get_field(value, "infinite_mode")),
        hours=_int(get_field(value, "hours")),
        minutes=_int(get_field(value, "minutes")),
        token_limit=_int(get_field(value, "token_limit")),
        used_tokens=_int(get_field(value, "used_tokens")),
        elapsed=_float(get_field(value, "elapsed")),
        running=_bool(get_field(value, "running")),
        stop_reason=_str(get_field(value, "stop_reason")),
        estimated=_bool(get_field(value, "estimated")),
    )


def norm_message(value: Any) -> MessageView:
    if value is None:
        return MessageView()
    return MessageView(
        id=_int(get_field(value, "id")),
        thread_id=_int(get_field(value, "thread_id")),
        agent_id=_str(get_field(value, "agent_id")),
        agent_name=_str(get_field(value, "agent_name")),
        stage=_str(get_field(value, "stage")),
        reply_to=_int(get_field(value, "reply_to")),
        topic=_str(get_field(value, "topic")),
        content=_str(get_field(value, "content")),
        created_at=get_field(value, "created_at"),
        closed=_bool(get_field(value, "closed")),
        pinned=_bool(get_field(value, "pinned")),
        announcement=_bool(get_field(value, "announcement")),
    )


def norm_post(value: Any) -> PostView:
    return PostView(
        id=_int(get_field(value, "id")),
        root=norm_message(get_field(value, "root")),
        replies=[norm_message(item) for item in (get_field(value, "replies") or [])],
        updated_at=get_field(value, "updated_at"),
        last_id=_int(get_field(value, "last_id")),
        root_missing=_bool(get_field(value, "root_missing")),
    )


def norm_page(value: Any) -> PageView:
    posts = [norm_post(item) for item in (get_field(value, "posts") or [])]
    return PageView(
        posts=posts,
        page=_int(get_field(value, "page"), 1),
        page_size=_int(get_field(value, "page_size"), DEFAULT_PAGE_SIZE),
        total_posts=_int(get_field(value, "total_posts")),
        total_pages=_int(get_field(value, "total_pages")),
        has_more=_bool(get_field(value, "has_more")),
        gap=_bool(get_field(value, "gap")),
    )


def norm_snapshot(value: Any) -> SnapshotView:
    snapshot = get_field(value, "snapshot", value)
    if snapshot is None:
        return SnapshotView()
    return SnapshotView(
        todos=[
            TodoView(
                id=_int(get_field(item, "id")),
                title=_str(get_field(item, "title")),
                status=_str(get_field(item, "status")),
                priority=_str(get_field(item, "priority")),
            )
            for item in (get_field(snapshot, "todos") or [])
        ],
        findings=[
            FindingView(
                id=_int(get_field(item, "id")),
                severity=_str(get_field(item, "severity")),
                title=_str(get_field(item, "title")),
                path=_str(get_field(item, "path")),
                line=_int(get_field(item, "line")),
                evidence=_str(get_field(item, "evidence")),
                impact=_str(get_field(item, "impact")),
                recommendation=_str(get_field(item, "recommendation")),
                cwe=_str(get_field(item, "cwe")),
            )
            for item in (get_field(snapshot, "findings") or [])
        ],
        project=ProjectView(note=_str(get_field(get_field(snapshot, "project"), "note"))),
        files=[
            FileView(
                path=_str(get_field(item, "path")),
                status=_str(get_field(item, "status")),
                note=_str(get_field(item, "note")),
            )
            for item in (get_field(snapshot, "files") or [])
        ],
        variables=[
            VariableView(
                name=_str(get_field(item, "name")),
                path=_str(get_field(item, "path")),
                status=_str(get_field(item, "status")),
                note=_str(get_field(item, "note")),
            )
            for item in (get_field(snapshot, "variables") or [])
        ],
        flows=[
            FlowView(
                name=_str(get_field(item, "name")),
                status=_str(get_field(item, "status")),
                note=_str(get_field(item, "note")),
            )
            for item in (get_field(snapshot, "flows") or [])
        ],
        audit=AuditView(
            ended=_bool(get_field(get_field(snapshot, "audit"), "ended")),
            summary=_str(get_field(get_field(snapshot, "audit"), "summary")),
            next_steps=_str(get_field(get_field(snapshot, "audit"), "next_steps")),
        ),
    )


# ---------------------------------------------------------------------------
# Formatting helpers


_ISO_FRACTION = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
)


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    match = _ISO_FRACTION.search(text)
    if match:
        year, month, day, hour, minute, second, fraction = match.groups()
        micro = int((fraction or "0")[:6].ljust(6, "0"))
        try:
            return datetime(
                int(year), int(month), int(day), int(hour), int(minute), int(second), micro
            )
        except ValueError:
            return None
    return None


def activity_clock(value: Any) -> str:
    stamp = parse_timestamp(value)
    if stamp is not None:
        return stamp.strftime("%H:%M:%S")
    return _str(value)


def format_clock(value: Any) -> str:
    return activity_clock(value)


def format_datetime(value: Any) -> str:
    stamp = parse_timestamp(value)
    if stamp is not None:
        return stamp.strftime("%Y-%m-%d %H:%M:%S")
    return _str(value)


def _round_half_away(value: float) -> int:
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def format_duration(seconds: float) -> str:
    """Format a duration as whole seconds with h/m/s units."""
    if seconds is None:
        return "0s"
    total = _round_half_away(float(seconds))
    if total == 0:
        return "0s"
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = sign
    if hours:
        parts += f"{hours}h"
    if hours or minutes:
        parts += f"{minutes}m"
    parts += f"{secs}s"
    return parts


def now_clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def session_filename() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S.%f")[:-3]
    return f"{stamp}.json"


# ---------------------------------------------------------------------------
# Layout


@dataclass(frozen=True)
class Layout:
    left: int
    right: int
    height: int
    narrow: bool

    @property
    def total_width(self) -> int:
        return self.left if self.narrow else self.left + 1 + self.right


def compute_layout(width: int, height: int) -> Layout:
    pane_height = max(3, height - 3)
    narrow = width < 90
    if narrow:
        pane_width = max(4, width)
        return Layout(pane_width, pane_width, pane_height, True)
    left = max(32, width * 36 // 100)
    return Layout(left, width - left - 1, pane_height, False)


# ---------------------------------------------------------------------------
# Modal state


@dataclass
class FindingsModal:
    items: list[FindingView] = field(default_factory=list)
    selected: int = 0
    detail: bool = False
    starts: list[int] = field(default_factory=list)
    list_wrapped: list[str] = field(default_factory=list)
    list_width: int = 0
    list_scroll: int = 0


@dataclass
class SessionsModal:
    items: list[str] = field(default_factory=list)
    selected: int = 0
    starts: list[int] = field(default_factory=list)


@dataclass
class AgentsModal:
    selected_id: str = ""
    list_scroll: int = 0
    line_starts: list[int] = field(default_factory=list)


@dataclass
class BudgetForm:
    infinite: bool = False
    selected: int = 0
    fields: list[str] = field(default_factory=lambda: ["0", "0", "0"])


@dataclass
class ModalState:
    kind: str = "detail"  # detail | findings | agents | sessions | budget | compose
    title: str = ""
    lines: list[str] = field(default_factory=list)
    wrapped: list[str] = field(default_factory=list)
    wrap_width: int = 0
    scroll: int = 0
    err: str = ""
    findings: FindingsModal | None = None
    sessions: SessionsModal | None = None
    agent_state: AgentsModal | None = None
    budget_form: BudgetForm | None = None
    budget: bool = False
    compose: bool = False
    compose_text: str = ""
    agent_index: int = 0
    agent_details: bool = False
    agent_starts: list[int] = field(default_factory=list)


@dataclass
class OperationEvent:
    at: str
    agent_id: str
    content: str


@dataclass
class ForumHeader:
    id: int
    start: int
    end: int
    body_end: int = 0


@dataclass
class ForumCache:
    width: int = 0
    revision: int = -1
    thread_id: int = 0
    lines: list[str] = field(default_factory=list)
    headers: list[ForumHeader] = field(default_factory=list)


@dataclass
class UiState:
    width: int = 120
    height: int = 40
    auto_dir: str = ""
    session_dir: str = ""
    session_path: str = ""
    workspace: str = ""
    last_query: str = ""
    pending_dir: str = ""
    workspace_ready: bool = False
    busy: bool = False
    stopping: bool = False
    quitting: bool = False
    saving: bool = False
    escape_armed: bool = False
    session: int = 0
    phase: str = "recon"
    workers: list[WorkerView] = field(default_factory=list)
    forum_page: PageView = field(default_factory=PageView)
    forum_search: str = ""
    forum_dirty: bool = False
    forum_cache: ForumCache = field(default_factory=ForumCache)
    forum_revision: int = 0
    thread_id: int = 0
    selected_post: int = 0
    expanded_posts: set[int] = field(default_factory=set)
    snapshot: SnapshotView = field(default_factory=SnapshotView)
    events: list[OperationEvent] = field(default_factory=list)
    modal: ModalState | None = None
    focus: int = 1
    scroll: list[int] = field(default_factory=lambda: [0, 0])
    follow: list[bool] = field(default_factory=lambda: [True, True])
    final_save_pending: bool = False
    budget: BudgetView = field(default_factory=BudgetView)
    budget_cfg: BudgetView = field(default_factory=BudgetView)
    bottom_key: str = ""
    bottom_at: float = 0.0
    bottom_modal: ModalState | None = None
    bottom_focus: int = 0

    # -- derived helpers -------------------------------------------------
    def layout(self) -> Layout:
        return compute_layout(self.width, self.height)

    def phase_text(self) -> str:
        return phase_label(self.phase)

    def header_state(self) -> str:
        state = self.phase_text()
        if self.stopping:
            state += " / 正在停止"
        elif self.busy:
            state += " / 运行中"
        if self.saving:
            state += " / 保存中"
        return state

    def agent_label(self, agent_id: str, name: str = "") -> str:
        if name:
            return name
        for worker in self.workers:
            if worker.id == agent_id and worker.name:
                return worker.name
        return {
            "": "",
            "user": "用户",
            "coordinator": "协调器",
            "system": "系统",
        }.get(agent_id, "未命名成员")

    def add_event(self, content: str, agent_id: str = "") -> None:
        entry = OperationEvent(
            at=now_clock(), agent_id=agent_id, content=bounded_text(content, 1000)
        )
        if len(self.events) >= MAX_UI_EVENTS:
            del self.events[0 : len(self.events) - MAX_UI_EVENTS + 1]
        self.events.append(entry)


# ---------------------------------------------------------------------------
# Worker mailbox


@dataclass
class EventView:
    kind: str = ""
    agent_id: str = ""
    phase: str = ""
    content: str = ""
    workers: list[WorkerView] | None = None
    forum: MessageView | None = None
    verify_turn: int = 0
    verify_limit: int = 0
    verify_title: str = ""
    verify_status: str = ""
    snapshot: SnapshotView | None = None


def norm_event(event: Any) -> EventView:
    workers_raw = get_field(event, "workers")
    workers = (
        [norm_worker(item) for item in workers_raw]
        if isinstance(workers_raw, (list, tuple)) and len(workers_raw) > 0
        else None
    )
    forum_raw = get_field(event, "forum")
    kind = _str(get_field(event, "kind"))
    snapshot = None
    if kind == "state":
        snapshot = norm_snapshot(event)
    return EventView(
        kind=kind,
        agent_id=_str(get_field(event, "agent_id")),
        phase=_str(get_field(event, "phase")),
        content=_str(get_field(event, "content")),
        workers=workers,
        forum=norm_message(forum_raw) if forum_raw is not None else None,
        verify_turn=_int(get_field(event, "verify_turn")),
        verify_limit=_int(get_field(event, "verify_limit")),
        verify_title=_str(get_field(event, "verify_title")),
        verify_status=_str(get_field(event, "verify_status")),
        snapshot=snapshot,
    )


class EventMailbox:
    """Bounded, coalescing queue between the engine task and the interface."""

    def __init__(self) -> None:
        self.queue: list[EventView] = []
        self.done = False
        self.failure = ""
        self.notify = None

    def post(self, event: Any) -> None:
        view = event if isinstance(event, EventView) else norm_event(event)
        if view.kind not in OPERATIONAL_KINDS:
            return
        if view.content:
            view.content = bounded_text(view.content, 1000)
        if self.done:
            return
        if view.kind in ("state", "model_progress"):
            for index in range(len(self.queue) - 1, -1, -1):
                if self.queue[index].kind == view.kind:
                    del self.queue[index]
                    break
        if len(self.queue) >= 256:
            del self.queue[0]
        self.queue.append(view)
        if self.notify is not None:
            self.notify()

    def finish(self, failure: str = "") -> None:
        if not self.done:
            self.done = True
            self.failure = failure

    def drain(self) -> tuple[list[EventView], bool, str]:
        events, self.queue = self.queue, []
        return events, self.done, self.failure


def monotonic() -> float:
    return time.monotonic()


def apply_event(state: UiState, event: EventView) -> None:
    """Apply one engine event to the interface state."""
    if event.kind == "state" and event.phase:
        state.phase = event.phase
    if event.workers:
        state.workers = event.workers
        if state.modal is not None and state.modal.kind == "agents":
            from drx_agent.code_audit.tui import agents as agents_mod

            agents_mod.refresh_agents_modal(state)
    if event.kind == "state" and event.snapshot is not None:
        state.snapshot = event.snapshot
        if event.phase:
            state.phase = event.phase
    elif event.kind == "forum":
        if event.forum is not None:
            state.forum_dirty = True
    elif event.kind == "name":
        state.forum_dirty = True
    elif event.kind == "verify_progress":
        state.add_event(
            f"复核 {bounded_text(event.verify_title, 120)} · "
            f"{event.verify_turn}/{event.verify_limit}",
            event.agent_id,
        )
    elif event.kind == "verify_done":
        state.add_event("复核结束", event.agent_id)
    elif event.kind in ("worker", "waiting", "tool", "info", "error"):
        if event.content:
            state.add_event(event.content, event.agent_id)
