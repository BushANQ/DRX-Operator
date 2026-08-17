"""MCPManager — load MCP server configs, spawn clients, surface tools.

Tools from MCP servers are exposed to the LLM with a `mcp__<server>__<tool>`
name prefix (matching the Claude Code convention) so they live in the same
flat OpenAI-format tool list as built-in tools.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from drx_agent.mcp.client import MCPClient, MCPError

logger = logging.getLogger(__name__)


_TOOL_PREFIX = "mcp__"


class MCPManager:
    def __init__(self, configs: dict[str, dict] | None = None) -> None:
        self._configs: dict[str, dict] = configs or {}
        self.clients: dict[str, MCPClient] = {}

    @classmethod
    def from_config_file(cls, path: str) -> "MCPManager":
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except FileNotFoundError:
            return cls({})
        except Exception as e:
            logger.warning("MCP config load failed (%s): %s", path, e)
            return cls({})

        mcp_section = data.get("mcp") or {}
        servers = mcp_section.get("servers") or {}
        servers = {
            name: cfg
            for name, cfg in servers.items()
            if isinstance(cfg, dict) and cfg.get("enabled", True)
        }
        return cls(servers)

    async def start_all(self) -> None:
        for name, cfg in self._configs.items():
            try:
                cmd = cfg.get("command")
                if not cmd:
                    logger.warning("MCP server %s missing 'command'", name)
                    continue
                argv = [cmd] + list(cfg.get("args") or [])
                client = MCPClient(
                    name=name,
                    command=argv,
                    env=cfg.get("env") or {},
                    cwd=cfg.get("cwd"),
                    request_timeout=float(cfg.get("timeout", 30.0)),
                )
                await client.start()
                self.clients[name] = client
                logger.info(
                    "MCP server '%s' ready: %d tools (%s)",
                    name,
                    len(client.tools),
                    client.server_info.get("name", "?"),
                )
            except MCPError as e:
                logger.warning("MCP server '%s' disabled: %s", name, e)
            except Exception as e:
                logger.exception("MCP server '%s' init crashed: %s", name, e)

    async def close_all(self) -> None:
        for client in list(self.clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        self.clients.clear()


    def openai_tool_schemas(self) -> list[dict]:
        """Return all MCP tools rendered as OpenAI/DeepSeek function schemas."""
        out: list[dict] = []
        for server_name, client in self.clients.items():
            for tool in client.tools:
                name = tool.get("name")
                if not name:
                    continue
                qualified = f"{_TOOL_PREFIX}{server_name}__{name}"
                description = (
                    f"[via MCP server '{server_name}'] "
                    + (tool.get("description") or "")
                ).strip()
                schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
                # OpenAI requires parameters be JSON schema with type=object
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    schema = {"type": "object", "properties": {}}
                out.append({
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": description[:512],
                        "parameters": schema,
                    },
                })
        return out

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith(_TOOL_PREFIX)

    def parse_tool_name(self, qualified: str) -> tuple[str, str] | None:
        if not qualified.startswith(_TOOL_PREFIX):
            return None
        body = qualified[len(_TOOL_PREFIX):]
        if "__" not in body:
            return None
        server, _, tool = body.partition("__")
        return server, tool

    async def call(self, qualified_tool_name: str, args: dict) -> str:
        parsed = self.parse_tool_name(qualified_tool_name)
        if parsed is None:
            return json.dumps(
                {"error": f"invalid MCP tool name: {qualified_tool_name!r}"},
                ensure_ascii=False,
            )
        server, tool = parsed
        client = self.clients.get(server)
        if client is None:
            return json.dumps(
                {"error": f"MCP server '{server}' not connected"},
                ensure_ascii=False,
            )
        try:
            return await client.call_tool(tool, args)
        except MCPError as e:
            return json.dumps(
                {"error": f"MCP call failed: {e}", "server": server, "tool": tool},
                ensure_ascii=False,
            )

    def status(self) -> list[dict]:
        return [
            {
                "name": name,
                "tools": len(client.tools),
                "server_info": client.server_info,
                "argv": client.argv,
            }
            for name, client in self.clients.items()
        ]
