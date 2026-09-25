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
from drx_agent.tui.transcript import TranscriptLog
from drx_agent.safety.gate import SafetyGate
from drx_agent.agent.knowledge_base import KnowledgeBase
from drx_agent.agent.task_scheduler import TaskScheduler, TaskPriority, ScheduledTask
from drx_agent.agent.master import MasterAgent
from drx_agent.engine.python_sandbox import PythonSandbox
from drx_agent.engine.bash_sandbox import BashSandbox
from drx_agent.engine.script_library import ScriptLibrary
from drx_agent.engine.process import cancel_task
from drx_agent.skills.registry import SkillsRegistry
from drx_agent.session.manager import SessionManager
from drx_agent.session.usage import usage_status
from drx_agent.agent.frontier import Frontier
from drx_agent.agent.handoff import Handoff
from drx_agent.agent.stage import StageMachine
from drx_agent.llm.base import LLMConfig
from drx_agent.llm.output_tokens import CUSTOM_MODEL_MAX_OUTPUT_TOKENS, model_output_limit
from drx_agent.mcp.manager import MCPManager
from drx_agent.hooks.manager import HookManager

logger = logging.getLogger(__name__)


def _load_dotenv(dotenv_path: str | None = None) -> None:
    """Load key-value pairs from .env into os.environ if not already set."""
    if dotenv_path is None:
        dotenv_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
    if not os.path.isfile(dotenv_path):
        return
    try:
        with open(dotenv_path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv()


def _build_one_provider(spec: dict):
    
    provider_name = (
        os.environ.get("DRX_LLM_PROVIDER") or spec.get("provider") or ""
    ).lower()
    is_exo = provider_name in ("exo", "qwen_exo", "qwen-exo")

    if provider_name in ("anthropic", "claude"):
        env_key = os.environ.get("ANTHROPIC_API_KEY")
    elif provider_name in ("openai",):
        env_key = os.environ.get("OPENAI_API_KEY")
    elif is_exo:
        env_key = ""
    else:
        env_key = os.environ.get("DEEPSEEK_API_KEY")
    api_key = (
        os.environ.get("DRX_LLM_API_KEY")
        or env_key
        or spec.get("api_key", "")
    )
    # Local EXO servers are unauthenticated by default, so an empty key is
    # valid for them; every other provider keeps the existing empty-key skip.
    if not api_key and not is_exo:
        logger.warning("Provider %r has no API key — skipped", provider_name or "default")
        return None

    api_interface = (os.environ.get("DRX_LLM_INTERFACE") or "").strip().lower()
    if api_interface not in ("chat", "responses"):
        api_interface = "chat"

    model = os.environ.get("DRX_LLM_MODEL") or spec.get("model", "deepseek-chat")
    base_url = os.environ.get("DRX_LLM_BASE_URL") or spec.get("base_url", "")
    max_tokens = spec.get("max_tokens")
    model_max_tokens = spec.get("model_max_tokens")
    if model_max_tokens is None:
        model_max_tokens = model_output_limit(model, provider_name, base_url)
    if model_max_tokens is None:
        model_max_tokens = CUSTOM_MODEL_MAX_OUTPUT_TOKENS
    context_window = spec.get("context_window")
    config = LLMConfig(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=float(spec.get("temperature", 0.7)),
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        api_interface=api_interface,
        model_max_tokens=int(model_max_tokens) if model_max_tokens is not None else None,
        omit_max_output_tokens=spec.get("omit_max_output_tokens", False),
        max_tokens_field=spec.get("max_tokens_field"),
        always_send_max_tokens=spec.get("always_send_max_tokens"),
        clamp_output_to_model_max=spec.get("clamp_output_to_model_max"),
        provider=provider_name,
        context_window=int(context_window) if context_window is not None else None,
    )
    try:
        if provider_name in ("anthropic", "claude"):
            from drx_agent.llm.anthropic_provider import AnthropicProvider
            return AnthropicProvider(config)
        if provider_name in ("openai",):
            from drx_agent.llm.openai_provider import OpenAIProvider
            return OpenAIProvider(config)
        if is_exo:
            from drx_agent.llm.exo_provider import EXOProvider
            # Optional startup hint only: if DRX_EXO_INGEST_PATHS is set, name the
            # project files that WOULD be ingested into long-term knowledge. No
            # network I/O happens here — ingestion is deferred to the caller.
            _ingest_paths = os.environ.get("DRX_EXO_INGEST_PATHS", "").strip()
            if _ingest_paths:
                _paths = [p.strip() for p in _ingest_paths.split(",") if p.strip()]
                if _paths:
                    logger.info(
                        "EXO knowledge ingestion requested at startup "
                        "(deferred, no I/O now): %s",
                        ", ".join(_paths),
                    )
            control_url = (
                spec.get("control_url")
                or os.environ.get("DRX_EXO_CONTROL_URL", "")
            )
            return EXOProvider(config, control_url=control_url)
        from drx_agent.llm.deepseek_provider import DeepSeekProvider
        return DeepSeekProvider(config)
    except Exception as exc:
        logger.warning("Failed to build provider %r: %s", provider_name, exc)
        return None


def _config_path() -> str:
    """Resolve the config file path across run modes (frozen binary / dev).

    Priority: $DRX_CONFIG → ~/.config/drx-operator/default_config.json →
    <executable-dir>/configs/default_config.json (frozen) → package-relative.
    """
    env = os.environ.get("DRX_CONFIG")
    if env:
        return os.path.abspath(env)
    user_cfg = os.path.join(
        os.path.expanduser("~"), ".config", "drx-operator", "default_config.json"
    )
    if os.path.isfile(user_cfg):
        return user_cfg
    if getattr(sys, "frozen", False):
        adjacent = os.path.join(
            os.path.dirname(sys.executable), "configs", "default_config.json"
        )
        if os.path.isfile(adjacent):
            return adjacent
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "configs", "default_config.json")
    )


