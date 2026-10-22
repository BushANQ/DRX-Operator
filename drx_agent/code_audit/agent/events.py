"""Shared event and status types for the audit engine.

The dataclasses here are the boundary between the engine and the terminal
interface.  Values mirror the audit state that the UI renders: worker
progress, budget accounting, and the inter-agent direct message queue.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from ..text import sanitize_text

phase_recon = "recon"
phase_audit = "audit"
phase_moderator = "moderator"
phase_completed = "completed"


@dataclass
class Event:
    """One engine notification."""

    kind: str
    agent_id: str = ""
    phase: str = ""
    content: str = ""
    workers: list["WorkerStatus"] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    todos: Any = None
    findings: Any = None
    project: Any = None
    files: Any = None
    variables: Any = None
    flows: Any = None
    audit: Any = None
    forum: Any = None
    generation: Any = None
    verify_turn: int = 0
    verify_limit: int = 0
    verify_title: str = ""
    verify_status: str = ""


@dataclass
class WorkerStatus:
    """Immutable view of one worker's progress."""

    id: str = ""
    name: str = ""
    phase: str = ""
    status: str = "pending"
    activity: str = ""
    turn: int = 0
    last_model_activity: str = ""
    last_tool_activity: str = ""
    generation: Any = None
    last_tool: str = ""
    output_excerpt: str = ""
    output_partial: bool = False


@dataclass
class BudgetStatus:
    """Task-scoped budget view.  Pausing stops the clock, not the usage."""

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
class IRCMessage:
    """A moderator-to-worker direct message and its delivery state."""

    message_id: int = 0
    change_id: int = 0
    agent_id: str = ""
    stage: str = ""
    content: str = ""
    state: str = "queued"
    created_at: str = ""
    delivered_at: str = ""
    replied_at: str = ""
    reply: str = ""
    forum_id: int = 0
    reply_forum_id: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "message_id": self.message_id,
            "change_id": self.change_id,
            "agent_id": self.agent_id,
            "stage": self.stage,
            "content": self.content,
            "state": self.state,
            "created_at": self.created_at,
        }
        for key, value in (
            ("delivered_at", self.delivered_at),
            ("replied_at", self.replied_at),
            ("reply", self.reply),
            ("forum_id", self.forum_id),
            ("reply_forum_id", self.reply_forum_id),
            ("error", self.error),
        ):
            if value:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IRCMessage":
        if not isinstance(data, dict):
            raise ValueError("invalid IRC message")
        return cls(
            message_id=int(data.get("message_id") or 0),
            change_id=int(data.get("change_id") or 0),
            agent_id=str(data.get("agent_id") or ""),
            stage=str(data.get("stage") or ""),
            content=str(data.get("content") or ""),
            state=str(data.get("state") or ""),
            created_at=str(data.get("created_at") or ""),
            delivered_at=str(data.get("delivered_at") or ""),
            replied_at=str(data.get("replied_at") or ""),
            reply=str(data.get("reply") or ""),
            forum_id=int(data.get("forum_id") or 0),
            reply_forum_id=int(data.get("reply_forum_id") or 0),
            error=str(data.get("error") or ""),
        )


Emit = Callable[[Event], None]
T = TypeVar("T")


async def maybe_await(value: T | Awaitable[T]) -> T:
    """Await *value* when it is awaitable, otherwise return it unchanged."""

    if inspect.isawaitable(value):
        return await value  # type: ignore[return-value]
    return value  # type: ignore[return-value]


def short_text(text: str, limit: int) -> str:
    """Byte-bounded prefix with a UTF-8 rune-safe cut and an ellipsis."""

    if limit <= 0:
        return ""
    text = sanitize_text(text)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit]
    while cut:
        try:
            return cut.decode("utf-8") + "…"
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "…"


def bounded_excerpt(text: str, limit: int) -> str:
    """Byte-bounded suffix with a UTF-8 rune-safe cut and no ellipsis."""

    if limit <= 0:
        return ""
    text = sanitize_text(text)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[-limit:]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[1:]
    return ""


def clone_generation(value: Any) -> Any:
    """Copy a generation progress object so checkpoints never alias live state."""

    if value is None:
        return None
    try:
        from dataclasses import replace

        return replace(value)
    except Exception:
        return value


def clone_worker_status(status: WorkerStatus) -> WorkerStatus:
    return WorkerStatus(
        id=status.id,
        name=status.name,
        phase=status.phase,
        status=status.status,
        activity=status.activity,
        turn=status.turn,
        last_model_activity=status.last_model_activity,
        last_tool_activity=status.last_tool_activity,
        generation=clone_generation(status.generation),
        last_tool=status.last_tool,
        output_excerpt=status.output_excerpt,
        output_partial=status.output_partial,
    )


async def gather_cancel(tasks: list[asyncio.Task]) -> None:
    """Cancel tasks and wait until they finish, never raising their errors."""

    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def field_value(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a mapping or a decision dataclass."""

    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def local_now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat()
