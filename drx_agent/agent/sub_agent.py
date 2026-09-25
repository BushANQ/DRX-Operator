"""SubAgent: a self-contained ReAct loop with isolated message history.

Receives a task from the master, runs its own LLM + tool loop using the
parent's tool executor, publishes SUB_AGENT_DISPATCH / SUB_AGENT_RESULT."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from drx_agent.agent.steering import MessageInterrupt, MessageSignal
from drx_agent.agent.execution_context import advance_turn, execution_context, execution_scope, worker_context
from drx_agent.event_bus import Activity, Event, EventBus, EventType, activity_model_stream

logger = logging.getLogger(__name__)


class SubAgentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class SubAgentResult:
    agent_id: str
    status: SubAgentStatus
    findings: list = field(default_factory=list)
    scripts_executed: int = 0
    new_targets: list = field(default_factory=list)
    error: str = ""
    text: str = ""


ToolExecutor = Callable[[str, dict], Awaitable[str]]


async def _wait_owned(awaitable: Awaitable, timeout: float | None):
    """Bound owned work, then join its cleanup even across repeated cancellation."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            drain = asyncio.gather(task, return_exceptions=True)
            cancelled = False
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        elif not task.cancelled():
            task.exception()


class SubAgent:
    """A self-contained ReAct loop with isolated message history."""

    def __init__(
        self,
        agent_type: str,
        target: str,
        task: str,
        event_bus: EventBus,
        llm_provider: Any = None,
        tool_executor: Optional[ToolExecutor] = None,
        tool_schemas: Optional[list[dict]] = None,
        system_prompt: str = "",
        ttl: float = 300,
        max_iterations: int = 12,
        parallel_tool_calls: bool = True,
        usage_callback: Optional[Callable[..., None]] = None,
        llm_call_timeout: float = 600.0,
        notification_provider: Optional[Callable[[], str]] = None,
        stop_on_pattern: Optional[re.Pattern] = None,
    ) -> None:
        self.agent_id = f"{agent_type}-{uuid.uuid4().hex[:12]}"
        self.agent_type = agent_type
        self.target = target
        self.task = task
        self.event_bus = event_bus
        self.llm_provider = llm_provider
        self.tool_executor = tool_executor
        self.tool_schemas = tool_schemas or []
        self.system_prompt = system_prompt
        self.ttl = ttl
        self.max_iterations = max_iterations
        self.parallel_tool_calls = parallel_tool_calls
        self.usage_callback = usage_callback
        self.llm_call_timeout = llm_call_timeout
        self.notification_provider = notification_provider
        self.stop_on_pattern = stop_on_pattern
        self.status = SubAgentStatus.QUEUED
        self._interrupt = False
        self.messages: list[dict] = []
        self.message_signal = MessageSignal()
        self.activation = 0
        self.started_at: float | None = None
        self.last_activity_at: float | None = None
        self._activity: Activity | None = None
        self.execution_task_id: str | None = None
        self._execution_parent = execution_context()
        self._execution_capture: dict = {}
        self._execution_previous_turn_id: str | None = None
        self._capture_queued = False
        self._capture_started_at: float | None = None

    def _begin_execution_capture(self) -> None:
        parent = execution_context()
        if not parent.get("run_id") and self.activation == 0:
            parent = {**self._execution_parent, **parent}
        self._execution_parent = deepcopy(parent)
        self._execution_capture = worker_context(parent, self.agent_id, self.execution_task_id)
        self._capture_started_at = None

    def queue(self) -> None:
        self._begin_execution_capture()
        self._capture_queued = True
        self._activity = Activity(
            self.event_bus, "worker", f"{self.agent_type} · {self.target}",
            agent_id=self.agent_id, state="queued",
        )
        self._publish_dispatch()

    def _publish_dispatch(self) -> None:
        self.event_bus.publish(Event(EventType.SUB_AGENT_DISPATCH, {
            "agent_id": self.agent_id, "type": self.agent_type, "role": self.agent_type,
            "target": self.target, "task": self.task, "status": self.status.value,
            "text": "", "error": "",
            "dispatch_id": self._execution_capture.get("dispatch_id"),
            "execution_context": deepcopy(self._execution_capture),
            "started_at": self._capture_started_at,
        }))

    def publish_result(self, result: SubAgentResult) -> None:
        self.event_bus.publish(Event(EventType.SUB_AGENT_RESULT, {
            "agent_id": self.agent_id, "type": self.agent_type, "role": self.agent_type,
            "target": self.target, "task": self.task, "status": result.status.value,
            "scripts_executed": result.scripts_executed,
            "text": result.text, "error": result.error,
            "dispatch_id": self._execution_capture.get("dispatch_id"),
            "execution_context": deepcopy(self._execution_capture),
            "started_at": self._capture_started_at, "completed_at": time.time(),
        }))

    def request_stop(self) -> None:
        """Cooperative stop; the owning task should also be cancelled."""
        self._interrupt = True
        if self._activity is not None:
            self._activity.update("stopping")

    def request_message(self) -> None:
        """Wake an active phase without changing explicit stop state."""
        self.message_signal.notify()

    @staticmethod
    def _cancelled_call(call: dict) -> dict:
        return {
            "role": "tool", "tool_call_id": call["id"],
            "name": call["function"]["name"],
            "content": json.dumps({"error": "Tool interrupted", "status": "cancelled"}),
        }

    @staticmethod
    def _validate_messages(messages: Any, *, allow_pending: bool = False) -> dict:
        if not isinstance(messages, list):
            raise ValueError("resident messages must be a list")
        pending: dict[str, dict] = {}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValueError("resident message must be an object")
            role = message.get("role")
            if not isinstance(role, str) or role not in {"system", "user", "assistant", "tool"}:
                raise ValueError("invalid resident message role")
            if (index == 0) != (role == "system"):
                raise ValueError("resident history must have one leading system prompt")
            content = message.get("content")
            if "content" not in message:
                raise ValueError("resident message content is missing")
            if not isinstance(content, (str, list)) and not (role == "assistant" and content is None):
                raise ValueError("invalid resident message content")
            if isinstance(content, list) and any(not isinstance(part, dict) for part in content):
                raise ValueError("invalid resident content blocks")
            if role == "system" and not isinstance(content, str):
                raise ValueError("invalid resident system prompt")
            if pending and role != "tool":
                raise ValueError("resident tool calls are not paired")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ValueError("unexpected resident tool result")
                call = pending.pop(call_id)
                if "name" in message and message["name"] != call["function"]["name"]:
                    raise ValueError("resident tool result name does not match")
            elif "tool_call_id" in message:
                raise ValueError("tool result identity on non-tool message")
            if "tool_calls" in message:
                calls = message["tool_calls"]
                if role != "assistant" or not isinstance(calls, list):
                    raise ValueError("invalid resident tool calls")
                for call in calls:
                    if not isinstance(call, dict):
                        raise ValueError("invalid resident tool call")
                    call_id, function = call.get("id"), call.get("function")
                    if not isinstance(call_id, str) or not call_id or call_id in pending:
                        raise ValueError("invalid or duplicate resident tool call identity")
                    if call.get("type") != "function" or not isinstance(function, dict):
                        raise ValueError("invalid resident tool function")
                    if not isinstance(function.get("name"), str) or not function["name"]:
                        raise ValueError("invalid resident tool name")
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        raise ValueError("invalid resident tool arguments")
                    try:
                        parsed = json.loads(arguments)
                    except (ValueError, TypeError) as exc:
                        raise ValueError("invalid resident tool arguments") from exc
                    if not isinstance(parsed, dict):
                        raise ValueError("resident tool arguments must be an object")
                    pending[call_id] = call
        if pending and not allow_pending:
            raise ValueError("resident tool calls are not paired")
        return pending

    @staticmethod
    def validate_runtime(data: Any) -> dict:
        """Validate persisted private context, returning a detached JSON value."""
        if not isinstance(data, dict):
            raise ValueError("resident runtime must be an object")
        if type(data.get("activation")) is not int or data["activation"] < 0:
            raise ValueError("invalid resident activation")
        if type(data.get("stopped")) is not bool:
            raise ValueError("invalid resident stopped flag")
        try:
            detached = json.loads(json.dumps(data, allow_nan=False))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("resident runtime must contain JSON values") from exc
        SubAgent._validate_messages(detached.get("messages"))
        messages = detached["messages"]
        if detached["activation"] and (len(messages) < 2 or messages[1]["role"] != "user"):
            raise ValueError("activated resident history must retain its initial context")
        if not isinstance(detached.get("execution_capture", {}), dict):
            raise ValueError("resident execution metadata must be an object")
        return {
            "messages": detached["messages"], "activation": detached["activation"],
            "stopped": detached["stopped"],
            "execution_capture": detached.get("execution_capture", {}),
        }

    def snapshot_runtime(self) -> dict:
        """Pair active calls in the snapshot only; restored calls never replay."""
        messages = deepcopy(self.messages)
        pending = self._validate_messages(messages, allow_pending=True)
        messages.extend(self._cancelled_call(call) for call in pending.values())
        return self.validate_runtime({
            "messages": messages, "activation": self.activation, "stopped": self._interrupt,
            "execution_capture": self._execution_capture,
        })

    def _take_notification(self) -> tuple[bool, str]:
        pending = self.message_signal.pending
        self.message_signal.acknowledge()
        note = ""
        if self.notification_provider is not None:
            try:
                note = self.notification_provider() or ""
            except Exception:
                logger.exception("Sub-agent %s notification_provider failed", self.agent_id)
        return pending or bool(note) or self.message_signal.pending, note

    def _append_notification(self, note: str) -> None:
        if note:
            self.messages.append({"role": "user", "content": "<新论坛通知>\n" + note})

    def _retain_assistant(self, supplied: dict | None, text: str) -> None:
        message = deepcopy(supplied) if supplied is not None else {
            "role": "assistant", "content": text,
        }
        message.pop("tool_calls", None)
        if text:
            message["content"] = text
        if supplied is not None or text:
            self.messages.append(message)

    def _mark_cancelled(self, error_seen: str) -> str:
        self.status = SubAgentStatus.CANCELLED
        return error_seen or "interrupted"

    async def run(self) -> SubAgentResult:
        if self.status is SubAgentStatus.RUNNING:
            raise RuntimeError("sub-agent activation is already running")
        if not self._capture_queued:
            self._begin_execution_capture()
        self._capture_queued = False
        self.activation += 1
        if not self.messages:
            self.messages.extend([
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self.task},
            ])
        self.status = SubAgentStatus.RUNNING
        started = asyncio.get_running_loop().time()
        self.started_at = self.last_activity_at = time.time()
        self._capture_started_at = self.started_at
        if self._activity is None:
            self._activity = Activity(
                self.event_bus, "worker", f"{self.agent_type} · {self.target}",
                agent_id=self.agent_id,
            )
        self._activity.update("running")
        self._publish_dispatch()
        result = SubAgentResult(agent_id=self.agent_id, status=self.status)

        try:
            if self._interrupt:
                result.error = self._mark_cancelled(result.error)
            # No-LLM fallback (used by /scan, /exploit and unit tests).
            elif self.llm_provider is None or self.tool_executor is None:
                _, note = self._take_notification()
                self._append_notification(note)
                self.status = SubAgentStatus.DONE
                result.scripts_executed = 1
            else:
                remaining = (
                    max(0.0, self.ttl - (asyncio.get_running_loop().time() - started))
                    if self.ttl else None
                )
                async def captured_react():
                    with execution_scope(self._execution_capture):
                        await self._react_loop(result)
                await _wait_owned(captured_react(), remaining)
        except asyncio.TimeoutError:
            self.status = SubAgentStatus.TIMEOUT
            result.error = f"ttl ({self.ttl}s) exceeded"
        except asyncio.CancelledError:
            self._interrupt = True
            result.error = self._mark_cancelled(result.error)
        except Exception as exc:
            self.status = SubAgentStatus.ERROR
            result.error = str(exc)
            raise
        finally:
            if self.status == SubAgentStatus.RUNNING:
                self.status = SubAgentStatus.DONE
            result.status = self.status
            self.publish_result(result)
            self._activity.update(
                "done" if self.status is SubAgentStatus.DONE else
                "cancelled" if self.status is SubAgentStatus.CANCELLED else "error",
                output=bool(result.text or result.error),
            )
        return result

    async def _react_loop(self, result: SubAgentResult) -> None:
        messages = self.messages

        for _iteration in range(self.max_iterations):
            if self._interrupt:
                result.error = self._mark_cancelled(result.error)
                break

            _, note = self._take_notification()
            self._append_notification(note)

            text_parts: list[str] = []
            pending_calls: list[dict] = []
            assistant_msg: Optional[dict] = None
            saw_error = False
            model_finished = False
            interrupted = False
            self._execution_previous_turn_id = advance_turn(self.agent_id, self._execution_previous_turn_id)
            self._execution_capture = execution_context()

            try:
                async def _consume():
                    nonlocal assistant_msg, saw_error
                    async with aclosing(activity_model_stream(
                        self.event_bus, self.llm_provider, messages,
                        label=f"Model · {self.agent_type}", agent_id=self.agent_id,
                        tools=self.tool_schemas, stream=False,
                    )) as stream:
                        async for ev in stream:
                            self.last_activity_at = time.time()
                            if self._interrupt:
                                result.error = self._mark_cancelled(result.error)
                                break
                            kind = getattr(ev, "type", None)
                            kind_value = kind.value if hasattr(kind, "value") else kind
                            if kind_value == "text" and ev.content:
                                text_parts.append(ev.content)
                            elif kind_value == "tool_call":
                                pending_calls.append({
                                    "id": (ev.metadata or {}).get("tool_call_id", ""),
                                    "name": ev.tool_name,
                                    "input": ev.tool_input or {},
                                })
                            elif kind_value in {"done", "error"}:
                                meta = ev.metadata or {}
                                assistant_msg = meta.get("assistant_message")
                                if self.usage_callback is not None:
                                    try:
                                        self.usage_callback(
                                            meta.get("usage"), meta.get("model"),
                                            actor=self.agent_type, provider=meta.get("provider"),
                                        )
                                    except Exception:
                                        logger.exception("sub-agent usage_callback failed")
                                if kind_value == "error":
                                    result.error = ev.content or "unknown LLM error"
                                    saw_error = True
                                break
                await self.message_signal.run(_consume(), timeout=self.llm_call_timeout)
                model_finished = True
            except MessageInterrupt:
                interrupted = True
            except asyncio.TimeoutError:
                logger.error(
                    "Sub-agent %s LLM call timed out after %ss",
                    self.agent_id, self.llm_call_timeout,
                )
                result.error = f"LLM call timed out after {self.llm_call_timeout}s"
                saw_error = True
            except Exception as exc:
                logger.exception("Sub-agent %s LLM call failed", self.agent_id)
                result.error = str(exc)
                saw_error = True
            finally:
                text_now = "".join(text_parts)
                if text_now:
                    result.text = text_now
                if not model_finished:
                    self._retain_assistant(assistant_msg, text_now)

            if self._interrupt:
                result.error = self._mark_cancelled(result.error)
            if self.status is SubAgentStatus.CANCELLED:
                if model_finished:
                    self._retain_assistant(assistant_msg, text_now)
                break
            if saw_error:
                if model_finished:
                    self._retain_assistant(assistant_msg, text_now)
                self.status = SubAgentStatus.ERROR
                break
            if interrupted:
                continue

            # Leave late mail unread if this activation cannot process another turn.
            incoming, note = (
                self._take_notification() if _iteration + 1 < self.max_iterations
                else (self.message_signal.pending, "")
            )
            if incoming:
                self._retain_assistant(assistant_msg, text_now)
                self._append_notification(note)
                continue
            if not pending_calls:
                self._retain_assistant(assistant_msg, text_now)
                break

            for call in pending_calls:
                call["id"] = call["id"] or f"call_{uuid.uuid4().hex}"
            assistant_msg = dict(assistant_msg or {"role": "assistant", "content": text_now})
            # The advertised calls must match exactly the calls we execute,
            # including fallback IDs absent from provider-supplied messages.
            assistant_msg["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["input"], ensure_ascii=False),
                    },
                }
                for c in pending_calls
            ]
            messages.append(assistant_msg)
            tool_start = len(messages)
            try:
                await self.message_signal.run(self._run_tool_calls(pending_calls, messages, result))
            except MessageInterrupt:
                continue
            if self._interrupt:
                result.error = self._mark_cancelled(result.error)
                break
            incoming, note = (
                self._take_notification() if _iteration + 1 < self.max_iterations
                else (self.message_signal.pending, "")
            )
            if incoming:
                self._append_notification(note)
                continue

            if self.stop_on_pattern is not None:
                tool_texts = [
                    str(m.get("content") or "")
                    for m in messages[tool_start:]
                    if m.get("role") == "tool"
                ]
                combined = text_now + "\n" + "\n".join(tool_texts)
                match = self.stop_on_pattern.search(combined)
                if match:
                    token = match.group(0)
                    if token not in result.text:
                        result.text = f"{result.text}\n{token}".strip()
                    break

    async def _run_tool_calls(
        self, pending_calls: list[dict], messages: list[dict], result: SubAgentResult
    ) -> None:
        executor = self.tool_executor
        if executor is None:
            return
        completed: set[str] = set()

        async def execute(call: dict):
            if self._interrupt or self.message_signal.pending:
                return
            try:
                try:
                    with execution_scope(tool_call_id=call.get("id")):
                        output = await executor(call["name"], call["input"])
                except Exception as exc:
                    output = json.dumps(
                        {"error": f"tool raised: {exc}"}, ensure_ascii=False
                    )
                completed.add(call["id"])
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "name": call["name"], "content": output,
                })
                result.scripts_executed += 1
            finally:
                self.last_activity_at = time.time()

        try:
            if self.parallel_tool_calls and len(pending_calls) > 1:
                await asyncio.gather(
                    *(execute(call) for call in pending_calls),
                    return_exceptions=True,
                )
            else:
                for call in pending_calls:
                    if self._interrupt:
                        result.error = self._mark_cancelled(result.error)
                        break
                    if self.message_signal.pending:
                        break
                    await execute(call)
        finally:
            # The phase owner joins every child before this pairing is exposed.
            # Finished outputs are already in history, including active snapshots.
            for call in pending_calls:
                if call["id"] not in completed:
                    messages.append(self._cancelled_call({
                        "id": call["id"], "function": {"name": call["name"]},
                    }))

