"""Minimal MCP (Model Context Protocol) client over stdio.

JSON-RPC 2.0; spawn server, initialize, tools/list, then tools/call on
demand. Keeps the server alive across tool calls so state persists."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MCPError(Exception):
    pass


class MCPClient:
    """One MCP server (stdio transport)."""

    def __init__(
        self,
        name: str,
        command: list[str] | str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        request_timeout: float = 30.0,
    ) -> None:
        self.name = name
        if isinstance(command, str):
            self.argv = shlex.split(command)
        else:
            self.argv = list(command)
        self.env = env or {}
        self.cwd = cwd
        self.request_timeout = request_timeout

        self.proc: Optional[asyncio.subprocess.Process] = None
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.tools: list[dict] = []
        self._next_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False


    async def start(self) -> None:
        if self.proc is not None:
            return
        spawn_env = os.environ.copy()
        spawn_env.update(self.env)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spawn_env,
                cwd=self.cwd,
            )
        except FileNotFoundError as e:
            raise MCPError(
                f"MCP server '{self.name}' command not found: {self.argv[0]!r}"
            ) from e
        except Exception as e:
            raise MCPError(f"MCP server '{self.name}' failed to start: {e}") from e

        # Drain stderr asynchronously so the server doesn't block on it.
        asyncio.create_task(self._drain_stderr())
        self._reader_task = asyncio.create_task(self._read_loop())

        try:
            init_result = await self._request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "drx-agent", "version": "0.5"},
                },
            )
        except Exception as e:
            await self.close()
            raise MCPError(f"MCP server '{self.name}' init failed: {e}") from e

        self.server_info = init_result.get("serverInfo", {}) or {}
        self.capabilities = init_result.get("capabilities", {}) or {}
        await self._notify("notifications/initialized", {})

        try:
            tools_resp = await self._request("tools/list", {})
            self.tools = list(tools_resp.get("tools") or [])
        except Exception as e:
            logger.warning("MCP server %s tools/list failed: %s", self.name, e)
            self.tools = []

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("server closed"))
        self._pending.clear()


    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> str:
        """Invoke an MCP tool by name. Returns a string (flattened content)."""
        if self.proc is None:
            raise MCPError(f"MCP server '{self.name}' not started")
        result = await self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
        )
        return self._flatten_content(result)

    @staticmethod
    def _flatten_content(result: dict) -> str:
        
        if not isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        if result.get("isError"):
            content = result.get("content") or []
            text = " ".join(
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
            return json.dumps({"error": text or "MCP tool error"}, ensure_ascii=False)

        parts: list[str] = []
        for c in result.get("content") or []:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "text":
                parts.append(c.get("text", ""))
            elif ctype == "resource":
                res = c.get("resource") or {}
                parts.append(
                    f"[resource {res.get('uri', '?')}]\n{res.get('text', '')}"
                )
            elif ctype == "image":
                parts.append(f"[image {c.get('mimeType', '?')}]")
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        joined = "\n".join(p for p in parts if p)
        if "structuredContent" in result:
            return json.dumps(
                {
                    "text": joined,
                    "structured": result["structuredContent"],
                },
                ensure_ascii=False,
            )
        return joined or json.dumps(result, ensure_ascii=False)


    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
            raise MCPError(f"MCP server '{self.name}' stdin closed")
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(encoded)
        try:
            await self.proc.stdin.drain()
        except Exception as e:
            raise MCPError(f"MCP write failed: {e}") from e

    async def _request(self, method: str, params: dict) -> Any:
        req_id = self._alloc_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            })
            return await asyncio.wait_for(fut, timeout=self.request_timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _notify(self, method: str, params: dict) -> None:
        await self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            try:
                line = await self.proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                logger.debug("MCP %s: non-JSON line: %s", self.name, line[:200])
                continue
            await self._dispatch(msg)
        # Stream closed — fail pending requests so callers don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("server closed"))
        self._pending.clear()

    async def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.get(msg["id"])
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(
                    MCPError(
                        f"{err.get('code', '?')}: {err.get('message', 'unknown error')}"
                    )
                )
            else:
                fut.set_result(msg.get("result"))
            return
            # Server-initiated requests/notifications: back-channel methods (sampling, roots) are ignored.

    async def _drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        while True:
            try:
                line = await self.proc.stderr.readline()
            except Exception:
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("[mcp:%s stderr] %s", self.name, text)

