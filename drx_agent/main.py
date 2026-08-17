"""DRX-Operator: Autonomous Red-Team Penetration Testing Expert System.

Agent-First architecture: TUI is a thin shell, the Agent is the
first-class citizen.  All operations are LLM tool calls.
"""

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drx_agent.event_bus import EventBus, Event, EventType
from drx_agent.tui.app import DrxAgentApp
from drx_agent.safety.gate import SafetyGate
from drx_agent.agent.knowledge_base import KnowledgeBase
from drx_agent.agent.task_scheduler import TaskScheduler, TaskPriority, ScheduledTask
from drx_agent.agent.master import MasterAgent
from drx_agent.engine.python_sandbox import PythonSandbox
from drx_agent.engine.bash_sandbox import BashSandbox
from drx_agent.engine.script_library import ScriptLibrary
from drx_agent.skills.registry import SkillsRegistry
from drx_agent.session.manager import SessionManager
from drx_agent.llm.base import LLMConfig
from drx_agent.mcp.manager import MCPManager
from drx_agent.hooks.manager import HookManager

logger = logging.getLogger(__name__)


def _build_one_provider(spec: dict):
    
    provider_name = (spec.get("provider") or "").lower()

    if provider_name in ("anthropic", "claude"):
        env_key = os.environ.get("ANTHROPIC_API_KEY")
    elif provider_name in ("openai",):
        env_key = os.environ.get("OPENAI_API_KEY")
    else:
        env_key = os.environ.get("DEEPSEEK_API_KEY")
    api_key = (
        os.environ.get("DRX_LLM_API_KEY")
        or env_key
        or spec.get("api_key", "")
    )
    if not api_key:
        logger.warning("Provider %r has no API key — skipped", provider_name or "default")
        return None

    config = LLMConfig(
        model=spec.get("model", "deepseek-chat"),
        api_key=api_key,
        base_url=spec.get("base_url", ""),
        temperature=float(spec.get("temperature", 0.7)),
        max_tokens=int(spec.get("max_tokens", 4096)),
    )
    try:
        if provider_name in ("anthropic", "claude"):
            from drx_agent.llm.anthropic_provider import AnthropicProvider
            return AnthropicProvider(config)
        if provider_name in ("openai",):
            from drx_agent.llm.openai_provider import OpenAIProvider
            return OpenAIProvider(config)
        from drx_agent.llm.deepseek_provider import DeepSeekProvider
        return DeepSeekProvider(config)
    except Exception as exc:
        logger.warning("Failed to build provider %r: %s", provider_name, exc)
        return None


def _build_llm_provider(event_bus=None):
    
    cfg_path = os.path.join(
        os.path.dirname(__file__), "..", "configs", "default_config.json"
    )
    cfg_path = os.path.abspath(cfg_path)
    if not os.path.isfile(cfg_path):
        logger.warning("LLM config not found at %s", cfg_path)
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:
        logger.warning("Failed to load LLM config: %s", exc)
        return None

    llm_cfg = data.get("llm", {})
    if not llm_cfg.get("enabled", True):
        return None

    primary = _build_one_provider(llm_cfg)
    providers = [primary] if primary else []

    for fb_spec in llm_cfg.get("fallback") or []:
        if not isinstance(fb_spec, dict):
            continue
        fb = _build_one_provider(fb_spec)
        if fb is not None:
            providers.append(fb)

    if not providers:
        logger.warning("No usable LLM provider configured")
        return None

    retry_cfg = llm_cfg.get("retry") or {}

    def _notify(msg: str):
        if event_bus is not None:
            try:
                event_bus.publish(Event(
                    type=EventType.STATUS_UPDATE, data={"text": msg}
                ))
                event_bus.publish(Event(
                    type=EventType.AGENT_MESSAGE,
                    data={"text": f"⚠ {msg}", "source": "system"},
                ))
            except Exception:
                pass

    from drx_agent.llm.resilient import ResilientProvider
    return ResilientProvider(
        providers=providers,
        max_retries=int(retry_cfg.get("max_retries", 3)),
        base_delay=float(retry_cfg.get("base_delay", 1.0)),
        max_delay=float(retry_cfg.get("max_delay", 30.0)),
        notify=_notify,
    )


