"""SubAgent: a self-contained ReAct loop with isolated message history.

Receives a task from the master, runs its own LLM + tool loop using the
parent's tool executor, publishes SUB_AGENT_DISPATCH / SUB_AGENT_RESULT."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from drx_agent.event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)


class SubAgentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    TIMEOUT = "timeout"
    ERROR = "error"


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
        ttl: int = 300,
        max_iterations: int = 12,
        parallel_tool_calls: bool = True,
        usage_callback: Optional[Callable[[Optional[dict], Optional[str]], None]] = None,
    ) -> None:
        self.agent_id = f"{agent_type}-{uuid.uuid4().hex[:4]}"
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
        self.status = SubAgentStatus.QUEUED

    async def run(self) -> SubAgentResult:
        self.status = SubAgentStatus.RUNNING
        self.event_bus.publish(
            Event(
                type=EventType.SUB_AGENT_DISPATCH,
                data={
                    "agent_id": self.agent_id,
                    "type": self.agent_type,
                    "target": self.target,
                    "task": self.task[:200],
                },
            )
        )

        # No-LLM fallback (used by /scan, /exploit and unit tests): emit lifecycle events and a synthetic "done" result.
        if self.llm_provider is None or self.tool_executor is None:
            self.status = SubAgentStatus.DONE
            self.event_bus.publish(
                Event(
                    type=EventType.SUB_AGENT_RESULT,
                    data={
                        "agent_id": self.agent_id,
                        "status": self.status.value,
                        "scripts_executed": 1,
                    },
                )
            )
            return SubAgentResult(
                agent_id=self.agent_id,
                status=self.status,
                scripts_executed=1,
            )

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.task},
        ]

        start = time.time()
        scripts_executed = 0
        final_text = ""
        error_seen = ""

        for iteration in range(self.max_iterations):
            if self.ttl and (time.time() - start) > self.ttl:
                self.status = SubAgentStatus.TIMEOUT
                error_seen = f"ttl ({self.ttl}s) exceeded"
                break

            text_parts: list[str] = []
            pending_calls: list[dict] = []
            assistant_msg: Optional[dict] = None
            saw_error = False

            try:
                async for ev in self.llm_provider.chat(
                    messages, tools=self.tool_schemas, stream=False
                ):
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
                    elif kind_value == "error":
                        error_seen = ev.content or "unknown LLM error"
                        saw_error = True
                        break
                    elif kind_value == "done":
                        meta = ev.metadata or {}
                        assistant_msg = meta.get("assistant_message")
                        if self.usage_callback is not None:
                            try:
                                self.usage_callback(meta.get("usage"), meta.get("model"))
                            except Exception:
                                logger.exception("sub-agent usage_callback failed")
                        break
            except Exception as exc:
                logger.exception("Sub-agent %s LLM call failed", self.agent_id)
                error_seen = str(exc)
                saw_error = True

            if saw_error:
                self.status = SubAgentStatus.ERROR
                break

            text_now = "".join(text_parts).strip()
            if text_now:
                final_text = text_now

            if pending_calls:
                if assistant_msg is None:
                    assistant_msg = {
                        "role": "assistant",
                        "content": text_now,
                        "tool_calls": [
                            {
                                "id": c["id"] or f"call_{i}",
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c["input"], ensure_ascii=False),
                                },
                            }
                            for i, c in enumerate(pending_calls)
                        ],
                    }
                messages.append(assistant_msg)

                if self.parallel_tool_calls and len(pending_calls) > 1:
                    coros = [
                        self.tool_executor(c["name"], c["input"]) for c in pending_calls
                    ]
                    results = await asyncio.gather(*coros, return_exceptions=True)
                    for call, res in zip(pending_calls, results):
                        if isinstance(res, Exception):
                            res_text = json.dumps(
                                {"error": f"tool raised: {res}"}, ensure_ascii=False
                            )
                        else:
                            res_text = res
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"] or "",
                            "name": call["name"],
                            "content": res_text,
                        })
                        scripts_executed += 1
                else:
                    for call in pending_calls:
                        try:
                            res = await self.tool_executor(call["name"], call["input"])
                        except Exception as exc:
                            res = json.dumps(
                                {"error": f"tool raised: {exc}"}, ensure_ascii=False
                            )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"] or "",
                            "name": call["name"],
                            "content": res,
                        })
                        scripts_executed += 1
                continue
            break

        if self.status == SubAgentStatus.RUNNING:
            self.status = SubAgentStatus.DONE

        self.event_bus.publish(
            Event(
                type=EventType.SUB_AGENT_RESULT,
                data={
                    "agent_id": self.agent_id,
                    "status": self.status.value,
                    "scripts_executed": scripts_executed,
                    "text": final_text[:500],
                },
            )
        )

        return SubAgentResult(
            agent_id=self.agent_id,
            status=self.status,
            scripts_executed=scripts_executed,
            error=error_seen,
            text=final_text,
        )

