"""Lossless, event-backed conversation history shared by every TUI surface."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
import logging
import threading
from typing import Any, Callable

from drx_agent.event_bus import Event, EventBus, EventType


def literal_text(value: Any) -> str:
    """Serialize structured payloads without interpreting terminal markup."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def message_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(
            "[image attached]" if isinstance(block, dict) and block.get("type") == "image_url"
            else literal_text(block.get("text")) if isinstance(block, dict)
            else literal_text(block)
            for block in value
        )
    return literal_text(value)


def tool_summary(tool: str, payload: Any) -> str:
    """Show the operation's useful discriminator, not a clipped JSON blob."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return payload.split("\n", 1)[0][:160]
    if not isinstance(payload, dict):
        return literal_text(payload).split("\n", 1)[0][:160]
    path = payload.get("path") or payload.get("file_path") or payload.get("file")
    if "command" in payload or "cmd" in payload:
        summary = "command · " + str(payload.get("command") or payload.get("cmd"))
    elif "patch" in payload or "edit" in tool or "write" in tool:
        summary = "edit · " + str(path or payload.get("patch") or payload.get("summary") or tool)
    elif path:
        start = payload.get("start_line", payload.get("offset"))
        end = payload.get("end_line")
        span = f":{start}" if start is not None else ""
        if end is not None:
            span += f"-{end}"
        elif payload.get("limit") is not None:
            span += f" +{payload['limit']} lines"
        summary = "path · " + str(path) + span
    elif "task" in payload or "todos" in payload or "task" in tool:
        summary = "task · " + literal_text(payload.get("task", payload.get("todos", payload)))
    else:
        summary = " · ".join(f"{key}={literal_text(value)}" for key, value in payload.items())
    return summary.split("\n", 1)[0][:160]


def record_text(record: dict[str, Any]) -> str:
    kind, actor = record["kind"], record.get("actor", "master")
    data = record.get("data", {})
    if kind == "tool":
        return (f"Tool · {actor} · {record.get('tool', 'tool')} · {record.get('status', '')}\n"
                f"Input:\n{literal_text(record.get('input'))}\nOutput:\n{record.get('output', '')}")
    if kind == "approval":
        return (f"Approval · {actor} · {record.get('status', 'pending')}\n"
                + literal_text(data))
    if kind == "worker":
        return (f"Worker · {actor} · {record.get('status', '')}\n" + literal_text(data))
    label = {"user": "用户", "assistant": "Agent", "system": "系统", "error": "错误"}.get(
        record.get("role", kind), kind)
    return f"{label} · {actor}: {record.get('text', '')}"


class TranscriptLog:
    """Ingest events independently of mounts; callbacks receive a record ID or reset.

    Records are JSON-serializable and replaced (never mutated) on updates. Callbacks
    may run on publishing threads; UI subscribers must enqueue Textual messages.
    """

    EVENT_TYPES = (
        EventType.AGENT_MESSAGE, EventType.TOOL_CALL, EventType.TOOL_RESULT,
        EventType.APPROVAL_REQUEST, EventType.APPROVAL_RESOLVED,
        EventType.SUB_AGENT_DISPATCH, EventType.SUB_AGENT_RESULT, EventType.ERROR,
    )

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._records: list[dict[str, Any]] = []
        self.revision = 0
        self._indices: dict[str, int] = {}
        self._callbacks: list[Callable[[str | None], None]] = []
        self._lock = threading.RLock()
        self._sequence = 0
        self._closed = False
        self._reset_revision = 0
        self._record_revisions: OrderedDict[str, int] = OrderedDict()
        self._generation = 0
        for event_type in self.EVENT_TYPES:
            event_bus.subscribe(event_type, self._ingest)

    def subscribe(self, callback: Callable[[str | None], None]) -> None:
        with self._lock:
            if not self._closed and callback not in self._callbacks:
                self._callbacks.append(callback)

    def unsubscribe(self, callback: Callable[[str | None], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def close(self) -> None:
        for event_type in self.EVENT_TYPES:
            self.event_bus.unsubscribe(event_type, self._ingest)
        with self._lock:
            self._closed = True
            self._callbacks.clear()

    @property
    def records(self) -> list[dict[str, Any]]:
        """Detached history for callers that explicitly need every record."""
        return self.export()

    @property
    def record_count(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def window(self, start: int, limit: int) -> list[dict[str, Any]]:
        """Copy only a bounded history slice, without exposing live records."""
        if start < 0 or limit < 0:
            raise ValueError("Transcript window bounds must be nonnegative")
        with self._lock:
            return deepcopy(self._records[start:start + limit])

    def snapshot(self, since_revision: int = -1) -> tuple[int, bool, list[dict[str, Any]]]:
        """Atomically read changes, or all records after a restore.

        Only the latest version of each changed record is copied, in transcript
        order. The latest-revision index is bounded by records, not streaming tokens.
        """
        with self._lock:
            reset = since_revision < self._reset_revision
            if reset:
                records = self._records
            else:
                indices = []
                for record_id, revision in reversed(self._record_revisions.items()):
                    if revision <= since_revision:
                        break
                    indices.append(self._indices[record_id])
                indices.sort()
                records = [self._records[index] for index in indices]
            return self.revision, reset, deepcopy(records)

    def export(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._records)

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            index = self._indices.get(record_id)
            return deepcopy(self._records[index]) if index is not None else None

    def _get(self, record_id: str) -> dict[str, Any]:
        # Called only while ingest holds the lock; do not copy growing streams.
        index = self._indices.get(record_id)
        return self._records[index] if index is not None else {}

    def _notify(self, record_id: str | None) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            with self._lock:
                if self._closed or callback not in self._callbacks:
                    continue
            try:
                callback(record_id)
            except Exception:
                logging.exception("Transcript listener failed")

    def restore(self, records: list[dict]) -> None:
        if not isinstance(records, list):
            raise ValueError("Transcript records must be a list")
        replacement = deepcopy(records)
        indices: dict[str, int] = {}
        for index, record in enumerate(replacement):
            if not isinstance(record, dict):
                raise ValueError("Each transcript record must be an object")
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id or record_id in indices:
                raise ValueError("Transcript record IDs must be unique nonempty strings")
            if not isinstance(record.get("kind"), str) or record["kind"] not in {
                "message", "tool", "approval", "worker", "error",
            }:
                raise ValueError("Unknown transcript record kind")
            if not isinstance(record.get("actor"), str) or not isinstance(record.get("data", {}), dict):
                raise ValueError("Invalid transcript actor or event data")
            for field in ("text", "output", "status", "tool", "role"):
                if field in record and not isinstance(record[field], str):
                    raise ValueError(f"Transcript {field} must be text")
            indices[record_id] = index
        # Check the entire graph before replacing any live state.
        try:
            json.dumps(replacement, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Transcript records must be JSON serializable") from error
        with self._lock:
            if self._closed:
                return
            self._records = replacement
            self._indices = indices
            self._sequence = 0
            self.revision += 1
            self._generation += 1
            self._reset_revision = self.revision
            self._record_revisions = OrderedDict.fromkeys(indices, self.revision)
        self._notify(None)

    def restore_messages(self, messages: list[dict]) -> None:
        # Import privately so observers see one atomic reset, not partial history.
        if not isinstance(messages, list) or any(
            not isinstance(message, dict) or not isinstance(message.get("role"), str)
            for message in messages
        ):
            raise ValueError("Legacy messages must be a list of role-bearing objects")
        for message in messages:
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or any(
                not isinstance(call, dict) or not isinstance(call.get("function"), dict)
                for call in calls
            ):
                raise ValueError("Malformed legacy tool calls")
        try:
            json.dumps(messages, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Legacy messages must be JSON serializable") from error
        bus = EventBus()
        imported = TranscriptLog(bus)
        for message in messages:
            role = message.get("role", "")
            actor = str(message.get("agent_id") or "master")
            content = message.get("content")
            if role in {"user", "assistant"}:
                text = message_text(content)
                if text:
                    imported._ingest(Event(EventType.AGENT_MESSAGE, {
                        "role": role, "agent_id": actor, "text": text,
                    }))
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    imported._ingest(Event(EventType.TOOL_CALL, {
                        "agent_id": actor, "tool_call_id": call.get("id"),
                        "tool": function.get("name", "tool"), "input": function.get("arguments", {}),
                    }))
                # Anthropic-style snapshots may retain tool blocks in content.
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        imported._ingest(Event(EventType.TOOL_CALL, {
                            "agent_id": actor, "tool_call_id": block.get("id"),
                            "tool": block.get("name", "tool"), "input": block.get("input", {}),
                        }))
                    elif block.get("type") == "tool_result":
                        imported._ingest(Event(EventType.TOOL_RESULT, {
                            "agent_id": actor, "tool_call_id": block.get("tool_use_id"),
                            "output": message_text(block.get("content")),
                            "status": "error" if block.get("is_error") else "done",
                        }))
            elif role in {"tool", "function"}:
                imported._ingest(Event(EventType.TOOL_RESULT, {
                    "agent_id": actor, "tool_call_id": message.get("tool_call_id"),
                    "tool": message.get("name"), "output": message_text(content),
                }))
        self.restore(imported.export())
        imported.close()

    def render_text(self, query: str = "") -> str:
        needle = query.casefold()
        with self._lock:
            entries = [record_text(record) for record in self._records]
        return "\n\n".join(entry for entry in entries if needle in entry.casefold())

    def last_assistant_text(self) -> str:
        with self._lock:
            for record in reversed(self._records):
                if record.get("role") == "assistant" and record.get("actor") == "master":
                    return record.get("text", "")
        return ""

    def _new_id(self) -> str:
        while True:
            self._sequence += 1
            record_id = f"entry:{self._sequence}"
            if record_id not in self._indices:
                return record_id

    @staticmethod
    def _key(kind: str, actor: str, identity: Any) -> str:
        return json.dumps([kind, actor, str(identity)], ensure_ascii=False)

    def _ingest(self, event: Event) -> None:
        with self._lock:
            if self._closed:
                return
            data = deepcopy(event.data)
            actor = str(data.get("agent_id") or "master")
            kind = event.type
            if kind == EventType.AGENT_MESSAGE:
                streaming = bool(data.get("streaming"))
                identity = data.get("stream_id")
                record_id = self._key("message", actor, identity) if streaming and identity else self._new_id()
                old = self._get(record_id)
                role = data.get("role") or ("assistant" if streaming or data.get("source") == "agent"
                                             else "system" if data.get("source") == "system" else "user")
                role = literal_text(role)
                if streaming and not data.get("final"):
                    text = old.get("text", "") + message_text(data.get("delta"))
                else:
                    value = data.get("text", data.get("content"))
                    text = old.get("text", "") if value is None else message_text(value)
                if not text and not old:
                    return
                record = {**old, "kind": "message", "role": role, "text": text,
                          "status": "running" if streaming and not data.get("final") else "done"}
            elif kind in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
                identity = next((data[field] for field in ("invocation_id", "call_seq", "tool_call_id")
                                 if data.get(field) is not None), None)
                record_id = self._key("tool", actor, identity) if identity is not None else self._new_id()
                old = self._get(record_id)
                record = {**old, "kind": "tool",
                          "tool": literal_text(data.get("tool") or old.get("tool", "tool")),
                          "status": literal_text(data.get("status") or (
                              "running" if kind == EventType.TOOL_CALL else "done"))}
                for key in ("input", "tool_input", "arguments", "code"):
                    if key in data:
                        record["input"] = data[key]
                        break
                if kind == EventType.TOOL_RESULT:
                    output = data.get("output")
                    if output is None:
                        output = literal_text(data.get("stdout"))
                        if data.get("stderr"):
                            output += ("\n" if output else "") + literal_text(data["stderr"])
                    record["output"] = literal_text(output)
            elif kind in {EventType.APPROVAL_REQUEST, EventType.APPROVAL_RESOLVED}:
                identity = data.get("request_id")
                record_id = self._key("approval", actor, identity) if identity else self._new_id()
                if kind == EventType.APPROVAL_RESOLVED and not data.get("agent_id"):
                    matches = [r for r in self._records if r["kind"] == "approval"
                               and r.get("data", {}).get("request_id") == identity]
                    if len(matches) == 1:
                        record_id, actor = matches[0]["id"], matches[0]["actor"]
                old = self._get(record_id)
                record = {**old, "kind": "approval", "status": "pending" if kind == EventType.APPROVAL_REQUEST
                          else "approved" if data.get("approved") is True else "denied"}
            elif kind in {EventType.SUB_AGENT_DISPATCH, EventType.SUB_AGENT_RESULT}:
                record_id = self._key("worker", actor, data.get("dispatch_id", actor))
                old = self._get(record_id)
                record = {**old, "kind": "worker", "status": literal_text(data.get("status") or (
                    "running" if kind == EventType.SUB_AGENT_DISPATCH else "done"))}
            else:
                record_id, old = self._new_id(), {}
                record = {"kind": "error", "text": literal_text(
                    data.get("message") or data.get("error")), "status": "error"}
            record.update(id=record_id, actor=actor, timestamp=old.get("timestamp", event.timestamp),
                          data={**old.get("data", {}), **data})
            for field in ("tool_call_id", "invocation_id", "execution_context", "started_at", "completed_at"):
                if field in data:
                    record[field] = deepcopy(data[field])
            # Streaming tokens belong only in the accumulated text, not duplicated
            # in every serialized payload; retain all other event metadata.
            record["data"].pop("delta", None)
            index = self._indices.get(record_id)
            if index is None:
                self._indices[record_id] = len(self._records)
                self._records.append(record)
            else:
                self._records[index] = record
            self.revision += 1
            self._record_revisions[record_id] = self.revision
            self._record_revisions.move_to_end(record_id)
        self._notify(record_id)