class DrxAgent:
    """DRX-Operator main controller — connects all subsystems."""

    # execute_bash may run these; destructive ops are still blocked by
    # BashSandbox.BLOCKED_PATTERNS and the PermissionEngine. None → no whitelist.
    BASH_WHITELIST = [
        "cat", "tac", "head", "tail", "less", "more", "nl", "wc",
        "file", "stat", "ls", "tree", "pwd", "readlink", "realpath",
        "strings", "od", "xxd", "hexdump",
        "grep", "egrep", "fgrep", "rg", "ag",
        "awk", "gawk", "sed", "cut", "tr", "sort", "uniq", "paste",
        "diff", "comm", "join", "column", "fold", "fmt", "rev", "expand",
        "find", "locate", "which", "whereis", "type",
        "base64", "base32", "md5sum", "sha1sum", "sha256sum", "sha512sum",
        "shasum", "cksum", "uuencode", "uudecode",
        "curl", "wget", "dig", "host", "nslookup", "whois",
        "ping", "ping6", "traceroute", "traceroute6", "tracepath",
        "nc", "ncat", "socat", "telnet", "openssl",
        "ip", "ifconfig", "netstat", "ss", "arp", "route", "ipcalc",
        "nmap", "masscan", "rustscan", "naabu",
        "sqlmap", "nikto", "hydra", "medusa", "patator",
        "gobuster", "feroxbuster", "ffuf", "wfuzz", "dirb",
        "amass", "subfinder", "httpx", "nuclei", "katana", "waybackurls",
        "dnsx", "dnsenum", "fierce", "theHarvester",
        "responder", "crackmapexec", "impacket-secretsdump",
        "uname", "hostname", "id", "whoami", "groups", "users", "w", "who",
        "uptime", "date", "env", "printenv", "getent", "lscpu", "lsblk",
        "ps", "top", "htop", "free", "df", "du", "mount",
        "tar", "gzip", "gunzip", "zcat", "bzip2", "bunzip2", "xz", "unxz",
        "zip", "unzip", "7z", "7za", "ar",
        "git",
        "echo", "printf", "true", "false", "test", "[", "yes", "seq",
        "sleep", "timeout", "tee", "xargs", "env",
        "ssh", "scp", "sftp", "rsync",
        "python", "python3", "perl", "ruby", "node", "deno", "php",
        "bash", "sh", "dash", "zsh", "fish", "lua",
    ]

    def __init__(self):
        self.event_bus = EventBus()

        self.safety_gate = SafetyGate()

        self.knowledge_base = KnowledgeBase()

        self.python_sandbox = PythonSandbox()
        # Config overrides: bash.whitelist null → everything allowed; [...] → exact list; extra_whitelist → append.
        bash_whitelist = list(self.BASH_WHITELIST)
        try:
            cfg_path = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "configs", "default_config.json")
            )
            with open(cfg_path, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            bash_cfg = cfg.get("bash") or {}
            if "whitelist" in bash_cfg:
                bash_whitelist = bash_cfg["whitelist"]
            extra = bash_cfg.get("extra_whitelist") or []
            if bash_whitelist is not None and extra:
                bash_whitelist = list(bash_whitelist) + list(extra)
        except Exception:
            pass
        self.bash_sandbox = BashSandbox(command_whitelist=bash_whitelist)

        self.script_library = ScriptLibrary()
        self.skills_registry = SkillsRegistry()

        self.scheduler = TaskScheduler()

        session_dir = os.path.join(os.path.dirname(__file__), "..", "sessions")
        self.session_manager = SessionManager(storage_dir=os.path.abspath(session_dir))

        self.llm_provider = _build_llm_provider(event_bus=self.event_bus)

        cfg_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "configs", "default_config.json")
        )
        self.mcp_manager = MCPManager.from_config_file(cfg_path)

        self.hooks = HookManager()
        try:
            with open(cfg_path, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            self.hooks.load_from_config(cfg.get("hooks") or [])
        except Exception:
            pass

        self.master = MasterAgent(
            event_bus=self.event_bus,
            scheduler=self.scheduler,
            python_sandbox=self.python_sandbox,
            bash_sandbox=self.bash_sandbox,
            knowledge_base=self.knowledge_base,
            safety_gate=self.safety_gate,
            skills_registry=self.skills_registry,
            script_library=self.script_library,
            llm_provider=self.llm_provider,
            mcp_manager=self.mcp_manager,
            hooks=self.hooks,
        )

        try:
            with open(cfg_path, "r", encoding="utf-8") as fp:
                _cfg = json.load(fp)
            _win = (_cfg.get("llm") or {}).get("context_window")
            if _win:
                self.master.model_context_window_override = int(_win)
        except Exception:
            pass

        self._load_skills()

        self._setup_session_handlers()

    async def async_setup(self) -> None:
        """One-time async startup: connect MCP servers, etc."""
        await self.mcp_manager.start_all()
        if self.mcp_manager.clients:
            count = sum(len(c.tools) for c in self.mcp_manager.clients.values())
            self.event_bus.publish(Event(
                type=EventType.STATUS_UPDATE,
                data={"text": (
                    f"MCP: {len(self.mcp_manager.clients)} server(s), "
                    f"{count} tool(s) ready"
                )},
            ))

    async def async_teardown(self) -> None:
        self.master.shutdown()
        await self.mcp_manager.close_all()

    def _load_skills(self):
        skills_dir = os.path.join(os.path.dirname(__file__), "..", "skills")
        abs_path = os.path.abspath(skills_dir)
        if os.path.isdir(abs_path):
            loaded = self.skills_registry.load_from_directory(abs_path)
            if loaded > 0:
                self.event_bus.publish(Event(
                    type=EventType.STATUS_UPDATE,
                    data={"text": f"Loaded {loaded} skills"}
                ))

    def _setup_session_handlers(self):
        def handle_save(event: Event):
            try:
                sid = self.session_manager.save(
                    kb=self.knowledge_base,
                    messages=getattr(self.master, 'messages', []),
                    active_targets=[t["host"] for t in self.knowledge_base.list_targets()],
                    name=f"session-{len(getattr(self.master, 'messages', []))}msgs",
                    todos=getattr(self.master, 'todos', []),
                    mode=getattr(self.master, 'mode', 'act'),
                    session_usage=getattr(self.master, 'session_usage', {}),
                )
                self.event_bus.publish(Event(
                    type=EventType.AGENT_MESSAGE,
                    data={
                        "text": (
                            f"💾 会话已保存: {sid} "
                            f"(messages={len(getattr(self.master, 'messages', []))}, "
                            f"targets={len(self.knowledge_base.list_targets())}, "
                            f"creds={len(self.knowledge_base.list_credentials())})"
                        ),
                        "source": "system",
                    }
                ))
            except Exception as e:
                self.event_bus.publish(Event(
                    type=EventType.ERROR,
                    data={"message": f"Save failed: {e}"}
                ))

        def handle_restore(event: Event):
            try:
                sessions = self.session_manager.list_sessions()
                if not sessions:
                    self.event_bus.publish(Event(
                        type=EventType.AGENT_MESSAGE,
                        data={"text": "没有可恢复的会话。", "source": "system"}
                    ))
                    return
                latest = sessions[0]
                restored = self.session_manager.restore(latest["id"])
                if not restored:
                    self.event_bus.publish(Event(
                        type=EventType.ERROR,
                        data={"message": "Restore returned no data"}
                    ))
                    return
                self.knowledge_base = restored["kb"]
                self.master.knowledge_base = restored["kb"]
                self.master.messages = restored.get("messages", [])
                self.master.todos = restored.get("todos", [])
                self.master.mode = restored.get("mode", "act") or "act"
                if restored.get("session_usage"):
                    self.master.session_usage.update(restored["session_usage"])
                if self.master.todos:
                    self.event_bus.publish(Event(
                        type=EventType.STATUS_UPDATE,
                        data={"tasks": [
                            {"name": t.get("content", ""), "status": t.get("status", "pending"), "id": t.get("id", "")}
                            for t in self.master.todos
                        ]},
                    ))
                self.event_bus.publish(Event(
                    type=EventType.STATUS_UPDATE,
                    data={
                        "mode": self.master.mode,
                        "active_targets": len(self.knowledge_base.list_targets()),
                    },
                ))
                self.event_bus.publish(Event(
                    type=EventType.AGENT_MESSAGE,
                    data={
                        "text": (
                            f"♻️ 会话已恢复: {latest['name']} "
                            f"(messages={len(self.master.messages)}, "
                            f"targets={len(self.knowledge_base.list_targets())}, "
                            f"creds={len(self.knowledge_base.list_credentials())}, "
                            f"mode={self.master.mode})"
                        ),
                        "source": "system",
                    }
                ))
            except Exception as e:
                self.event_bus.publish(Event(
                    type=EventType.ERROR,
                    data={"message": f"Restore failed: {e}"}
                ))

        self.event_bus.subscribe(EventType.SESSION_SAVE, handle_save)
        self.event_bus.subscribe(EventType.SESSION_RESTORE, handle_restore)


def main():
    """Entry point — create agent and launch TUI."""
    agent = DrxAgent()
    app = DrxAgentApp(agent.event_bus, drx_agent=agent)
    app.run()


if __name__ == "__main__":
    main()

