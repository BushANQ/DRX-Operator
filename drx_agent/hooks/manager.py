"""User-defined hooks fired around tool calls and LLM calls.

Python hooks (sync/async callables) or command hooks (shell command, event
JSON on stdin). Events: pre_tool / post_tool / pre_llm / post_llm /
agent_message. A pre_tool hook may return {"deny": "reason"} to
short-circuit the call."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


EVENTS = (
    "pre_tool",
    "post_tool",
    "pre_llm",
    "post_llm",
    "agent_message",
)


HookCallable = Callable[[dict], Any]


@dataclass
class HookSpec:
    event: str
    handler: HookCallable
    name: str = ""
    tool_glob: str = "*"


class HookManager:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookSpec]] = {e: [] for e in EVENTS}


    def register(
        self,
        event: str,
        handler: HookCallable,
        name: str = "",
        tool_glob: str = "*",
    ) -> None:
        if event not in self._hooks:
            raise ValueError(f"unknown hook event: {event!r}")
        self._hooks[event].append(
            HookSpec(event=event, handler=handler, name=name or getattr(handler, "__name__", "hook"), tool_glob=tool_glob)
        )

    def register_command(
        self,
        event: str,
        command: str | list[str],
        name: str = "",
        tool_glob: str = "*",
        timeout: float = 5.0,
    ) -> None:
        """Register a shell-command hook. The event JSON is piped to stdin."""
        argv = shlex.split(command) if isinstance(command, str) else list(command)

        def runner(event_data: dict) -> Any:
            try:
                proc = subprocess.run(
                    argv,
                    input=json.dumps(event_data, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                stdout = (proc.stdout or "").strip()
                if not stdout:
                    return None
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError:
                    return {"stdout": stdout}
            except Exception as e:
                logger.warning("Hook command %r failed: %s", argv, e)
                return None

        self.register(event, runner, name=name or argv[0], tool_glob=tool_glob)

    def load_from_config(self, hooks_cfg: list[dict]) -> None:
        """Load declarative hook specs from config."""
        for cfg in hooks_cfg or []:
            if not isinstance(cfg, dict):
                continue
            event = cfg.get("event")
            cmd = cfg.get("command")
            if not event or not cmd:
                continue
            self.register_command(
                event,
                cmd,
                name=cfg.get("name", ""),
                tool_glob=cfg.get("tool_glob", "*"),
                timeout=float(cfg.get("timeout", 5.0)),
            )


    @staticmethod
    def _tool_matches(spec_glob: str, tool_name: str) -> bool:
        import fnmatch
        return fnmatch.fnmatchcase(tool_name, spec_glob)

    async def dispatch(self, event: str, payload: dict) -> list[Any]:
        """Run all hooks for *event*; returns their return values."""
        specs = self._hooks.get(event) or []
        if not specs:
            return []
        payload = dict(payload)
        payload.setdefault("event", event)
        payload.setdefault("ts", time.time())
        tool_name = payload.get("tool", "")
        results: list[Any] = []
        for spec in specs:
            if event in ("pre_tool", "post_tool") and not self._tool_matches(spec.tool_glob, tool_name):
                continue
            try:
                ret = spec.handler(payload)
                if inspect.iscoroutine(ret) or isinstance(ret, Awaitable):
                    ret = await ret
                results.append(ret)
            except Exception as exc:
                logger.exception("Hook %r raised on %s", spec.name, event)
                results.append({"error": str(exc), "hook": spec.name})
        return results

