"""Observation metadata scoped to an actual run, model turn, or invocation."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import uuid


_context: ContextVar[dict | None] = ContextVar("execution_observation", default=None)
_live_invocations: set[str] = set()


def execution_context() -> dict:
    return deepcopy(_context.get() or {})


def set_execution_context(value: dict) -> None:
    _context.set(deepcopy(value))


@contextmanager
def invocation_scope(invocation_id: str):
    _live_invocations.add(invocation_id)
    try:
        yield
    finally:
        _live_invocations.discard(invocation_id)


@contextmanager
def execution_scope(value: dict | None = None, **updates):
    current = execution_context() if value is None else deepcopy(value)
    current.update(updates)
    token = _context.set(current)
    try:
        yield current
    finally:
        _context.reset(token)


def run_context(actor: str, run_id: str, request_id: str | None) -> dict:
    return {
        "version": 1, "run_id": run_id, "request_id": request_id, "actor": actor,
        "turn_id": None, "previous_turn_id": None, "dispatch_id": None,
        "parent_invocation_id": None, "parent_tool_call_id": None,
        "task_id": None, "invocation_id": None, "tool_call_id": None,
    }


def advance_turn(actor: str, previous_turn_id: str | None = None) -> str:
    current = execution_context()
    previous = previous_turn_id or (current.get("turn_id") if current.get("actor") == actor else None)
    turn_id = uuid.uuid4().hex
    current.update(version=1, actor=actor, turn_id=turn_id,
                   previous_turn_id=previous, invocation_id=None, tool_call_id=None)
    set_execution_context(current)
    return turn_id


def worker_context(parent: dict, actor: str, task_id: str | None = None) -> dict:
    current = run_context(actor, parent.get("run_id") or uuid.uuid4().hex, parent.get("request_id"))
    invocation_id = parent.get("invocation_id")
    live_parent = isinstance(invocation_id, str) and invocation_id in _live_invocations
    current.update(dispatch_id=uuid.uuid4().hex, task_id=task_id,
                   parent_invocation_id=invocation_id if live_parent else None,
                   parent_tool_call_id=parent.get("tool_call_id") if live_parent else None,
                   parent_actor=parent.get("actor"), parent_turn_id=parent.get("turn_id"),
                   stage=parent.get("stage"))
    return current


def tool_context(actor: str, invocation_id: str, *, stage=None, todos=None, intent_id=None) -> dict:
    current = execution_context()
    current.setdefault("version", 1)
    for field in ("run_id", "request_id", "turn_id", "previous_turn_id", "dispatch_id",
                  "parent_invocation_id", "parent_tool_call_id", "tool_call_id", "task_id"):
        current.setdefault(field, None)
    active = [deepcopy(item) for item in (todos or [])
              if isinstance(item, dict) and item.get("status") == "in_progress"]
    current.update(actor=actor, invocation_id=invocation_id, stage=stage,
                   active_tasks=active,
                   active_task=active[0] if actor == "master" and len(active) == 1 else None)
    if actor == "master" and intent_id:
        current["task_id"] = intent_id
    return current