def _build_llm_provider(event_bus=None):
    
    cfg_path = _config_path()
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
        "r2", "radare2", "objdump", "readelf", "nm", "gdb", "ltrace", "strace",
        "patchelf", "checksec", "rabin2", "r2pipe", "xxd", "hexdump", "od",
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
        self.transcript = TranscriptLog(self.event_bus)

        self.safety_gate = SafetyGate()

        self.knowledge_base = KnowledgeBase()

        self.python_sandbox = PythonSandbox()
        # Config overrides: bash.whitelist null → everything allowed; [...] → exact list; extra_whitelist → append.
        bash_whitelist = list(self.BASH_WHITELIST)
        cfg = {}
        try:
            cfg_path = _config_path()
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

        collaboration = cfg.get("collaboration", {})
        if not isinstance(collaboration, dict):
            raise ValueError("collaboration configuration must be an object")
        scheduler_config = collaboration.get("scheduler", {})
        if not isinstance(scheduler_config, dict):
            raise ValueError("collaboration.scheduler must be an object")
        self.scheduler = TaskScheduler(
            max_concurrent_per_target=scheduler_config.get("max_concurrent_per_target", 4),
            max_concurrent=scheduler_config.get("max_concurrent", 16),
            global_qps=scheduler_config.get("global_qps"),
        )

        session_dir = os.path.join(os.path.dirname(__file__), "..", "sessions")
        self.session_manager = SessionManager(storage_dir=os.path.abspath(session_dir))

        self.llm_provider = _build_llm_provider(event_bus=self.event_bus)
        from drx_agent.llm.model_selection import ModelSelection
        self.model_selection = ModelSelection(self.llm_provider) if self.llm_provider is not None else None
        self._model_sequence = 0

        cfg_path = _config_path()
        self.mcp_manager = MCPManager.from_config_file(cfg_path)
        self._setup_task: asyncio.Task | None = None

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
            collaboration_config=collaboration,
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
        self.event_bus.subscribe(EventType.MODEL_REQUEST, self._on_model_request)

    def _on_model_request(self, event: Event) -> None:
        if self.master._closing or self.master._restoring:
            return
        self._model_sequence += 1
        self.master._schedule(self._handle_model_request(dict(event.data), self._model_sequence))

    def _publish_model_state(self, sequence: int, state: str, *, query: str = "",
                             open_menu: bool = False, error: str = "") -> None:
        selection = self.model_selection
        self.event_bus.publish(Event(EventType.MODEL_STATE, {
            "request_id": f"model-{sequence}", "sequence": sequence, "state": state,
            "models": selection.snapshot() if selection is not None else [],
            "current": selection.current_key if selection is not None else None,
            "query": query, "open_menu": open_menu, "error": error,
        }))

    def _model_output(self, text: str) -> None:
        self.event_bus.publish(Event(EventType.AGENT_MESSAGE, {"text": text, "source": "system"}))

    def _apply_model_context(self, row: dict) -> None:
        # A previous model's explicit window must not leak into the next model.
        self.master.model_context_window_override = row.get("context_window") or 0
        self.event_bus.publish(Event(EventType.STATUS_UPDATE, {"model": row["key"]}))

    def _select_model(self, key: str, sequence: int) -> None:
        row = self.model_selection.select(key)
        self._apply_model_context(row)
        self._publish_model_state(sequence, "selected")
        self._model_output(
            f"当前模型：{row['key']}。从下一次模型请求生效；正在返回的响应不受影响。"
        )

    async def _handle_model_request(self, data: dict, sequence: int) -> None:
        action = data.get("action", "menu")
        query = data.get("query", "")
        if not isinstance(query, str):
            query = ""
        query = query.strip()
        if sequence != self._model_sequence or self.master._closing or self.master._restoring:
            return
        if self.model_selection is None:
            error = "没有可用的模型服务；请先配置 llm 和对应凭据。"
            self._publish_model_state(sequence, "error", open_menu=action in ("menu", "refresh"), error=error)
            self._model_output(error)
            return
        try:
            selection = self.model_selection
            if action == "current":
                row = next(row for row in selection.snapshot() if row["key"] == selection.current_key)
                window = row.get("context_window") or "自动"
                capability = row.get("max_tokens") or "未知"
                self._model_output(
                    f"当前模型：{row['key']}\n服务：{row['endpoint']}\n"
                    f"上下文窗口：{window}\n模型输出能力：{capability}（请求仍按接口策略裁剪）"
                )
                self._publish_model_state(sequence, "ready")
                return
            if action == "select":
                key = data.get("key")
                if isinstance(key, str) and key:
                    self._select_model(key, sequence)
                    return
                matches = selection.match(query) if query else []
                if len(matches) == 1 and query.casefold() in (
                    matches[0]["key"].casefold(), matches[0]["model"].casefold(),
                ):
                    self._select_model(matches[0]["key"], sequence)
                    return
            if action not in ("menu", "refresh", "list", "select"):
                raise ValueError("用法：/model、/model <模型>、/model list、/model current、/model refresh")
            if action in ("menu", "refresh"):
                self._publish_model_state(sequence, "loading", query=query, open_menu=True)
            warnings = await selection.refresh()
            if sequence != self._model_sequence or self.master._closing or self.master._restoring:
                return
            matches = selection.match(query) if query else selection.snapshot()
            error = "\n".join(warnings)
            if action == "list":
                lines = [f"可用模型（当前：{selection.current_key}）："]
                lines.extend(
                    f"{'*' if row['key'] == selection.current_key else '-'} {row['key']}"
                    f"  [{row['endpoint']}]"
                    for row in matches
                )
                if not matches:
                    lines.append("没有匹配的模型。")
                if error:
                    lines.append(error)
                self._model_output("\n".join(lines))
                self._publish_model_state(sequence, "ready")
            elif action == "select" and len(matches) == 1:
                self._select_model(matches[0]["key"], sequence)
            else:
                if action == "select" and not matches:
                    error = "\n".join(filter(None, ("没有匹配的模型；可修改搜索词或使用 /model refresh。", error)))
                self._publish_model_state(sequence, "ready", query=query, open_menu=True, error=error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if sequence != self._model_sequence or self.master._closing or self.master._restoring:
                return
            error = str(exc) if isinstance(exc, ValueError) else f"模型操作失败（{type(exc).__name__}）。"
            self._publish_model_state(sequence, "error", query=query, error=error)
            self._model_output(error)

    async def async_setup(self) -> None:
        """One-time async startup: connect MCP servers, etc."""
        if self.master._closing:
            return
        if getattr(self, "_setup_task", None) is None:
            self._setup_task = asyncio.create_task(self.mcp_manager.start_all())
        try:
            await asyncio.shield(self._setup_task)
        except asyncio.CancelledError:
            if not self._setup_task.done():
                cancel_task(self._setup_task)
            await asyncio.gather(self._setup_task, return_exceptions=True)
            raise
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
        self.master._closing = True
        setup = getattr(self, "_setup_task", None)
        try:
            if setup is not None:
                if not setup.done():
                    cancel_task(setup)
                await asyncio.gather(setup, return_exceptions=True)
            await self.master.async_shutdown()
        finally:
            try:
                selection = getattr(self, "model_selection", None)
                if selection is not None:
                    await selection.close()
            finally:
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

    def save_session(self) -> str:
        """Persist the current snapshot, propagating errors to the caller."""
        return self.session_manager.save(
            kb=self.knowledge_base,
            messages=self.master.messages,
            active_targets=[t["host"] for t in self.knowledge_base.list_targets()],
            name=f"session-{len(self.master.messages)}msgs",
            todos=self.master.todos,
            mode=self.master.mode,
            session_usage=self.master.session_usage,
            frontier=self.master.frontier.to_dict(),
            handoff=self.master.handoff.to_dict() if self.master.handoff is not None else None,
            stage=self.master.stage_machine.to_dict(),
            forum=self.master.forum.to_dict(),
            claims=self.master.claims.to_dict(),
            moderator=self.master.moderator.to_dict(),
            irc=self.master.irc.to_dict(),
            project_note=self.master.project_note.to_dict(),
            team=self.master._export_team_state(),
            transcript=self.transcript.export(),
            execution_capture={"version": 1, "request_id": getattr(self.master, "_execution_request_id", None)},
            swarm={"enabled": self.master.swarm_mode, "batch_size": self.master.batch_size,
                   "max_concurrent": self.master.scheduler.max_concurrent},
            model_selection=(
                self.model_selection.export_selection()
                if getattr(self, "model_selection", None) is not None else None
            ),
        )

    def _setup_session_handlers(self):
        def handle_save(event: Event):
            if self.master._closing:
                return
            try:
                sid = self.save_session()
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

        async def handle_restore(event: Event):
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
                if restored is None:
                    raise ValueError("Restore returned no data")
                if (not isinstance(restored["todos"], list)
                        or any(not isinstance(todo, dict) for todo in restored["todos"])):
                    raise ValueError("Saved todos must be a list of objects")
                if restored["mode"] not in ("act", "plan"):
                    raise ValueError("Invalid saved operating mode")
                capture = restored.get("execution_capture") or {}
                if not isinstance(capture, dict) or (capture.get("request_id") is not None and not isinstance(capture["request_id"], str)):
                    raise ValueError("Invalid saved execution metadata")

                # Validate every replacement, including display history, without
                # disturbing live producers or an outstanding approval.
                from drx_agent.agent.forum import Forum
                from drx_agent.agent.claims import ClaimRegistry
                from drx_agent.agent.moderator import Moderator
                from drx_agent.agent.irc import IRC
                from drx_agent.agent.project_note import ProjectNote

                frontier = Frontier.from_dict(
                    restored["frontier"], max_intents=self.master.frontier.max_intents,
                )
                frontier.reconcile_restored()
                raw_handoff = restored["handoff"]
                handoff = Handoff.from_dict(raw_handoff) if raw_handoff else None
                stage = StageMachine.from_dict(restored["stage"])
                forum = Forum.from_dict(restored["forum"])
                claims = ClaimRegistry.from_dict(restored["claims"])
                moderator = Moderator.from_dict(restored["moderator"])
                irc = IRC.from_dict(restored["irc"])
                project_note = ProjectNote.from_dict(restored["project_note"])
                ballot, members, run_id, residents = self.master._decode_team_state(
                    restored["team"], stage=stage.stage.value,
                )
                candidate = TranscriptLog(EventBus())
                try:
                    candidate.restore_messages(restored["messages"])
                    if restored["transcript"] is not None:
                        candidate.restore(restored["transcript"])
                    history = candidate.export()
                finally:
                    candidate.close()

                selection = getattr(self, "model_selection", None)
                saved_model = restored.get("model_selection")
                prepared_model = None
                if saved_model is not None:
                    if selection is None:
                        raise ValueError("恢复该会话需要先配置模型服务。")
                    prepared_model = selection.prepare_restore(saved_model)
                self._model_sequence = getattr(self, "_model_sequence", 0) + 1
                await self.master.prepare_restore()
                try:
                    # No await between the successful preflight barrier and commit.
                    if prepared_model is not None:
                        selected_model = selection.commit_restore(prepared_model)
                        self._apply_model_context(selected_model)
                    self.transcript.restore(history)

                    self.knowledge_base = restored["kb"]
                    self.master.knowledge_base = restored["kb"]
                    self.master.messages = restored["messages"]
                    self.master.todos = restored["todos"]
                    self.master.frontier = frontier
                    self.master.handoff = handoff
                    self.master.stage_machine = stage
                    self.master.forum = forum
                    self.master.claims = claims
                    self.master.moderator = moderator
                    self.master.irc = irc
                    self.master.project_note = project_note
                    self.master.mode = restored["mode"]
                    self.master.session_usage = restored["session_usage"]
                    self.master._recent_request_ts.clear()
                    self.master.ballot = ballot
                    self.master._team_members = members
                    self.master._run_id = run_id
                    self.master._execution_request_id = capture.get("request_id")
                    self.master._resident_workers = residents
                finally:
                    self.master.finish_restore()

                self.event_bus.publish(Event(
                    type=EventType.SESSION_RESTORED,
                    data={"session_id": latest["id"]},
                ))
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
                        **usage_status(self.master.session_usage),
                        "rate": 0,
                        "mode": self.master.mode,
                        "active_targets": len(self.knowledge_base.list_targets()),
                    },
                ))
                self.event_bus.publish(Event(
                    type=EventType.AGENT_MESSAGE,
                    data={
                        "text": (
                            f"会话已恢复: {latest['name']} "
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
        self.event_bus.subscribe(
            EventType.SESSION_RESTORE,
            lambda event: self.master._schedule(handle_restore(event)),
        )


def main() -> int:
    """Entry point — create agent and launch TUI, or ``--web`` dashboard."""
    import argparse

    parser = argparse.ArgumentParser(
        description="DRX-Operator: Autonomous Red-Team Penetration Testing Expert System",
        add_help=True,
    )
    parser.add_argument(
        "--web", action="store_true",
        help="Launch the Web Dashboard (React Flow replay UI) instead of the TUI",
    )
    parser.add_argument(
        "--web-port", type=int, default=7300,
        help="Port for the web dashboard (default: 7300)",
    )
    parser.add_argument(
        "--web-host", default="0.0.0.0",
        help="Bind host for the web dashboard (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--no-agent", action="store_true",
        help="(Web mode only) Start in replay-only mode without a live agent",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="(Web mode only) Don't open the browser automatically",
    )
    args = parser.parse_args()

    if args.web:
        from drx_agent.web.launcher import launch_web
        launch_web(
            host=args.web_host,
            port=args.web_port,
            with_agent=not args.no_agent,
            open_browser=not args.no_browser,
        )
        return 0

    agent = DrxAgent()
    app = DrxAgentApp(agent.event_bus, drx_agent=agent)
    app.run()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())

