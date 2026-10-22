"""Session persistence: immutable checkpoints saved as one JSON document."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .events import BudgetStatus, IRCMessage, WorkerStatus, utc_now_iso
from .irc import validate_irc_session
from ..text import sanitize_data


@dataclass
class WorkerSession:
    status: WorkerStatus = field(default_factory=WorkerStatus)
    assignment: str = ""
    messages: list[Any] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    trace_path: str = ""
    snapshot: Any = None
    plan: dict[str, Any] | None = None
    moderator_suggestions: list[str] = field(default_factory=list)
    completed: bool = False
    forum_cursor: int = 0
    forum_pending: str = ""
    announced_name: str = ""
    user_broadcast_cursor: int = 0
    user_broadcast_delivered: int = 0


@dataclass
class TeamSession:
    version: int = 2
    saved_at: str = ""
    workspace: str = ""
    phase: str = ""
    input: str = ""
    recon_agents: int = 0
    audit_agents: int = 0
    workers: list[WorkerSession] = field(default_factory=list)
    forum: list[Any] = field(default_factory=list)
    forum_names: dict[str, str] = field(default_factory=dict)
    forum_participants: dict[str, dict[str, int]] = field(default_factory=dict)
    moderator: WorkerSession | None = None
    revocations: list[dict[str, Any]] = field(default_factory=list)
    pending_assignments: dict[str, list[str]] = field(default_factory=dict)
    irc: list[IRCMessage] = field(default_factory=list)
    irc_next_id: int = 0
    budget: BudgetStatus | None = None
    user_broadcasts: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Generic dataclass serialization
# ---------------------------------------------------------------------------

def _to_dict(value: Any) -> Any:
    if value is None:
        return None
    if dataclasses.is_dataclass(value):
        return {item.name: _to_dict(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_dict(item) for key, item in value.items()}
    return value


def _message_to_dict(message: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name in ("role", "content", "type", "call_id", "name", "arguments", "id"):
        value = getattr(message, name, None)
        if value:
            data[name] = value
    return data


def _message_from_dict(data: dict[str, Any]) -> Any:
    from ..llm import Message

    return Message(
        role=data.get("role", "") or "",
        content=data.get("content", "") or "",
        type=data.get("type", "") or "",
        call_id=data.get("call_id", "") or "",
        name=data.get("name", "") or "",
        arguments=data.get("arguments", "") or "",
        id=data.get("id", "") or "",
    )


def snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    return _to_dict(snapshot)


def snapshot_from_dict(data: dict[str, Any] | None) -> Any:
    from ..tools import Snapshot

    if not data:
        return Snapshot()
    try:
        return Snapshot(
            todos=[_todo(item) for item in data.get("todos", [])],
            findings=[_finding(item) for item in data.get("findings", [])],
            project_note=_project(data.get("project_note") or data.get("project") or {}),
            files=[_file(item) for item in data.get("files", [])],
            variables=[_variable(item) for item in data.get("variables", [])],
            flows=[_flow(item) for item in data.get("flows", [])],
            audit=_audit(data.get("audit") or {}),
        )
    except TypeError:  # pragma: no cover - tolerant fallback
        return Snapshot(**data)


def _todo(data: dict[str, Any]):
    from ..tools import Todo

    return _construct(Todo, data)


def _finding(data: dict[str, Any]):
    from ..tools import Finding

    return _construct(Finding, data)


def _project(data: dict[str, Any]):
    from ..tools import ProjectNote

    return _construct(ProjectNote, data)


def _file(data: dict[str, Any]):
    from ..tools import FileReview

    return _construct(FileReview, data)


def _variable(data: dict[str, Any]):
    from ..tools import VariableReview

    return _construct(VariableReview, data)


def _flow(data: dict[str, Any]):
    from ..tools import FlowReview

    return _construct(FlowReview, data)


def _audit(data: dict[str, Any]):
    from ..tools import AuditState

    return _construct(AuditState, data)


def _construct(cls, data: dict[str, Any]):
    if not isinstance(data, dict):
        return cls()
    names = {item.name for item in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()
    kwargs = {key: value for key, value in data.items() if key in names}
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Worker session conversion
# ---------------------------------------------------------------------------

def worker_session_to_dict(session: WorkerSession) -> dict[str, Any]:
    data = {
        "status": _to_dict(session.status),
        "assignment": session.assignment,
        "messages": [_message_to_dict(message) for message in session.messages],
        "snapshot": snapshot_to_dict(session.snapshot),
        "completed": session.completed,
    }
    if session.skills:
        data["skills"] = list(session.skills)
    if session.trace_path:
        data["trace_path"] = session.trace_path
    if session.plan is not None:
        data["plan"] = session.plan
    if session.moderator_suggestions:
        data["moderator_suggestions"] = list(session.moderator_suggestions)
    if session.forum_cursor:
        data["forum_cursor"] = session.forum_cursor
    if session.forum_pending:
        data["forum_pending"] = session.forum_pending
    if session.announced_name:
        data["announced_name"] = session.announced_name
    if session.user_broadcast_cursor:
        data["user_broadcast_cursor"] = session.user_broadcast_cursor
    if session.user_broadcast_delivered:
        data["user_broadcast_delivered"] = session.user_broadcast_delivered
    return data


def worker_session_from_dict(data: dict[str, Any]) -> WorkerSession:
    status_data = data.get("status") or {}
    status = _construct(WorkerStatus, status_data)
    return WorkerSession(
        status=status,
        assignment=str(data.get("assignment") or ""),
        messages=[_message_from_dict(item) for item in data.get("messages", [])],
        skills=[str(item) for item in data.get("skills", []) or []],
        trace_path=str(data.get("trace_path") or ""),
        snapshot=snapshot_from_dict(data.get("snapshot")),
        plan=data.get("plan"),
        moderator_suggestions=[str(item) for item in data.get("moderator_suggestions", []) or []],
        completed=bool(data.get("completed")),
        forum_cursor=int(data.get("forum_cursor") or 0),
        forum_pending=str(data.get("forum_pending") or ""),
        announced_name=str(data.get("announced_name") or ""),
        user_broadcast_cursor=int(data.get("user_broadcast_cursor") or 0),
        user_broadcast_delivered=int(data.get("user_broadcast_delivered") or 0),
    )


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------

def save_session(team: Any, path: str) -> None:
    budget = team.budget_status()
    budget.running = False
    session = TeamSession(
        version=2,
        saved_at=utc_now_iso(),
        workspace=str(team.registry.workspace),
        phase=team._phase,
        input=team.input,
        recon_agents=int(team.cfg.agent.recon_agents),
        audit_agents=int(team.cfg.agent.audit_agents),
        workers=[worker.saved for worker in team.workers],
        forum=list(team.board.messages()),
        forum_names=dict(team.board.names()) if hasattr(team.board, "names") else {},
        forum_participants=(
            dict(team.board.participants()) if hasattr(team.board, "participants") else {}
        ),
        moderator=team.moderator.saved if team.moderator is not None else None,
        revocations=list(team.revocations),
        pending_assignments={key: list(value) for key, value in team.pending_assignments.items()},
        irc=list(team.irc_messages),
        irc_next_id=team.irc_next_id,
        budget=budget,
        user_broadcasts=[
            {"id": broadcast.id, "content": broadcast.content} for broadcast in team.user_broadcasts
        ],
    )
    for saved in session.workers:
        saved.status.name = session.forum_names.get(saved.status.id, saved.status.name)
    if session.moderator is not None:
        session.moderator.status.name = "论坛管理员"
    payload = sanitize_data(_session_to_dict(session))
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=".audit-session-", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _session_to_dict(session: TeamSession) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": session.version,
        "saved_at": session.saved_at,
        "workspace": session.workspace,
        "phase": session.phase,
        "input": session.input,
        "recon_agents": session.recon_agents,
        "audit_agents": session.audit_agents,
        "workers": [worker_session_to_dict(worker) for worker in session.workers],
        "forum": [_to_dict(message) for message in session.forum],
    }
    if session.forum_names:
        data["forum_names"] = session.forum_names
    if session.forum_participants:
        data["forum_participants"] = session.forum_participants
    if session.moderator is not None:
        data["moderator"] = worker_session_to_dict(session.moderator)
    if session.revocations:
        data["revocations"] = session.revocations
    if session.pending_assignments:
        data["pending_assignments"] = session.pending_assignments
    if session.irc:
        data["irc"] = [message.to_dict() for message in session.irc]
    if session.irc_next_id:
        data["irc_next_id"] = session.irc_next_id
    if session.budget is not None:
        data["budget"] = _to_dict(session.budget)
    if session.user_broadcasts:
        data["user_broadcasts"] = session.user_broadcasts
    return data


def _session_from_dict(data: dict[str, Any]) -> TeamSession:
    from ..forum import Message

    forum = [
        _construct_message(Message, item) for item in data.get("forum", []) or []
    ]
    return TeamSession(
        version=int(data.get("version") or 0),
        saved_at=str(data.get("saved_at") or ""),
        workspace=str(data.get("workspace") or ""),
        phase=str(data.get("phase") or ""),
        input=str(data.get("input") or ""),
        recon_agents=int(data.get("recon_agents") or 0),
        audit_agents=int(data.get("audit_agents") or 0),
        workers=[worker_session_from_dict(item) for item in data.get("workers", []) or []],
        forum=forum,
        forum_names={
            str(key): str(value) for key, value in (data.get("forum_names") or {}).items()
        },
        forum_participants={
            str(key): {str(inner): int(value) for inner, value in (mapping or {}).items()}
            for key, mapping in (data.get("forum_participants") or {}).items()
        },
        moderator=(
            worker_session_from_dict(data["moderator"]) if data.get("moderator") else None
        ),
        revocations=list(data.get("revocations") or []),
        pending_assignments={
            str(key): [str(item) for item in value]
            for key, value in (data.get("pending_assignments") or {}).items()
        },
        irc=[IRCMessage.from_dict(item) for item in data.get("irc", []) or []],
        irc_next_id=int(data.get("irc_next_id") or 0),
        budget=(
            _construct(BudgetStatus, data["budget"]) if data.get("budget") else None
        ),
        user_broadcasts=list(data.get("user_broadcasts") or []),
    )


def _construct_message(cls, data: Any):
    if isinstance(data, dict):
        return _construct(cls, data)
    return data


def _legacy_session_from_dict(raw: dict[str, Any]) -> TeamSession:
    """Migrate a version-0 snapshot (workspace + tool snapshot) into version 2.

    The old model transcript is intentionally not cloned into teammates; only
    the structured snapshot is carried over as a completed recon handoff.
    """
    workspace = str(raw.get("workspace") or "")
    if not workspace:
        raise ValueError("不是有效的审计会话")
    snapshot = snapshot_from_dict(raw.get("snapshot") or {})
    files = [
        str(getattr(item, "path", "") or "")
        for item in (snapshot.files or [])
        if str(getattr(item, "path", "") or "")
    ]
    project_note = getattr(snapshot, "project_note", None)
    audit_map = str(getattr(project_note, "note", "") or "")
    from .events import WorkerStatus

    return TeamSession(
        version=2,
        saved_at="",
        workspace=workspace,
        phase="audit",
        input="恢复历史审计，独立复核遗留结论并继续未完成范围。",
        recon_agents=4,
        audit_agents=4,
        workers=[
            WorkerSession(
                status=WorkerStatus(
                    id="legacy", name="", phase="recon", status="completed"
                ),
                snapshot=snapshot,
                completed=True,
                plan={
                    "summary": "旧版审计会话迁移",
                    "audit_map": audit_map,
                    "audit_files": files,
                },
            )
        ],
    )


def repair_native_history(messages: list[Any]) -> list[Any]:
    """Restore tool-call pairing for calls interrupted before their result.

    Pausing cancels a worker between recording a native function call and
    writing its result.  A saved history in that state cannot be replayed, so
    append one explicit cancelled result per unresolved call and let the agent
    continue from there.
    """
    from ..llm import Message

    output_ids = {
        getattr(message, "call_id", "") or ""
        for message in messages
        if (getattr(message, "type", "") or "") == "function_call_output"
    }
    repaired: list[Any] = []
    for message in messages:
        repaired.append(message)
        if (getattr(message, "type", "") or "") != "function_call":
            continue
        call_id = getattr(message, "call_id", "") or ""
        if not call_id or call_id in output_ids:
            continue
        repaired.append(
            Message(
                type="function_call_output",
                call_id=call_id,
                content=json.dumps(
                    {
                        "ok": False,
                        "cancelled": True,
                        "error": "会话恢复时补全：上次中断未产生工具执行结果；如需该结果请重新调用工具。",
                    },
                    ensure_ascii=False,
                ),
            )
        )
    return repaired


def load_session(team: Any, path: str) -> None:
    with open(path, "r", encoding="utf-8") as fp:
        raw = json.load(fp)
    raw = sanitize_data(raw)
    if not isinstance(raw, dict):
        raise ValueError("不是有效的审计会话")
    if int(raw.get("version") or 0) == 0:
        session = _legacy_session_from_dict(raw)
    else:
        session = _session_from_dict(raw)
    for worker in list(session.workers) + (
        [session.moderator] if session.moderator is not None else []
    ):
        worker.messages = repair_native_history(worker.messages)
    _validate_loaded_session(team, session, raw)
    _apply_loaded_session(team, session)


def _validate_loaded_session(team: Any, session: TeamSession, raw: dict[str, Any]) -> None:
    if session.version != 2:
        raise ValueError("不支持的会话版本 %d" % session.version)
    if session.phase not in ("recon", "audit", "completed"):
        raise ValueError("无效的会话阶段 %r" % session.phase)
    if not (1 <= session.recon_agents <= 32 and 1 <= session.audit_agents <= 32):
        raise ValueError("无效的会话团队规模")
    for index, broadcast in enumerate(session.user_broadcasts):
        if int(broadcast.get("id") or 0) != index + 1:
            raise ValueError("invalid user broadcast sequence")
        from .broadcast import validate_user_broadcast

        validate_user_broadcast(str(broadcast.get("content") or ""))
    if session.budget is not None:
        if session.budget.used_tokens < 0 or session.budget.elapsed < 0:
            raise ValueError("invalid saved audit budget consumption")
        if session.budget.hours < 0 or session.budget.minutes < 0 or session.budget.token_limit < 0:
            raise ValueError("audit budgets must be nonnegative; 0 means unlimited")
    for record in session.revocations:
        if not record.get("finding_key") or not record.get("reason") or not record.get("evidence"):
            raise ValueError("invalid finding revocation")
    if session.moderator is not None:
        status = session.moderator.status
        if status.id != "moderator" or status.phase != "moderator" or status.name != "论坛管理员":
            raise ValueError("invalid moderator identity")
    for agent_id, assignments in session.pending_assignments.items():
        valid = any(
            worker.status.id == agent_id and worker.status.phase == session.phase
            for worker in session.workers
        )
        if not valid or len(assignments) > 8:
            raise ValueError("invalid pending moderator assignments")
        for content in assignments:
            content = sanitize_data(content)
            if not content or len(content.encode("utf-8")) > 4096:
                raise ValueError("invalid moderator assignment content")
    seen: set[str] = set()
    for worker in session.workers:
        status = worker.status
        if not status.id or status.id in seen or status.phase not in ("recon", "audit"):
            raise ValueError("无效或重复的 worker 身份")
        seen.add(status.id)
        if status.phase == "recon" and worker.completed and worker.plan is None:
            raise ValueError("已完成侦察缺少交接资料")
        if status.phase == "audit" and worker.completed and not getattr(worker.snapshot.audit, "ended", False):
            raise ValueError("已完成审计缺少 end_audit 状态")
    if len(session.workers) > 64:
        raise ValueError("会话 worker 过多")
    validate_irc_session(session)
    all_workers = list(session.workers) + ([session.moderator] if session.moderator else [])
    from .native import validate_native_history

    forum_ids = {message.id for message in session.forum}
    latest = max(forum_ids) if forum_ids else 0
    for saved in all_workers:
        if (
            saved.user_broadcast_cursor < 0
            or saved.user_broadcast_cursor > len(session.user_broadcasts)
            or saved.user_broadcast_delivered < 0
            or saved.user_broadcast_delivered > saved.user_broadcast_cursor
        ):
            raise ValueError("invalid worker user broadcast cursor")
        for broadcast in session.user_broadcasts[: saved.user_broadcast_cursor]:
            from .broadcast import broadcast_message

            canonical = broadcast_message(_Broadcast(broadcast))
            if not any(
                getattr(message, "role", "") == canonical.role
                and getattr(message, "content", "") == canonical.content
                and getattr(message, "type", "") == ""
                for message in saved.messages
            ):
                raise ValueError("full user broadcast missing from worker history")
        try:
            validate_native_history(saved.messages)
        except ValueError as exc:
            raise ValueError("invalid native history for %s: %s" % (saved.status.id, exc)) from exc
        if saved.forum_cursor < 0 or saved.forum_cursor > latest:
            raise ValueError("invalid worker forum cursor")
        if saved.forum_pending and not any(
            getattr(message, "role", "") == "user"
            and getattr(message, "content", "") == saved.forum_pending
            for message in saved.messages
        ):
            raise ValueError("pending forum notification missing from worker history")


class _Broadcast:
    def __init__(self, data: dict[str, Any]) -> None:
        self.id = int(data.get("id") or 0)
        self.content = str(data.get("content") or "")


def _apply_loaded_session(team: Any, session: TeamSession) -> None:
    if team.running:
        raise ValueError("请等待当前审计完全停止再恢复会话")
    from ..tools import Registry

    team.registry.close()
    from .. import tools as tools_pkg

    new_registry = tools_pkg.Registry(session.workspace, team.cfg.agent.max_tool_result_chars)
    team.registry = new_registry
    team.cfg.agent.recon_agents = session.recon_agents
    team.cfg.agent.audit_agents = session.audit_agents
    team.budget = type(team.budget)()
    team.budget.bind_config(team.cfg)
    if session.budget is not None:
        team.cfg.agent.infinite_mode = bool(session.budget.infinite_mode)
        team.cfg.agent.budget_hours = int(session.budget.hours)
        team.cfg.agent.budget_minutes = int(session.budget.minutes)
        team.cfg.agent.budget_tokens = int(session.budget.token_limit)
        team.budget.elapsed = float(session.budget.elapsed)
        team.budget.tokens = int(session.budget.used_tokens)
        team.budget.estimated = bool(session.budget.estimated)
        team.budget.reason = str(session.budget.stop_reason or "")
    team._phase = session.phase
    team.input = session.input
    team.handoff = ""
    team.workers = []
    team.moderator = None
    team.revocations = list(session.revocations)
    team.pending_assignments = {
        key: list(value) for key, value in session.pending_assignments.items()
    }
    team.irc_messages = list(session.irc)
    team.irc_next_id = session.irc_next_id
    team.user_broadcasts = [
        _Broadcast(item) for item in session.user_broadcasts
    ]
    team.irc_changed = __import__("asyncio").Event()
    team.reset_board()
    team.board.restore(session.forum)
    for worker in session.workers:
        team.board.register(worker.status.id, worker.status.phase)
    if session.moderator is not None:
        team.board.register("moderator", "moderator")
    team.board.restore_names(session.forum_names)
    team.board.restore_participants(session.forum_participants)

    all_workers = list(session.workers) + ([session.moderator] if session.moderator else [])
    for saved in all_workers:
        from .core import new_worker
        from .native import validate_native_history

        worker = team.rebuild_worker(saved)
        if saved.status.phase == "moderator":
            team.moderator = worker
        else:
            team.workers.append(worker)
    team.handoff = team.build_handoff()
    for worker in team.workers:
        if worker.saved.status.phase == "audit":
            worker.agent.handoff = team.handoff
    team._snapshot = team.aggregate()
