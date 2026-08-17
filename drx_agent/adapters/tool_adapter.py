"""Adapter: map legacy tool-call patterns onto the Code Execution Engine.

The old DRX-SCAN tools (info_tools, probe_tools, vuln_tools) are
wrapped as script templates that the new agent can execute through
the Python/Bash sandboxes rather than calling directly.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class AdaptedTool:
    name: str
    category: str
    risk_level: str
    script_template: str
    description: str = ""


class ToolAdapter:
    """Wraps legacy tool modules as sandbox-executable script templates."""

    def __init__(self):
        self._tools: dict[str, AdaptedTool] = {}

    def register(self, tool: AdaptedTool) -> None:
        self._tools[tool.name] = tool

    def get_script(self, name: str, **params) -> str | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        script = tool.script_template
        for key, value in params.items():
            script = script.replace(f"{{{{{key}}}}}", str(value))
        return script

    def list_tools(self) -> list[dict]:
        return [
            {"name": t.name, "category": t.category, "risk_level": t.risk_level}
            for t in self._tools.values()
        ]

    def get_tool(self, name: str) -> AdaptedTool | None:
        return self._tools.get(name)


def create_default_adapter() -> ToolAdapter:
    adapter = ToolAdapter()

    adapter.register(AdaptedTool(
        name="port_scan",
        category="recon",
        risk_level="L0",
        description="TCP port scan on a target",
        script_template='''
import socket
import json

target = "{{target}}"
ports = [int(p) for p in "{{ports}}".split(",")]
results = []

for port in ports:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    result = sock.connect_ex((target, port))
    if result == 0:
        try:
            sock.send(b"HEAD / HTTP/1.0\\r\\n\\r\\n")
            banner = sock.recv(1024).decode(errors="ignore").strip()
        except Exception:
            banner = ""
        results.append({"port": port, "open": True, "banner": banner})
    else:
        results.append({"port": port, "open": False, "banner": ""})
    sock.close()

print(json.dumps({"status": "success", "results": results}))
'''))

    adapter.register(AdaptedTool(
        name="http_get",
        category="recon",
        risk_level="L0",
        description="HTTP GET request with response analysis",
        script_template='''
import urllib.request
import json
import ssl

url = "{{url}}"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

try:
    req = urllib.request.Request(url, headers={"User-Agent": "DRX-AGENT/1.0"})
    resp = urllib.request.urlopen(req, context=ctx, timeout=10)
    body = resp.read().decode(errors="ignore")[:4096]
    print(json.dumps({
        "status": "success",
        "status_code": resp.status,
        "headers": dict(resp.headers),
        "body_preview": body,
    }))
except Exception as e:
    print(json.dumps({"status": "error", "error": str(e)}))
'''))

    adapter.register(AdaptedTool(
        name="dns_lookup",
        category="recon",
        risk_level="L0",
        description="DNS forward and reverse lookup",
        script_template='''
import socket
import json

hostname = "{{hostname}}"
results = {}

try:
    results["forward"] = socket.gethostbyname_ex(hostname)
except Exception as e:
    results["forward_error"] = str(e)

print(json.dumps(results))
'''))

    return adapter
