"""Master Agent — autonomous ReAct loop for red-team penetration testing.

Implements the Plan -> Think -> Act -> Observe -> Reflect decision loop with
evidence-driven analysis, sub-agent dispatch, approval flow, and full
integration with all DRX-Operator subsystems.
"""

import asyncio
import difflib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from drx_agent.agent.blackboard import Blackboard, SECTIONS
from drx_agent.agent.finding import Evidence, Finding
from drx_agent.agent.knowledge_base import Credential
from drx_agent.agent.artifact_store import ArtifactStore
from drx_agent.agent.prompts import METHODOLOGY_PROMPT, SUB_AGENT_DISCIPLINE
from drx_agent.agent.sub_agent import SubAgent, SubAgentResult, SubAgentStatus
from drx_agent.agent.frontier import Frontier
from drx_agent.agent.task_scheduler import TaskPriority, TaskScheduler
from drx_agent.engine.bash_sandbox import BashSandbox, BLOCKED_PATTERNS
from drx_agent.engine.python_sandbox import PythonSandbox, SandboxResult
from drx_agent.engine.script_library import ScriptLibrary
from drx_agent.engine.oob_listener import OOBListener
from drx_agent.engine.shell_session import ShellSessionManager
from drx_agent.hooks.manager import HookManager
from drx_agent.mcp.manager import MCPManager
from drx_agent.event_bus import Event, EventBus, EventType
from drx_agent.safety.gate import CheckResult, RiskLevel, SafetyGate
from drx_agent.safety.permissions import PermissionEngine
from drx_agent.skills.registry import SkillsRegistry

logger = logging.getLogger(__name__)

# Confirmation phrase required to authorize L4 (destructive / irreversible)
# operations. Only this exact phrase approves; "y"/"n"/anything else denies.
DESTROY_CONFIRMATION_PHRASE = "I CONFIRM DESTRUCTIVE ACTION"

# After this many consecutive tool calls without a new Finding, inject an
# Observer review with a causal replay of recent history (no auto-kill).
STUCK_TICK_THRESHOLD = 6


class MasterAgent:
    """Autonomous master agent driving a ReAct loop; integrates EventBus,
    TaskScheduler, sandboxes, KnowledgeBase, SafetyGate, SkillsRegistry
    and ScriptLibrary."""

    # Plan mode: readonly tools only. Mutating tools (write/edit/exec/shells/
    # destructive sub-agents) are rejected so the LLM observes and proposes.
    _PLAN_MODE_READONLY_TOOLS: set = {
        "read_file", "grep", "http_fetch", "web_search", "cve_lookup",
        "parse_nmap", "parse_http", "todo_write", "shell_list",
        "list_findings", "blackboard_read",
    }

    def __init__(
        self,
        event_bus: EventBus,
        scheduler: TaskScheduler,
        python_sandbox: PythonSandbox,
        bash_sandbox: BashSandbox,
        knowledge_base: Any,
        safety_gate: SafetyGate,
        skills_registry: SkillsRegistry,
        script_library: ScriptLibrary,
        llm_provider: Any = None,
        mcp_manager: Optional[MCPManager] = None,
        hooks: Optional[HookManager] = None,
    ) -> None:
        self.event_bus = event_bus
        self.scheduler = scheduler
        self.python_sandbox = python_sandbox
        self.bash_sandbox = bash_sandbox
        self.knowledge_base = knowledge_base
        if getattr(self.knowledge_base, "blackboard", None) is None:
            self.knowledge_base.blackboard = Blackboard()
        self.safety_gate = safety_gate
        self.skills_registry = skills_registry
        self.script_library = script_library
        self.llm_provider = llm_provider
        self.mcp = mcp_manager or MCPManager({})
        self.hooks = hooks or HookManager()
        # Rolling history; the system prompt is prepended on every call so it can be re-tuned without resetting history.
        self.messages: list[dict] = []

        # ReAct loop runs by default via the EventBus; start()/stop() only pause externally.
        self.running = True
        self.active_sub_agents: dict[str, SubAgent] = {}
        self.active_sub_agent_tasks: dict[str, asyncio.Task] = {}
        self.frontier: Frontier = Frontier()
        self._current_intent_id: str | None = None
        self._recent_tool_keys: list[tuple[str, str]] = []
        self._stuck_ticks: int = 0
        self._stuck_fact_baseline: int = 0
        self._pending_observer_msg: str | None = None
        self._script_counter = 0
        self._retry_counts: dict[str, int] = {}
        # After this many tool calls in one turn, ask the user to continue
        # (0 disables); a parked Future resumes the awaiting coroutine.
        self.iteration_soft_threshold = 100
        self._iter_continue_future: Optional[asyncio.Future] = None
        self.todos: list[dict] = []
        # Context management — a layered filter/prune pipeline (NOT memory).
        # Trigger is the MODEL's real context window, not a fixed number.
        #   L1 result-storage : large tool results offloaded to disk + pointer
        #   L2 micro-compact   : dedup identical tool results, drop filler
        #   L3 session-memory  : a living nine-segment progress doc
        #   L4 full-compact    : LLM summary into the nine-segment structure
        #   L5 auto-extract    : artifact index surfaced + read_artifact tool
        #   L6 dream           : /dream — second-pass consolidation + prune
        #   L7 cross-agent     : sub-agent transcripts → shared artifact store
        self.context_window_fraction: float = 0.80
        self.context_compact_to_ratio: float = 0.5
        self.context_recent_budget_ratio: float = 0.4
        self.context_keep_recent: int = 6
        self.context_keep_recent_tools: int = 4
        self.context_tool_result_cap: int = 800
        # L1: any single tool result longer than this (chars) is offloaded
        # to the artifact store on the way back to the model.
        self.artifact_offload_threshold: int = 4000
        self._compaction_inflight: bool = False
        self._compaction_count: int = 0
        # L3: the living progress document (nine-segment structure). Distinct
        # from the rolling narrative summary; updated on every full compaction.
        self._progress_doc: str = ""
        # Manual override; if 0 the model window is used automatically.
        self.context_token_limit: int = 0
        # Per-session model context-window override (llm.context_window); 0 → auto-detect.
        self.model_context_window_override: int = 0
        # L1/L7 artifact store (disk-backed). storage_dir wired in after init.
        self.artifacts = ArtifactStore(
            base_dir=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "sessions", "artifacts",
            )
        )
        self.shells = ShellSessionManager(max_sessions=8)
        self.oob = OOBListener()
        self.session_usage: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cost_usd": 0.0,
            "requests": 0,
            "by_model": {},
        }
        self._recent_request_ts: list[float] = []
        # Permission engine (allow/ask/deny per tool) — independent of the L0-L4 SafetyGate.
        self.permissions = PermissionEngine()
        self._tool_approval_future: Optional[asyncio.Future] = None
        self._tool_approval_args: Optional[tuple[str, str]] = None
        self._safety_approval_future: Optional[asyncio.Future] = None
        self._safety_approval_request_id: Optional[str] = None
        self._safety_approval_requires_phrase: bool = False
        # Operating mode: 'act' (default) or 'plan' (readonly tools); switch via /plan /act.
        self.mode: str = "act"
        # A new directive or /stop sets _interrupt; the loop checks it at the top of each iteration and bails out cleanly.
        self._chat_active: bool = False
        self._interrupt: bool = False
        # Serializes the ReAct loop: a steering message waits for the current
        # loop to release the lock, so tool_call/tool pairs can't interleave.
        self._chat_lock: Optional[asyncio.Lock] = None
        self._safety_approval_lock: asyncio.Lock = asyncio.Lock()
        self._tool_approval_lock: asyncio.Lock = asyncio.Lock()
        # Project memory from DRX.md / AGENTS.md, appended to the system prompt every turn.
        self.project_memory: str = self._load_project_memory()
        self.project_memory_path: Optional[Path] = self._project_memory_path()

        self.event_bus.subscribe(EventType.AGENT_MESSAGE, self._on_agent_message)
        self.event_bus.subscribe(
            EventType.APPROVAL_RESPONSE, self._on_approval_response
        )


    async def start(self) -> None:
        
        self.running = True
        self.event_bus.publish(
            Event(
                type=EventType.STATUS_UPDATE,
                data={"status": "started", "message": "Master Agent started"},
            )
        )
        logger.info("MasterAgent started")

    async def stop(self) -> None:
        
        self.running = False
        self.event_bus.publish(
            Event(
                type=EventType.STATUS_UPDATE,
                data={"status": "stopped", "message": "Master Agent stopped"},
            )
        )
        logger.info("MasterAgent stopped")


    def _on_agent_message(self, event: Event) -> None:
        
        role = event.data.get("role", "")
        if role == "assistant":
            return
        source = event.data.get("source", "user")
        if source != "user":
            return
        self._schedule(self._handle_user_message(event))

    def _on_approval_response(self, event: Event) -> None:
        
        self._schedule(self._handle_approval_response(event))

    @staticmethod
    def _schedule(coro) -> None:
        
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            try:
                asyncio.run(coro)
            except Exception:
                logger.exception("Failed to run coroutine without active loop")


    async def _handle_user_message(self, event: Event) -> None:
        
        text = event.data.get("text", "").strip()
        image_path = event.data.get("image_path")

        if (
            self._safety_approval_future is not None
            and not self._safety_approval_future.done()
            and self._safety_approval_requires_phrase
            and text
            and not text.startswith("/")
        ):
            req_id = self._safety_approval_request_id
            if text == DESTROY_CONFIRMATION_PHRASE:
                if req_id:
                    self.safety_gate.approve(req_id)
                self._safety_approval_future.set_result(True)
                self.publish_action("破坏性操作已确认。")
            else:
                if req_id:
                    self.safety_gate.deny(req_id)
                self._safety_approval_future.set_result(False)
                self.publish_action(
                    f"确认短语不匹配，操作已拒绝。请重新发起操作并输入精确短语："
                    f"「{DESTROY_CONFIRMATION_PHRASE}」"
                )
            return

        if text in ("/stop", "/cancel", "/interrupt"):
            if self._chat_active:
                self._signal_interrupt()
                self.publish_action("⏹ 已请求停止当前任务…")
            else:
                self.publish_action("当前没有正在运行的任务。")
            return

        # If a loop is already running, signal _interrupt and let the chat
        # lock serialize — no polling, no race, no concurrent loops.
        if self._chat_active and (text or image_path):
            self._signal_interrupt()
            self.publish_action("⏹ 收到新指令，正在中断当前任务并接管…")

        if image_path and self.llm_provider is not None:
            await self._chat_with_image(text, image_path)
            return
        if not text:
            return

        if text.startswith("/scan"):
            await self._handle_scan_command(text)
        elif text.startswith("/exploit"):
            await self._handle_exploit_command(text)
        elif text.startswith("/target"):
            await self._handle_target_command(text)
        elif text.startswith("/status"):
            await self._handle_status_command(text)
        elif text == "/plan":
            self.mode = "plan"
            self.publish_action(
                "已切换到 plan 模式：只允许只读工具（read_file/grep/web_search/"
                "cve_lookup/http_fetch/parse_*/todo_write）。输入 /act 恢复全部工具。"
            )
        elif text == "/act":
            self.mode = "act"
            self.publish_action("已切换到 act 模式：全部工具可用。")
        elif text == "/mode":
            self.publish_action(
                f"当前模式: {self.mode}（{'只读工具' if self.mode == 'plan' else '所有工具'}）"
            )
        elif text == "/dream":
            await self._dream()
        elif text == "/progress":
            if self._progress_doc:
                self.publish_action("📄 进度文档（九段）:\n" + self._progress_doc[:2000])
            else:
                self.publish_action("还没有进度文档（上下文尚未触发深度压缩）。")
        elif text == "/context":
            budget = self._effective_input_budget()
            used = self._estimate_messages_tokens(self.messages)
            self.publish_action(
                f"上下文: {used}/{budget} tokens ({used*100//max(budget,1)}%) | "
                f"模型窗口={self._model_window(self._current_model())} | "
                f"消息={len(self.messages)} | 压缩次数={self._compaction_count} | "
                f"产物={len(self.artifacts.list())}"
            )
        elif text == "/memory":
            if self.project_memory:
                preview = self.project_memory[:1000]
                trail = "…" if len(self.project_memory) > 1000 else ""
                self.publish_action(
                    f"📒 项目记忆 ({self.project_memory_path}):\n{preview}{trail}"
                )
            else:
                self.publish_action(
                    "没有找到项目记忆文件 — 在工作目录或父目录创建 "
                    "DRX.md / AGENTS.md / CLAUDE.md，然后 /memory reload。"
                )
        elif text == "/memory reload":
            ok = self.reload_project_memory()
            if ok:
                self.publish_action(
                    f"已重新加载项目记忆: {self.project_memory_path} "
                    f"({len(self.project_memory)} chars)"
                )
            else:
                self.publish_action("没有找到可加载的记忆文件。")
        elif self.llm_provider is not None:
            await self._chat_with_llm(text)
        else:
            self.publish_think(f"Processing user directive: {text}")
            await self._react_cycle("default", {"message": text})


    @property
    def blackboard(self) -> Blackboard:
        bb = getattr(self.knowledge_base, "blackboard", None)
        if bb is None:
            bb = Blackboard()
            self.knowledge_base.blackboard = bb
        return bb

    def _build_system_prompt(self) -> str:
        targets = self.knowledge_base.list_targets()
        target_summary = (
            "; ".join(f"{t['host']}(ports={len(t.get('open_ports', []))})" for t in targets)
            if targets else "none"
        )
        owned = len(self.knowledge_base.owned_targets())
        findings_lines = []
        for f_host, f_obj in self.knowledge_base.all_findings()[:12]:
            findings_lines.append(
                f"- [{f_obj.status}] {f_host}: {f_obj.claim[:80]}"
            )
        findings_summary = (
            "\n".join(findings_lines)
            or "(空 — 发现即用 record_finding 记录，假设有生命周期)"
        )
        memory_block = ""
        if self.project_memory:
            memory_block = (
                "\n【项目记忆 — 操作员预先设定的指令，必须遵守】\n"
                f"{self.project_memory}\n"
                f"(来自 {self.project_memory_path})\n"
            )
        model_name = self._current_model() or "未知模型"
        return (
            "你是 DRX-Operator，一个自主红队渗透测试专家系统。\n"
            f"你由 {model_name} 模型驱动。\n"
            "【身份】当被问及「你是谁 / 你是什么模型」时，"
            f"如实回答：你是 DRX-Operator，底层模型是 {model_name}。\n"
            "默认使用简体中文与用户交流（除非用户明确使用其他语言）。\n"
            "\n"
            "【严禁幻觉】你的训练数据是静态、可能过期、且不包含具体网站的实时内容。\n"
            "当用户要求你查看、阅读、分析、抓取任何 URL / 网页 / 网络资源时，你**必须**\n"
            "调用工具（http_fetch 或 execute_bash 配合 curl）实际抓取，再基于真实返回\n"
            "内容回答。**严禁**凭印象编造网页内容、文章标题、作者、发布日期等。\n"
            "如果工具调用失败，如实告诉用户失败原因，不要伪造结果。\n"
            "\n"
            "【可用工具】(必须通过 tool call 调用，不要把工具名写在文本里)\n"
            "网络:\n"
            "- http_fetch(url, method?, headers?, body?)：抓取任意 URL，返回状态码+正文。\n"
            "- web_search(query, max_results?)：用搜索引擎搜信息，返回 title/url/snippet 列表。\n"
            "  查 CVE/PoC/目标背景/最新漏洞时优先用，不要凭训练记忆答。\n"
            "- cve_lookup(cve_id)：直接查 NVD 数据库，返回 description/CVSS/CWE/refs。\n"
            "  收到 CVE 编号时必须用这个，比 web_search 准。\n"
            "执行:\n"
            "- execute_bash(command)：**一次性**白名单沙箱，无状态。\n"
            "- execute_python(code)：一次性 Python 沙箱，stdout 即返回值。\n"
            "持久 Shell（需要保留状态/交互/SSH/反弹 shell 时用，execute_bash 不行）:\n"
            "- shell_open(command, name?)：spawn 一个持久 PTY shell，返回 session_id。\n"
            "  典型用法：shell_open('ssh user@host')、shell_open('bash')、shell_open('nc -lvnp 4444')\n"
            "- shell_exec(session_id, input, timeout?, idle_timeout?)：发命令、读输出。\n"
            "- shell_signal(session_id, signal?)：发信号（默认 SIGINT，中断卡住的命令）。\n"
            "- shell_close(session_id) / shell_list()：关闭 / 列出活跃会话。\n"
            "结构化解析（消除自己读输出的猜测）:\n"
            "- parse_nmap(output, update_kb?)：把 nmap XML/文本解析成 hosts[ports[]]。\n"
            "  跑完 nmap **必须**走这个再写 KB。\n"
            "- parse_http(raw)：把原始 HTTP 请求/响应文本解析成 headers/body 等字段。\n"
            "上下文产物:\n"
            "- read_artifact(artifact_id, offset?, limit?)：大的工具输出会被自动存档，\n"
            "  正文里留下 artifact://<id> 指针。需要看全文时用这个取回（支持分页）。\n"
            "  看到 artifact:// 不要假装知道内容，要 read_artifact 拿真实数据。\n"
            "文件 (相对路径基于当前工作目录):\n"
            "- read_file(path, offset?, limit?)：读文件，返回带行号的内容。默认读 2000 行。\n"
            "- write_file(path, content)：创建或覆盖文件，自动展示 diff。\n"
            "- edit_file(path, old_string, new_string)：精确替换。old_string 必须唯一匹配。\n"
            "- multi_edit_file(path, edits)：一次性应用多个 {old_string, new_string} 编辑。\n"
            "- grep(pattern, path?, glob?, max_results?)：跨文件正则搜索，返回 file+line+text。\n"
            "规划与协作:\n"
            "- todo_write(todos)：写入 / 更新 todo 列表（{content, status} 数组），侧栏会显示。\n"
            "  做多步任务时先开 todo，每完成一项把 status 改成 completed。\n"
            "- task(description, agent_type?)：派发子任务给一个独立的子 Agent，它有自己的\n"
            "  消息历史，调用工具完成任务后返回结果。复杂、可拆分的子任务用这个。\n"
            "- generate_report(path?, format?, title?)：把会话产出汇总成 Markdown / HTML\n"
            "  报告写入磁盘。在用户说『出报告/写报告/总结成文档』时调用。\n"
            "知识库:\n"
            "- update_target(host, info)：把发现的端口/服务/版本写入知识库；\n"
            "  完全控制目标时传 owned=true。\n"
            "- record_finding(host, claim, evidence?, confidence?, severity?, cve?, status?)：\n"
            "  记录发现/假设。status 三态：suspected(疑似)/confirmed(证实)/exploited(已利用)，\n"
            "  evidence 数组放工具返回的关键数据。发现即记录，拿到证据就推进状态。\n"
            "- update_finding_status(host, claim, status)：推进假设生命周期。\n"
            "- list_findings(host?)：列出已记录的发现。\n"
            "- cred_list / cred_show：查看凭据库。\n"
            "- dispatch_sub_agent(agent_type, target, task)：派发 recon/exploit/lateral\n"
            "  /persist/report 子 Agent（红队场景专用）。\n"
            "黑板报（全体 Agent 共享的作战状态）:\n"
            "- blackboard_write(section, text)：上板。section 取值：objective(作战目标)/\n"
            "  findings(已确认发现)/hypotheses(待验证假设)/dead_ends(已尝试死路，禁止重复)/\n"
            "  credentials(凭据)/next_steps(下一步计划)。\n"
            "- blackboard_read(section?)：读某一区或全部。\n"
            "  重要进展随手上板；派子 Agent 前先看黑板；死路必须上板。\n"
            "作战账本（Operation Ledger — 意图前沿队列与死路账）:\n"
            "- intent_add(hypothesis, action, priority?, max_steps?, expiry_s?, depends_on?, evidence?)：\n"
            "  提出想验证的假设和动作，进入前沿队列。hypothesis 是断言，action 是计划。\n"
            "  依赖已记录 Finding 时用 depends_on 传 host::claim（来自 list_findings），\n"
            "  该 Finding 被推翻时会级联 kill 此意图。\n"
            "- intent_list()：查看前沿队列（open/claimed/done/dead）。\n"
            "- intent_claim(intent_id)：认领一个 open Intent 开始执行。\n"
            "- intent_done(intent_id, conclusion)：验证完成，写结论。\n"
            "- intent_kill(intent_id, reason)：此路不通，记死路（禁止重复）。\n"
            "  死胡同必须 intent_kill 而不是默默换方向；新想法必须 intent_add 而不是\n"
            "  只写在回复里。前沿视图每轮自动注入，认领后执行它。\n"
            "\n"
            f"{METHODOLOGY_PROMPT}\n"
            f"\n【当前模式】{self.mode}。在 plan 模式下只能用只读工具（read/grep/"
            "web_search/cve_lookup/http_fetch/parse_*/todo_write/list_findings/"
            "blackboard_read）；write/edit/exec/shell/dispatch 全部被拒。用户切到 /act 才能动手。\n"
            "\n"
            f"【当前知识库】targets=[{target_summary}], owned={owned}。\n"
            f"【发现(Findings)】\n{findings_summary}\n"
            "\n"
            f"{self.blackboard.render(2000)}\n"
            "\n"
            "高危操作（漏洞利用/横向移动/破坏性）需用户审批，先告知再执行。"
            + memory_block
        )

    def _build_tool_schemas(self) -> list[dict]:
        
        return [
            {
                "type": "function",
                "function": {
                    "name": "http_fetch",
                    "description": (
                        "通过 HTTP/HTTPS 抓取一个 URL 的内容。需要查看任何网页、"
                        "API 响应、文章内容时使用。返回 status_code、headers、body 文本。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "完整 URL"},
                            "method": {
                                "type": "string",
                                "enum": ["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS"],
                                "description": "HTTP 方法，默认 GET",
                            },
                            "headers": {
                                "type": "object",
                                "description": "可选请求头，key/value 都是字符串",
                            },
                            "body": {
                                "type": "string",
                                "description": "可选请求体（用于 POST/PUT）",
                            },
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "intent_add",
                    "description": (
                        "向作战账本的前沿队列提出一个新意图：想验证的假设 + 计划动作。"
                        "hypothesis 是断言（如『登录页有 SQLi』），action 是验证方式。"
                        "依赖已记录 Finding 时用 depends_on 传 host::claim，该 Finding "
                        "被推翻（retracted）时依赖它的 Intent 会被自动 kill。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "hypothesis": {"type": "string", "description": "想验证的断言"},
                            "action": {"type": "string", "description": "计划怎么验证"},
                            "priority": {"type": "integer", "description": "1-5，1 最高，默认 3"},
                            "max_steps": {"type": "integer", "description": "预算步数，默认 8"},
                            "expiry_s": {"type": "number", "description": "过期秒数，默认 900"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "依赖的 Finding，格式 host::claim（来自 list_findings）",
                            },
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "证据引用（artifact://id 或工具输出摘要）",
                            },
                        },
                        "required": ["hypothesis", "action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "intent_list",
                    "description": "查看探索前沿队列（所有意图及其状态）。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "intent_claim",
                    "description": "认领一个 open 意图，表示现在开始执行它。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intent_id": {"type": "string", "description": "意图 id"}
                        },
                        "required": ["intent_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "intent_done",
                    "description": "意图验证完成，写入结论。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intent_id": {"type": "string"},
                            "conclusion": {"type": "string", "description": "结论/证据摘要"},
                        },
                        "required": ["intent_id", "conclusion"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "intent_kill",
                    "description": "意图走不通，标记为死路并记录原因（禁止重复尝试）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intent_id": {"type": "string"},
                            "reason": {"type": "string", "description": "为什么走不通"},
                        },
                        "required": ["intent_id", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_bash",
                    "description": (
                        "在白名单 bash 沙箱中执行命令。可用命令：nmap, curl, dig, whois, "
                        "sqlmap, nikto, hydra, gobuster, wget, openssl, nc, ping, "
                        "traceroute, ssh, telnet。返回 stdout/stderr/exit_code。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "完整 bash 命令字符串",
                            }
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_python",
                    "description": (
                        "在 Python 沙箱中执行代码（最长 60s，256MB）。可用 socket、ssl、"
                        "urllib、requests、re、json、base64、hashlib；禁用 os、subprocess、"
                        "shutil、ctypes。stdout 即返回内容，最好用 print(json.dumps(...))。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "Python 源码",
                            }
                        },
                        "required": ["code"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_target",
                    "description": (
                        "把发现的目标信息写入知识库（端口/服务/版本/备注）。"
                        "完全控制目标时传 owned=true。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "open_ports": {
                                "type": "array",
                                "items": {"type": "integer"},
                            },
                            "services": {
                                "type": "object",
                                "description": "key=端口字符串，value=服务名/版本",
                            },
                            "owned": {
                                "type": "boolean",
                                "description": "已完全控制该目标时置 true",
                            },
                            "notes": {"type": "string"},
                        },
                        "required": ["host"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "record_finding",
                    "description": (
                        "记录一条发现/假设到知识库。status 三态：suspected(疑似，"
                        "刚观察到可疑点)/confirmed(证实，有明确证据)/exploited(已利用"
                        "成功)。evidence 数组放工具返回的关键数据片段。发现即记录，"
                        "拿到新证据就用 update_finding_status 推进。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "claim": {
                                "type": "string",
                                "description": "一句话结论，如 'download.php 存在路径遍历'",
                            },
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "支撑证据片段（工具输出的关键行）",
                            },
                            "confidence": {"type": "number"},
                            "severity": {
                                "type": "string",
                                "enum": ["info", "low", "medium", "high", "critical"],
                            },
                            "cve": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["suspected", "confirmed", "exploited"],
                            },
                        },
                        "required": ["host", "claim"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_finding_status",
                    "description": (
                        "推进假设生命周期：suspected → confirmed → exploited → retracted。"
                        "claim 填要更新的原发现的子串即可。"
                        "retracted 会级联 kill 依赖该发现的 Intent。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "claim": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["suspected", "confirmed", "exploited", "retracted"],
                            },
                            "superseded_by": {"type": "string"},
                        },
                        "required": ["host", "claim", "status"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_findings",
                    "description": "列出已记录的发现（可按 host 过滤），含状态/严重度/CVE。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "blackboard_write",
                    "description": (
                        "写黑板报（全体 Agent 共享作战状态）。section：objective(作战"
                        "目标)/findings(已确认发现)/hypotheses(待验证假设)/dead_ends("
                        "已尝试死路——上板后全员禁止重复)/credentials(凭据)/next_steps("
                        "下一步计划)。重要进展随手记。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "section": {
                                "type": "string",
                                "enum": list(SECTIONS.keys()),
                            },
                            "text": {"type": "string"},
                            "author": {
                                "type": "string",
                                "description": "署名（如 master / recon-ab12）",
                            },
                        },
                        "required": ["section", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "blackboard_read",
                    "description": (
                        "读黑板报。不传 section 返回全部；传了返回该区完整条目。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cred_add",
                    "description": (
                        "把发现的凭据存入凭据库。常见用法：拿到 /etc/shadow 后逐条 cred_add；"
                        "爆破成功后 cred_add(verified=true)。type 取值：password/hash/token/"
                        "key/ssh-key。同一 (host,user,service,port,secret) 会去重。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "username": {"type": "string"},
                            "secret": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": ["password", "hash", "token", "key", "ssh-key"],
                            },
                            "service": {"type": "string"},
                            "port": {"type": "integer"},
                            "source": {"type": "string"},
                            "verified": {"type": "boolean"},
                            "notes": {"type": "string"},
                        },
                        "required": ["host", "username", "secret"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cred_list",
                    "description": (
                        "列出凭据库中所有（或指定 host 的）凭据。返回 id/username/secret"
                        "（截断）/type/service/port/verified/source。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "verified_only": {"type": "boolean"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cred_verify",
                    "description": (
                        "把一条凭据标记为 verified（确认登录成功后调用）。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "username": {"type": "string"},
                            "service": {"type": "string"},
                            "port": {"type": "integer"},
                        },
                        "required": ["host", "username"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cred_show",
                    "description": (
                        "取回凭据库中某一条凭据的**完整** secret（cred_list 只返回截断预览）。"
                        "index 是 cred_list 返回数组中的条目序号（从 0 开始）。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {
                                "type": "integer",
                                "description": "cred_list 返回的条目序号（从 0 开始）",
                            },
                        },
                        "required": ["index"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dispatch_sub_agent",
                    "description": "派发一个子 Agent 执行子任务。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_type": {
                                "type": "string",
                                "enum": ["recon", "exploit", "lateral", "persist", "report"],
                            },
                            "target": {"type": "string"},
                            "task": {"type": "string"},
                        },
                        "required": ["agent_type", "target", "task"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "读本地文件，返回带行号的文本。默认从第 1 行开始，最多 2000 行。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "文件路径（相对或绝对）"},
                            "offset": {"type": "integer", "description": "起始行号 (0-based)，默认 0"},
                            "limit": {"type": "integer", "description": "最多读多少行，默认 2000"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": (
                        "创建或覆盖文件。会自动展示 diff。对于已存在的文件应该先 read_file 看一眼"
                        "再决定是否覆盖。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": (
                        "在文件中做一次精确字符串替换。old_string 必须在文件中**恰好出现一次**"
                        "（否则会拒绝，提示加上下文）。返回应用后的 diff。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_string": {
                                "type": "string",
                                "description": "要被替换的原文本（必须包含足够上下文以唯一匹配）",
                            },
                            "new_string": {"type": "string", "description": "新文本"},
                        },
                        "required": ["path", "old_string", "new_string"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "multi_edit_file",
                    "description": (
                        "一次性对同一个文件应用多个编辑（按顺序）。任何一个失败则全部回滚。"
                        "每个 edit 默认要求 old_string 唯一匹配；设 replace_all=true 时全替换。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "edits": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "old_string": {"type": "string"},
                                        "new_string": {"type": "string"},
                                        "replace_all": {"type": "boolean"},
                                    },
                                    "required": ["old_string", "new_string"],
                                },
                            },
                        },
                        "required": ["path", "edits"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "grep",
                    "description": (
                        "在文件/目录中跨文件正则搜索。返回匹配的 file/line/text 列表。"
                        "默认搜索当前目录，pattern 是 Python re 语法。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Python 正则"},
                            "path": {"type": "string", "description": "起点目录或文件，默认 ."},
                            "glob": {"type": "string", "description": "文件 glob，默认 **/*"},
                            "max_results": {"type": "integer", "description": "默认 100"},
                            "ignore_case": {"type": "boolean", "description": "大小写不敏感"},
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "todo_write",
                    "description": (
                        "写入 / 更新 todo 列表。每项是 {content, status: pending|in_progress|completed}。"
                        "多步任务开始前先建 todo，每完成一项就把状态改成 completed。todo 会显示在侧栏。"
                        "传入的数组**整体替换**当前 todo 列表。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "todos": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "content": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": ["pending", "in_progress", "completed"],
                                        },
                                    },
                                    "required": ["content", "status"],
                                },
                            },
                        },
                        "required": ["todos"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": (
                        "用搜索引擎查询信息，返回 N 条 {title, url, snippet}。"
                        "查 CVE / PoC / 目标背景 / 新闻 / 文档时**必须**用此工具，而不是凭记忆。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "max_results": {"type": "integer", "description": "默认 10"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cve_lookup",
                    "description": (
                        "查询权威 CVE 数据库（NVD API 2.0），返回该 CVE 的完整结构化"
                        "信息：description、CVSS v3/v2 分数、affected products、references。"
                        "比 web_search 准、比 LLM 记忆靠谱。**遇到 CVE 编号必须用这个**。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cve_id": {
                                "type": "string",
                                "description": "形如 CVE-2021-44228",
                            },
                        },
                        "required": ["cve_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shell_open",
                    "description": (
                        "打开一个**持久** PTY shell 会话。命令任意，常见用法：\n"
                        "  shell_open('bash') — 本地交互 bash（cwd/env/history 保留）\n"
                        "  shell_open('ssh user@host') — SSH 到远程主机\n"
                        "  shell_open('nc -lvnp 4444') — 起反弹 shell listener\n"
                        "返回 session_id，后续用 shell_exec 发命令。execute_bash 是一次性\n"
                        "的，无法用于需要多步交互或保留状态的场景，那种情况**必须**用 shell。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "spawn 命令"},
                            "name": {"type": "string", "description": "可选标签，便于 UI 显示"},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shell_exec",
                    "description": (
                        "向已打开的 shell 会话发送 input（自动追加换行）并读取输出。"
                        "默认在 0.4s 无新输出后返回。需要等久一点（启动服务、扫描）可调"
                        "高 timeout。input='' 则只读当前输出（peek）。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "input": {"type": "string"},
                            "timeout": {"type": "number", "description": "硬上限秒，默认 10"},
                            "idle_timeout": {"type": "number", "description": "空闲秒数后返回，默认 0.4"},
                        },
                        "required": ["session_id", "input"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shell_signal",
                    "description": "向 shell 会话发送信号（默认 SIGINT 中断当前前台进程）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "signal": {
                                "type": "string",
                                "description": "SIGINT/SIGTERM/SIGKILL/SIGQUIT 等",
                            },
                        },
                        "required": ["session_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shell_close",
                    "description": "关闭并清理一个 shell 会话。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                        },
                        "required": ["session_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shell_list",
                    "description": "列出当前所有活跃的 shell 会话。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "oob_start",
                    "description": (
                        "启动本地 HTTP 回调监听器，用于确认盲打/OOB 漏洞（SSRF、blind XSS、"
                        "Log4j、blind RCE 等）。返回 {callback_url, token}。把 callback_url "
                        "嵌入 payload，然后调用 oob_logs 查询命中。\n"
                        "注意：监听器默认绑在本机 0.0.0.0；如果目标在公网无法回连，需要 "
                        "ngrok/cloudflared 等工具开外网。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "port": {"type": "integer", "description": "省略则系统自选"},
                            "bind": {"type": "string", "description": "默认 0.0.0.0"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "oob_logs",
                    "description": (
                        "查询回调监听器收到的请求记录。返回 method/path/headers/body/"
                        "client/ts/token_match 数组。token_match=true 的是本会话 payload "
                        "触发的，其他是被动扫到的。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "since_ts": {"type": "number", "description": "只看时间戳之后的"},
                            "token_only": {"type": "boolean", "description": "只看 token 匹配的"},
                            "last_n": {"type": "integer", "description": "最近 N 条"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "oob_stop",
                    "description": "停止回调监听器（清掉端口占用）。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wordlist_list",
                    "description": (
                        "扫描常见路径找系统已安装的字典文件（SecLists / Kali / "
                        "/usr/share/wordlists 等），返回 {path, size, category} 列表。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "可选过滤：passwords/web/dns/usernames/...",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wordlist_top",
                    "description": (
                        "读取字典的前 N 行（默认 100）。注意大字典直接 read_file 会爆。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "n": {"type": "integer", "description": "默认 100"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_artifact",
                    "description": (
                        "取回被上下文压缩存档的完整内容。当你看到 artifact://<id> 指针、"
                        "或进度文档/产物索引里列出的 id 时，用这个拉回全文。支持 offset/limit "
                        "分页（大产物别一次全拉）。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "artifact_id": {"type": "string", "description": "形如 a1b2c3d4"},
                            "offset": {"type": "integer", "description": "起始字符，默认 0"},
                            "limit": {"type": "integer", "description": "最多取多少字符，默认 6000"},
                        },
                        "required": ["artifact_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "parse_nmap",
                    "description": (
                        "把 nmap 的 XML 或正常文本输出解析成结构化 JSON："
                        "{hosts: [{host, hostnames, ports: [{port, protocol, service, "
                        "product, version}], os}]}。比让 LLM 自己读 nmap 输出**靠谱得多**，"
                        "做完端口扫描后**必须**用这个工具消化结果再写 KB。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "output": {"type": "string", "description": "nmap 的 stdout/XML"},
                            "update_kb": {
                                "type": "boolean",
                                "description": "解析完是否自动 update_target 写知识库",
                            },
                        },
                        "required": ["output"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "parse_http",
                    "description": (
                        "解析原始 HTTP 请求或响应文本（headers + body），返回 "
                        "{kind, method/status, path/reason, headers, body, body_length}。"
                        "看抓包、Burp 复制的 raw request、curl -v 输出时用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "raw": {"type": "string"},
                        },
                        "required": ["raw"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_report",
                    "description": (
                        "把本次会话已发现的目标、漏洞、findings、命令历史汇总成一份"
                        "Markdown 渗透测试报告写入磁盘。可选 format='markdown'（默认）"
                        "或 'html'。返回报告内容与文件路径。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "输出文件路径；省略则自动生成 reports/report-<ts>.md",
                            },
                            "format": {
                                "type": "string",
                                "enum": ["markdown", "html"],
                                "description": "默认 markdown",
                            },
                            "title": {"type": "string"},
                            "include_session_usage": {
                                "type": "boolean",
                                "description": "默认 true，包含 token/成本统计",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "task",
                    "description": (
                        "派发一个独立的子 Agent 来完成一个可拆分的子任务。子 Agent 有自己的"
                        "消息历史、自己的工具调用循环，最终把结果汇报回来。适合：1) 需要多轮工"
                        "具调用但与主任务无关的子目标；2) 需要并行探索的方向。**子 Agent 不能"
                        "递归调用 task**。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": "完整、自包含的任务描述（子 Agent 看不到主对话上下文）",
                            },
                            "agent_type": {
                                "type": "string",
                                "description": "可选标签（research/analyze/scan/etc.）用于 UI 展示",
                            },
                        },
                        "required": ["description"],
                    },
                },
            },
        ] + self.mcp.openai_tool_schemas()


    def _safety_risk_level(self, name: str, args: dict) -> Optional[RiskLevel]:
        if name == "dispatch_sub_agent":
            agent_type = str(args.get("agent_type") or "recon").lower()
            if agent_type == "exploit":
                return RiskLevel.L2
            if agent_type in ("lateral", "persist"):
                return RiskLevel.L3
            return RiskLevel.L1
        if name == "execute_bash":
            command = str(args.get("command") or "").lower()
            for pattern in BLOCKED_PATTERNS:
                if pattern.lower() in command:
                    return RiskLevel.L4
            return RiskLevel.L1
        if name in ("execute_python", "write_file", "edit_file", "multi_edit_file", "shell_exec"):
            return RiskLevel.L1
        return None

    def _safety_operation(self, name: str, args: dict) -> str:
        if name == "dispatch_sub_agent":
            return f"dispatch_sub_agent:{args.get('agent_type', 'recon')}"
        return name

    def _safety_target(self, name: str, args: dict) -> str:
        if name == "dispatch_sub_agent":
            return str(args.get("target") or "unknown")
        if name in ("write_file", "edit_file", "multi_edit_file"):
            return str(args.get("path") or "(local)")
        if name == "shell_exec":
            return str(args.get("session_id") or "(local)")
        return "(local)"

    async def _execute_tool(self, name: str, args: dict) -> str:
        
        if name == "http_fetch":
            preview = f"{args.get('method', 'GET')} {args.get('url', '')}"
        elif name == "execute_bash":
            preview = str(args.get("command", ""))[:500]
        elif name == "execute_python":
            preview = str(args.get("code", ""))[:500]
        elif name == "update_target":
            preview = f"host={args.get('host', '?')}"
        elif name == "cred_add":
            preview = f"{args.get('host', '?')}/{args.get('username', '?')} ({args.get('type', 'password')})"
        elif name == "cred_list":
            preview = f"host={args.get('host', 'all')}"
        elif name == "cred_verify":
            preview = f"{args.get('host', '?')}/{args.get('username', '?')} ✓"
        elif name == "cred_show":
            preview = f"index={args.get('index', '?')}"
        elif name == "dispatch_sub_agent":
            preview = f"{args.get('agent_type', '?')} → {args.get('target', '?')}"
        elif name == "read_file":
            preview = f"path={args.get('path', '?')}"
        elif name == "write_file":
            preview = (
                f"path={args.get('path', '?')}  "
                f"({len(str(args.get('content', '')))} chars)"
            )
        elif name == "edit_file":
            preview = f"path={args.get('path', '?')}"
        elif name == "multi_edit_file":
            preview = (
                f"path={args.get('path', '?')}  "
                f"({len(args.get('edits') or [])} edits)"
            )
        elif name == "grep":
            preview = f"pattern={args.get('pattern', '?')!r}  path={args.get('path', '.')}"
        elif name == "todo_write":
            preview = f"{len(args.get('todos') or [])} todos"
        elif name == "web_search":
            preview = f"query={args.get('query', '?')!r}"
        elif name == "cve_lookup":
            preview = f"cve_id={args.get('cve_id', '?')}"
        elif name == "task":
            preview = str(args.get("description", ""))[:200]
        elif name == "generate_report":
            preview = (
                f"path={args.get('path', 'auto')} format={args.get('format', 'markdown')}"
            )
        elif name == "shell_open":
            preview = f"spawn: {args.get('command', '?')}"
        elif name == "shell_exec":
            preview = (
                f"[{args.get('session_id', '?')}] "
                f"{str(args.get('input', ''))[:200]}"
            )
        elif name == "shell_signal":
            preview = f"[{args.get('session_id', '?')}] {args.get('signal', 'SIGINT')}"
        elif name == "shell_close":
            preview = f"close {args.get('session_id', '?')}"
        elif name == "shell_list":
            preview = "list active shells"
        elif name == "oob_start":
            preview = f"bind={args.get('bind', '0.0.0.0')}:{args.get('port', 'auto')}"
        elif name == "oob_logs":
            preview = (
                f"last_n={args.get('last_n', 'all')} "
                f"token_only={bool(args.get('token_only', False))}"
            )
        elif name == "oob_stop":
            preview = "stop oob listener"
        elif name == "wordlist_list":
            preview = f"category={args.get('category', 'all')}"
        elif name == "wordlist_top":
            preview = f"path={args.get('path', '?')} n={args.get('n', 100)}"
        elif name == "read_artifact":
            preview = f"artifact={args.get('artifact_id', '?')}"
        elif name == "parse_nmap":
            preview = f"{len(args.get('output', ''))} chars of nmap output"
        elif name == "parse_http":
            preview = f"{len(args.get('raw', ''))} chars of raw HTTP"
        elif self.mcp.is_mcp_tool(name):
            parsed = self.mcp.parse_tool_name(name)
            head = f"{parsed[0]}:{parsed[1]}" if parsed else name
            preview = f"[mcp/{head}] " + json.dumps(args, ensure_ascii=False)[:200]
        else:
            preview = json.dumps(args, ensure_ascii=False)[:300]

        self._script_counter += 1
        call_seq = self._script_counter

        if self.mode == "plan" and name not in self._PLAN_MODE_READONLY_TOOLS:
            denial = {
                "error": (
                    f"plan mode: tool '{name}' is disabled — only read-only "
                    f"tools allowed. Tell the user your proposed plan; they "
                    f"can switch to act mode with /act."
                ),
                "mode": "plan",
            }
            self.event_bus.publish(
                Event(
                    type=EventType.TOOL_CALL,
                    data={"tool": name, "code": preview, "status": "error", "call_seq": call_seq},
                )
            )
            self.event_bus.publish(
                Event(
                    type=EventType.TOOL_RESULT,
                    data={
                        "tool": name,
                        "status": "error",
                        "output": json.dumps(denial, ensure_ascii=False),
                        "call_seq": call_seq,
                    },
                )
            )
            return json.dumps(denial, ensure_ascii=False)

        allow_destructive = False
        risk = self._safety_risk_level(name, args)
        if risk == RiskLevel.L4:
            operation = self._safety_operation(name, args)
            gate_target = self._safety_target(name, args)
            check = self.safety_gate.check(operation, risk, gate_target)
            if not check.approved:
                approved = await self._await_safety_approval(
                    request_id=check.request_id,
                    operation=operation,
                    risk_level=risk,
                    target=gate_target,
                    requires_approval=check.requires_approval,
                    requires_confirmation_phrase=check.requires_confirmation_phrase,
                )
                if not approved:
                    denial = {
                        "error": "destructive operation not confirmed by user",
                        "tool": name,
                    }
                    self.event_bus.publish(
                        Event(
                            type=EventType.TOOL_RESULT,
                            data={
                                "tool": name,
                                "status": "error",
                                "output": json.dumps(denial, ensure_ascii=False),
                                "call_seq": call_seq,
                            },
                        )
                    )
                    return json.dumps(denial, ensure_ascii=False)
            allow_destructive = True

        decision = self.permissions.check(name, args)
        if decision.action == "deny" and not allow_destructive:
            denial = {
                "error": "denied by permission rule",
                "tool": name,
                "rule": decision.reason,
            }
            self.event_bus.publish(
                Event(
                    type=EventType.TOOL_CALL,
                    data={"tool": name, "code": preview, "status": "error", "call_seq": call_seq},
                )
            )
            self.event_bus.publish(
                Event(
                    type=EventType.TOOL_RESULT,
                    data={
                        "tool": name,
                        "status": "error",
                        "output": json.dumps(denial, ensure_ascii=False),
                        "call_seq": call_seq,
                    },
                )
            )
            return json.dumps(denial, ensure_ascii=False)

        if decision.action == "ask" and not allow_destructive:
            approved = await self._ask_tool_permission(name, preview, decision)
            if not approved:
                denial = {
                    "error": "denied by user",
                    "tool": name,
                    "rule": decision.reason,
                }
                self.event_bus.publish(
                    Event(
                        type=EventType.TOOL_RESULT,
                        data={
                            "tool": name,
                            "status": "error",
                            "output": json.dumps(denial, ensure_ascii=False),
                            "call_seq": call_seq,
                        },
                    )
                )
                return json.dumps(denial, ensure_ascii=False)

        if risk is not None and risk != RiskLevel.L4:
            operation = self._safety_operation(name, args)
            gate_target = self._safety_target(name, args)
            check = self.safety_gate.check(operation, risk, gate_target)
            if not check.approved:
                approved = await self._await_safety_approval(
                    request_id=check.request_id,
                    operation=operation,
                    risk_level=risk,
                    target=gate_target,
                    requires_approval=check.requires_approval,
                    requires_confirmation_phrase=check.requires_confirmation_phrase,
                )
                if not approved:
                    denial = {"error": "tool execution denied by user", "tool": name}
                    self.event_bus.publish(
                        Event(
                            type=EventType.TOOL_RESULT,
                            data={
                                "tool": name,
                                "status": "error",
                                "output": json.dumps(denial, ensure_ascii=False),
                                "call_seq": call_seq,
                            },
                        )
                    )
                    return json.dumps(denial, ensure_ascii=False)

        self.event_bus.publish(
            Event(
                type=EventType.TOOL_CALL,
                data={"tool": name, "code": preview, "status": "running", "call_seq": call_seq},
            )
        )

        try:
            hook_returns = await self.hooks.dispatch(
                "pre_tool",
                {"tool": name, "args": args, "call_seq": call_seq, "preview": preview},
            )
        except Exception:
            hook_returns = []
        for ret in hook_returns:
            if isinstance(ret, dict) and ret.get("deny"):
                reason = str(ret.get("deny"))
                denial = {"error": f"denied by hook: {reason}", "tool": name}
                self.event_bus.publish(
                    Event(
                        type=EventType.TOOL_RESULT,
                        data={
                            "tool": name,
                            "status": "error",
                            "output": json.dumps(denial, ensure_ascii=False),
                            "call_seq": call_seq,
                        },
                    )
                )
                return json.dumps(denial, ensure_ascii=False)

        tool_start_t = time.time()
        result_text = ""
        status = "done"
        try:
            if name == "http_fetch":
                result_text = await asyncio.to_thread(
                    self._tool_http_fetch,
                    args.get("url", ""),
                    args.get("method", "GET"),
                    args.get("headers") or {},
                    args.get("body"),
                )
            elif name == "execute_bash":
                result_text = await asyncio.to_thread(
                    self._tool_execute_bash,
                    args.get("command", ""),
                    allow_destructive,
                )
            elif name == "execute_python":
                result_text = await asyncio.to_thread(
                    self._tool_execute_python, args.get("code", "")
                )
            elif name == "update_target":
                result_text = self._tool_update_target(args)
            elif name == "record_finding":
                result_text = self._tool_record_finding(args)
            elif name == "update_finding_status":
                result_text = self._tool_update_finding_status(args)
            elif name == "list_findings":
                result_text = self._tool_list_findings(args)
            elif name == "blackboard_write":
                result_text = self._tool_blackboard_write(args)
            elif name == "blackboard_read":
                result_text = self._tool_blackboard_read(args)
            elif name == "intent_add":
                result_text = self._tool_intent_add(args)
            elif name == "intent_list":
                result_text = self._tool_intent_list(args)
            elif name == "intent_claim":
                result_text = self._tool_intent_claim(args)
            elif name == "intent_done":
                result_text = self._tool_intent_done(args)
            elif name == "intent_kill":
                result_text = self._tool_intent_kill(args)
            elif name == "cred_add":
                result_text = self._tool_cred_add(args)
            elif name == "cred_list":
                result_text = self._tool_cred_list(args)
            elif name == "cred_verify":
                result_text = self._tool_cred_verify(args)
            elif name == "cred_show":
                result_text = self._tool_cred_show(args)
            elif name == "dispatch_sub_agent":
                result_text = await self._tool_dispatch_sub_agent(args)
            elif name == "read_file":
                result_text = self._tool_read_file(
                    args.get("path", ""),
                    int(args.get("offset", 0) or 0),
                    int(args.get("limit", 2000) or 2000),
                )
            elif name == "write_file":
                result_text = self._tool_write_file(
                    args.get("path", ""), args.get("content", "")
                )
            elif name == "edit_file":
                result_text = self._tool_edit_file(
                    args.get("path", ""),
                    args.get("old_string", ""),
                    args.get("new_string", ""),
                )
            elif name == "multi_edit_file":
                result_text = self._tool_multi_edit_file(
                    args.get("path", ""), args.get("edits") or []
                )
            elif name == "grep":
                result_text = await asyncio.to_thread(
                    self._tool_grep,
                    args.get("pattern", ""),
                    args.get("path", "."),
                    args.get("glob", "**/*"),
                    int(args.get("max_results", 100) or 100),
                    bool(args.get("ignore_case", False)),
                )
            elif name == "todo_write":
                result_text = self._tool_todo_write(args.get("todos") or [])
            elif name == "web_search":
                result_text = await asyncio.to_thread(
                    self._tool_web_search,
                    args.get("query", ""),
                    int(args.get("max_results", 10) or 10),
                )
            elif name == "cve_lookup":
                result_text = await asyncio.to_thread(
                    self._tool_cve_lookup, args.get("cve_id", "")
                )
            elif name == "task":
                result_text = await self._tool_task(
                    args.get("description", ""),
                    args.get("agent_type", "general"),
                )
            elif name == "generate_report":
                result_text = self._tool_generate_report(
                    path=args.get("path"),
                    fmt=args.get("format", "markdown"),
                    title=args.get("title", ""),
                    include_session_usage=bool(
                        args.get("include_session_usage", True)
                    ),
                )
            elif name == "shell_open":
                result_text = await asyncio.to_thread(
                    self._tool_shell_open,
                    args.get("command", ""),
                    args.get("name", ""),
                )
            elif name == "shell_exec":
                result_text = await asyncio.to_thread(
                    self._tool_shell_exec,
                    args.get("session_id", ""),
                    args.get("input", ""),
                    float(args.get("timeout", 10.0) or 10.0),
                    float(args.get("idle_timeout", 0.4) or 0.4),
                )
            elif name == "shell_signal":
                result_text = self._tool_shell_signal(
                    args.get("session_id", ""),
                    args.get("signal", "SIGINT"),
                )
            elif name == "shell_close":
                result_text = self._tool_shell_close(args.get("session_id", ""))
            elif name == "shell_list":
                result_text = self._tool_shell_list()
            elif name == "oob_start":
                result_text = self._tool_oob_start(
                    args.get("port"), args.get("bind", "0.0.0.0")
                )
            elif name == "oob_logs":
                result_text = self._tool_oob_logs(
                    float(args.get("since_ts", 0) or 0),
                    bool(args.get("token_only", False)),
                    args.get("last_n"),
                )
            elif name == "oob_stop":
                result_text = self._tool_oob_stop()
            elif name == "wordlist_list":
                result_text = await asyncio.to_thread(
                    self._tool_wordlist_list, args.get("category", "")
                )
            elif name == "wordlist_top":
                result_text = await asyncio.to_thread(
                    self._tool_wordlist_top,
                    args.get("path", ""),
                    int(args.get("n", 100) or 100),
                )
            elif name == "parse_nmap":
                result_text = self._tool_parse_nmap(
                    args.get("output", ""),
                    bool(args.get("update_kb", False)),
                )
            elif name == "parse_http":
                result_text = self._tool_parse_http(args.get("raw", ""))
            elif name == "read_artifact":
                result_text = self._tool_read_artifact(
                    args.get("artifact_id", ""),
                    int(args.get("offset", 0) or 0),
                    int(args.get("limit", 6000) or 6000),
                )
            elif self.mcp.is_mcp_tool(name):
                result_text = await self.mcp.call(name, args)
            else:
                result_text = json.dumps(
                    {"error": f"unknown tool: {name}"}, ensure_ascii=False
                )
                status = "error"

            try:
                parsed = json.loads(result_text)
                if isinstance(parsed, dict):
                    if parsed.get("error"):
                        status = "error"
                    elif parsed.get("status") in {"error", "timeout", "blocked", "memory_error"}:
                        status = "error"
            except (json.JSONDecodeError, TypeError):
                pass
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            result_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            status = "error"

        self.event_bus.publish(
            Event(
                type=EventType.TOOL_RESULT,
                data={
                    "tool": name,
                    "status": status,
                    "output": result_text[:8000],
                    "call_seq": call_seq,
                },
            )
        )
        if self._current_intent_id:
            self.frontier.tick(self._current_intent_id, 1)
            if not name.startswith("intent_"):
                key = (name, preview)
                self._recent_tool_keys.append(key)
                if len(self._recent_tool_keys) > 12:
                    self._recent_tool_keys.pop(0)
                if (
                    len(self._recent_tool_keys) >= 3
                    and self._recent_tool_keys[-3:] == [key, key, key]
                ):
                    self.frontier.kill(self._current_intent_id, "repeated_action")
                    self.publish_action(
                        f"⚠ 检测到连续 3 次相同动作 {name}，当前意图已剪枝，"
                        "请换路径或 intent_kill。"
                    )
                    self._set_current_intent(None)
                total = self.knowledge_base.finding_total()
                if total > self._stuck_fact_baseline:
                    self._stuck_fact_baseline = total
                    self._stuck_ticks = 0
                else:
                    self._stuck_ticks += 1
                    if self._stuck_ticks >= STUCK_TICK_THRESHOLD:
                        self._stuck_ticks = 0
                        self._flag_stuck_observer()
        try:
            await self.hooks.dispatch(
                "post_tool",
                {
                    "tool": name,
                    "args": args,
                    "call_seq": call_seq,
                    "status": status,
                    "result": result_text[:4000],
                    "duration_ms": (time.time() - tool_start_t) * 1000,
                },
            )
        except Exception:
            pass

        # L1 result-storage: huge tool results are offloaded to the artifact
        # store before entering the message list (read_artifact/read_file exempt).
        if (
            name not in ("read_artifact", "read_file")
            and isinstance(result_text, str)
            and len(result_text) > self.artifact_offload_threshold
        ):
            pointer = self.artifacts.make_pointer(
                result_text, tool=name,
                head=(self.artifact_offload_threshold * 2) // 3,
                tail=self.artifact_offload_threshold // 3,
            )
            if pointer is not None:
                return pointer
        return result_text

    def _tool_http_fetch(
        self, url: str, method: str, headers: dict, body: Optional[str]
    ) -> str:
        # NOTE: runs in a worker thread — do NOT publish EventBus events here
        # (Textual widgets aren't thread-safe); the orchestrator publishes on the loop.
        import ssl
        import urllib.request
        import urllib.error
        if not url:
            return json.dumps({"error": "url is required"}, ensure_ascii=False)
        try:
            data = body.encode("utf-8") if body else None
            req = urllib.request.Request(
                url=url, data=data, method=method or "GET",
                headers={"User-Agent": "DRX-Operator/0.5"},
            )
            for k, v in (headers or {}).items():
                req.add_header(str(k), str(v))
            try:
                resp = urllib.request.urlopen(req, timeout=30)
            except urllib.error.URLError as e:
                # macOS / older Pythons frequently lack a usable CA bundle.
                # Retry with an unverified SSL context so URL fetches still
                # work (we are a red-team tool — verification posture is
                # the user's call, not the library's).
                if "CERTIFICATE_VERIFY_FAILED" in str(e):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    resp = urllib.request.urlopen(req, timeout=30, context=ctx)
                else:
                    raise
            try:
                raw = resp.read()
                status_code = resp.status
                resp_headers = dict(resp.headers.items())
            finally:
                resp.close()
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = repr(raw[:2000])
            result = {
                "url": url,
                "method": method,
                "status_code": status_code,
                "headers": {k: resp_headers.get(k) for k in list(resp_headers)[:20]},
                "body": text[:8000],
                "body_truncated": len(text) > 8000,
                "body_length": len(text),
            }
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
            result = {
                "url": url,
                "method": method,
                "status_code": e.code,
                "error": str(e),
                "body": body_text[:8000],
            }
        except Exception as e:
            result = {"url": url, "error": str(e)}

        return json.dumps(result, ensure_ascii=False)

    def _tool_execute_bash(self, command: str, allow_destructive: bool = False) -> str:
        # Runs in a worker thread — see note in _tool_http_fetch.
        if not command:
            return json.dumps({"error": "command is required"}, ensure_ascii=False)
        res = self.bash_sandbox.run(command, allow_destructive=allow_destructive)
        return json.dumps(
            {
                "status": res.status,
                "exit_code": res.exit_code,
                "stdout": (res.stdout or "")[:6000],
                "stderr": (res.stderr or "")[:2000],
            },
            ensure_ascii=False,
        )

    def _tool_execute_python(self, code: str) -> str:
        # Runs in a worker thread — see note in _tool_http_fetch.
        if not code:
            return json.dumps({"error": "code is required"}, ensure_ascii=False)
        res = self.python_sandbox.run(code)
        return json.dumps(
            {
                "status": res.status,
                "exit_code": res.exit_code,
                "stdout": (res.stdout or "")[:6000],
                "stderr": (res.stderr or "")[:2000],
            },
            ensure_ascii=False,
        )

    def _tool_cred_add(self, args: dict) -> str:
        host = args.get("host")
        username = args.get("username")
        secret = args.get("secret")
        if not host or not username or secret is None:
            return json.dumps(
                {"error": "host, username, secret all required"},
                ensure_ascii=False,
            )
        cred = Credential(
            host=host,
            username=str(username),
            secret=str(secret),
            type=args.get("type", "password") or "password",
            service=args.get("service", "") or "",
            port=args.get("port"),
            source=args.get("source", "") or "",
            verified=bool(args.get("verified", False)),
            notes=args.get("notes", "") or "",
        )
        stored = self.knowledge_base.add_credential(cred)
        return json.dumps(
            {
                "ok": True,
                "id": stored.id,
                "host": stored.host,
                "username": stored.username,
                "type": stored.type,
                "service": stored.service,
                "port": stored.port,
                "verified": stored.verified,
                "deduped": stored.id != cred.id,
            },
            ensure_ascii=False,
        )

    def _tool_cred_list(self, args: dict) -> str:
        host = args.get("host") or None
        verified_only = bool(args.get("verified_only", False))
        creds = self.knowledge_base.list_credentials(host)
        if verified_only:
            creds = [c for c in creds if c.verified]
        out = []
        for c in creds:
            # Truncate secrets to avoid blowing the context window when the
            # vault holds hash dumps. The LLM can fetch the full secret via
            # cred_show(index).
            preview = c.secret[:64] + ("…" if len(c.secret) > 64 else "")
            out.append({
                "id": c.id,
                "host": c.host,
                "username": c.username,
                "secret_preview": preview,
                "secret_length": len(c.secret),
                "type": c.type,
                "service": c.service,
                "port": c.port,
                "source": c.source,
                "verified": c.verified,
                "ts": c.ts,
            })
        return json.dumps({"credentials": out, "count": len(out)}, ensure_ascii=False)

    def _tool_cred_show(self, args: dict) -> str:
        try:
            index = int(args.get("index", -1))
        except (TypeError, ValueError):
            return json.dumps({"error": "index must be an integer"}, ensure_ascii=False)
        creds = self.knowledge_base.list_credentials()
        if index < 0 or index >= len(creds):
            return json.dumps(
                {"error": f"index {index} out of range (0-{len(creds) - 1})"},
                ensure_ascii=False,
            )
        c = creds[index]
        return json.dumps(
            {
                "id": c.id,
                "host": c.host,
                "username": c.username,
                "secret": c.secret,
                "type": c.type,
                "service": c.service,
                "port": c.port,
                "source": c.source,
                "verified": c.verified,
            },
            ensure_ascii=False,
        )

    def _tool_cred_verify(self, args: dict) -> str:
        host = args.get("host")
        username = args.get("username")
        if not host or not username:
            return json.dumps(
                {"error": "host and username required"}, ensure_ascii=False
            )
        cred = self.knowledge_base.mark_credential_verified(
            host=host,
            username=username,
            service=args.get("service", "") or "",
            port=args.get("port"),
        )
        if cred is None:
            return json.dumps(
                {"error": f"no credential matches {host}/{username}"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": True, "id": cred.id, "verified": True}, ensure_ascii=False
        )

    def _tool_update_target(self, args: dict) -> str:
        host = args.get("host")
        if not host:
            return json.dumps({"error": "host required"}, ensure_ascii=False)
        update = {k: v for k, v in args.items() if k != "host" and v is not None}
        if update.pop("owned", False):
            self.knowledge_base.update_target(host)
            self.knowledge_base.mark_owned(host)
            self.blackboard.add("findings", f"{host}: 已完全控制(owned)", author="master")
        self.knowledge_base.update_target(host, **update)
        return json.dumps({"ok": True, "host": host, "updated": list(update.keys()) + (["owned"] if args.get("owned") else [])}, ensure_ascii=False)

    def _tool_record_finding(self, args: dict) -> str:
        host = args.get("host") or "unknown"
        claim = (args.get("claim") or "").strip()
        if not claim:
            return json.dumps({"error": "claim required"}, ensure_ascii=False)
        status = args.get("status") or "suspected"
        if status not in Finding.VALID_STATUSES:
            return json.dumps(
                {"error": f"status must be one of {Finding.VALID_STATUSES}"},
                ensure_ascii=False,
            )
        try:
            confidence = float(args.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        evidence = [
            Evidence(type="tool_output", value=str(e)[:300])
            for e in (args.get("evidence") or [])[:5]
        ]
        finding = Finding(
            claim=claim,
            confidence=confidence,
            evidence=evidence,
            cve=args.get("cve", "") or "",
            severity=args.get("severity", "info") or "info",
            status=status,
            verified=status in ("confirmed", "exploited"),
        )
        self.knowledge_base.add_finding(host, finding)
        board_section = "hypotheses" if status == "suspected" else "findings"
        self.blackboard.add(board_section, f"{host}: {claim[:120]} [{status}]", author="master")
        return json.dumps(
            {"ok": True, "host": host, "status": status, "claim": claim[:120]},
            ensure_ascii=False,
        )

    def _tool_update_finding_status(self, args: dict) -> str:
        host = args.get("host") or ""
        claim = args.get("claim") or ""
        status = args.get("status") or ""
        superseded_by = args.get("superseded_by") or ""
        finding = self.knowledge_base.update_finding_status(
            host, claim, status, superseded_by
        )
        if finding is None:
            return json.dumps(
                {"error": f"no finding on {host} matches claim substr or bad status"},
                ensure_ascii=False,
            )
        if status == "retracted":
            killed = self.frontier.invalidate(
                f"{host}::{claim}", "dependency retracted"
            )
            if claim != finding.claim:
                killed += self.frontier.invalidate(
                    f"{host}::{finding.claim}", "dependency retracted"
                )
            self.publish_action(
                f"⚠ Finding 已推翻（retracted）：{claim[:60]}；"
                f"级联 kill {killed} 个依赖 Intent。"
            )
        self.blackboard.add(
            "findings", f"{host}: {finding.claim[:120]} [{status}]", author="master"
        )
        return json.dumps(
            {"ok": True, "host": host, "claim": finding.claim[:120], "status": status},
            ensure_ascii=False,
        )

    def _tool_list_findings(self, args: dict) -> str:
        host_filter = args.get("host")
        rows = []
        for f_host, f_obj in self.knowledge_base.all_findings():
            if host_filter and f_host != host_filter:
                continue
            rows.append(
                {
                    "host": f_host,
                    "claim": f_obj.claim,
                    "status": f_obj.status,
                    "severity": f_obj.severity,
                    "cve": f_obj.cve,
                    "confidence": f_obj.confidence,
                    "verified": f_obj.verified,
                }
            )
        return json.dumps(
            {"findings": rows[:50], "count": len(rows)}, ensure_ascii=False
        )

    def _tool_blackboard_write(self, args: dict) -> str:
        ok = self.blackboard.add(
            args.get("section", ""),
            args.get("text", ""),
            author=args.get("author", "") or "master",
        )
        if not ok:
            return json.dumps(
                {"ok": False, "error": "bad section, empty text, or duplicate",
                 "valid_sections": list(SECTIONS.keys())},
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, "section": args.get("section")}, ensure_ascii=False)

    def _tool_blackboard_read(self, args: dict) -> str:
        section = args.get("section")
        if section:
            return json.dumps(
                {"section": section, "entries": self.blackboard.entries(section)},
                ensure_ascii=False,
            )
        return json.dumps(self.blackboard.to_dict(), ensure_ascii=False)

    def _tool_intent_add(self, args: dict) -> str:
        iid = self.frontier.add_intent(
            hypothesis=str(args.get("hypothesis", "")),
            action=str(args.get("action", "")),
            priority=int(args.get("priority", 3) or 3),
            max_steps=int(args.get("max_steps", 8) or 8),
            expiry_s=float(args.get("expiry_s", 900.0) or 900.0),
            depends_on=tuple(args.get("depends_on") or ()),
            evidence=tuple(args.get("evidence") or ()),
        )
        if iid is None:
            return json.dumps(
                {"ok": False, "error": "empty hypothesis/action, or duplicate open intent"},
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, "intent_id": iid}, ensure_ascii=False)

    def _tool_intent_list(self, args: dict) -> str:
        intents = [
            {
                "id": i.id,
                "hypothesis": i.hypothesis,
                "action": i.action,
                "status": i.status.value,
                "priority": i.priority,
                "steps_used": i.budget.steps_used,
                "result": i.result,
            }
            for i in sorted(
                self.frontier._intents.values(),
                key=lambda i: (i.priority, i.budget.created_ts),
            )
        ]
        return json.dumps({"intents": intents}, ensure_ascii=False)

    def _focused_context(self, intent_id: str, scope: str | None = None) -> str:
        refs = self.frontier.supporting_refs(intent_id, scope=scope)
        lines: list[str] = []
        for ref in refs:
            if "::" in ref:
                host, claim = ref.split("::", 1)
                resolved = self._kb_claim_text(host, claim)
                lines.append(f"- [Fact] {host}: {resolved or claim}")
            else:
                lines.append(f"- {ref}")
        if not lines:
            return ""
        return "【聚焦上下文 — 支撑本意图的事实链】\n" + "\n".join(lines[:12])

    def _kb_claim_text(self, host: str, claim: str) -> str:
        for f_host, finding in self.knowledge_base.all_findings():
            if f_host == host and claim.lower() in finding.claim.lower():
                return f"[{finding.status}] {finding.claim}"
        return ""

    def _set_current_intent(self, intent_id: str | None) -> None:
        self._current_intent_id = intent_id
        self._recent_tool_keys.clear()
        self._stuck_ticks = 0
        self._stuck_fact_baseline = self.knowledge_base.finding_total()

    def _flag_stuck_observer(self) -> None:
        events = self.frontier.history()[-20:]
        replay = "\n".join(
            f"- {e['type']}: {json.dumps(e['payload'], ensure_ascii=False)[:160]}"
            for e in events
        )
        self._pending_observer_msg = (
            "【Observer 卡死审视】当前意图连续多步无新 Fact，最近因果链：\n"
            f"{replay or '(空)'}\n"
            "请判断：是否已陷入死胡同？是 → intent_kill 并写明原因；"
            "否 → 说明换什么动作继续。"
        )
        self.publish_action("⚠ Stuck Detector 触发：多步无新 Fact，注入因果重放审视…")

    def _tool_intent_claim(self, args: dict) -> str:
        iid = str(args.get("intent_id", ""))
        ok = self.frontier.claim(iid)
        if ok:
            self._set_current_intent(iid)
        ctx = self._focused_context(iid) if ok else ""
        return json.dumps({"ok": ok, "context": ctx[:800]}, ensure_ascii=False)

    def _tool_intent_done(self, args: dict) -> str:
        iid = str(args.get("intent_id", ""))
        ok = self.frontier.complete(iid, str(args.get("conclusion", "")))
        if ok and self._current_intent_id == iid:
            self._set_current_intent(None)
        return json.dumps({"ok": ok}, ensure_ascii=False)

    def _tool_intent_kill(self, args: dict) -> str:
        iid = str(args.get("intent_id", ""))
        ok = self.frontier.kill(iid, str(args.get("reason", "")))
        if ok and self._current_intent_id == iid:
            self._set_current_intent(None)
        return json.dumps({"ok": ok}, ensure_ascii=False)

    async def _tool_dispatch_sub_agent(self, args: dict) -> str:
        agent_type = args.get("agent_type", "recon")
        target = args.get("target", "unknown")
        task = args.get("task", "")
        priority_map = {
            "exploit": TaskPriority.EXPLOIT,
            "recon": TaskPriority.RECON,
            "lateral": TaskPriority.LATERAL,
            "persist": TaskPriority.PERSIST,
            "report": TaskPriority.REPORT,
        }
        priority = priority_map.get(agent_type, TaskPriority.RECON)
        result = await self._dispatch_sub_agent(agent_type, target, task, priority)
        return json.dumps(
            {
                "agent_id": getattr(result, "agent_id", ""),
                "status": getattr(getattr(result, "status", None), "value", str(getattr(result, "status", ""))),
                "scripts_executed": getattr(result, "scripts_executed", 0),
                "error": getattr(result, "error", ""),
                "text": (getattr(result, "text", "") or "")[:4000],
                "new_findings": [
                    {"host": h, "claim": c[:120]}
                    for h, c in (getattr(result, "findings", None) or [])[:20]
                ],
                "new_targets": list(getattr(result, "new_targets", None) or [])[:10],
            },
            ensure_ascii=False,
        )


    @staticmethod
    def _make_unified_diff(old: str, new: str, label: str) -> str:
        diff_iter = difflib.unified_diff(
            old.splitlines(keepends=False),
            new.splitlines(keepends=False),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            lineterm="",
        )
        return "\n".join(diff_iter)

    def _tool_read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        if not path:
            return json.dumps({"error": "path is required"}, ensure_ascii=False)
        try:
            p = Path(path).expanduser()
            if not p.exists():
                return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
            if not p.is_file():
                return json.dumps({"error": f"Not a regular file: {path}"}, ensure_ascii=False)
            try:
                content = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return json.dumps(
                    {
                        "error": "binary file (use a hex/decode tool)",
                        "path": str(p.resolve()),
                        "size_bytes": p.stat().st_size,
                    },
                    ensure_ascii=False,
                )

            lines = content.splitlines()
            total = len(lines)
            if offset < 0:
                offset = 0
            if offset >= total and total > 0:
                return json.dumps(
                    {"error": f"offset {offset} >= total lines {total}"}, ensure_ascii=False
                )
            selected = lines[offset : offset + limit] if limit > 0 else lines[offset:]
            numbered = "\n".join(
                f"{offset + i + 1:6}\t{line}" for i, line in enumerate(selected)
            )
            return json.dumps(
                {
                    "path": str(p.resolve()),
                    "start_line": offset + 1,
                    "end_line": offset + len(selected),
                    "total_lines": total,
                    "content": numbered,
                    "truncated": offset + len(selected) < total,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_write_file(self, path: str, content: str) -> str:
        if not path:
            return json.dumps({"error": "path is required"}, ensure_ascii=False)
        try:
            p = Path(path).expanduser()
            old_content = ""
            existed = p.exists()
            if existed:
                try:
                    old_content = p.read_text(encoding="utf-8")
                except Exception:
                    old_content = ""
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            diff_text = self._make_unified_diff(old_content, content, str(p))
            return json.dumps(
                {
                    "ok": True,
                    "path": str(p.resolve()),
                    "existed": existed,
                    "lines_written": len(content.splitlines()),
                    "bytes_written": len(content.encode("utf-8")),
                    "diff": diff_text,
                    "summary": (
                        f"{'Overwrote' if existed else 'Created'} {path} "
                        f"({len(content.splitlines())} lines)"
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_edit_file(self, path: str, old_string: str, new_string: str) -> str:
        if not path or not old_string:
            return json.dumps({"error": "path and old_string required"}, ensure_ascii=False)
        try:
            p = Path(path).expanduser()
            if not p.exists():
                return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
            old_content = p.read_text(encoding="utf-8")
            count = old_content.count(old_string)
            if count == 0:
                return json.dumps(
                    {"error": "old_string not found in file"}, ensure_ascii=False
                )
            if count > 1:
                return json.dumps(
                    {
                        "error": (
                            f"old_string matches {count} times — add surrounding "
                            "context to make it unique, or use multi_edit_file with "
                            "replace_all"
                        )
                    },
                    ensure_ascii=False,
                )
            new_content = old_content.replace(old_string, new_string, 1)
            p.write_text(new_content, encoding="utf-8")
            diff_text = self._make_unified_diff(old_content, new_content, str(p))
            return json.dumps(
                {
                    "ok": True,
                    "path": str(p.resolve()),
                    "bytes_delta": len(new_content) - len(old_content),
                    "diff": diff_text,
                    "summary": f"Edited {path}",
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_multi_edit_file(self, path: str, edits: list) -> str:
        if not path:
            return json.dumps({"error": "path is required"}, ensure_ascii=False)
        if not isinstance(edits, list) or not edits:
            return json.dumps({"error": "edits must be a non-empty array"}, ensure_ascii=False)
        try:
            p = Path(path).expanduser()
            if not p.exists():
                return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
            old_content = p.read_text(encoding="utf-8")
            current = old_content
            applied = 0
            for i, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    return json.dumps(
                        {"error": f"edit #{i} is not an object"}, ensure_ascii=False
                    )
                old_s = edit.get("old_string", "")
                new_s = edit.get("new_string", "")
                replace_all = bool(edit.get("replace_all", False))
                if not old_s:
                    return json.dumps(
                        {"error": f"edit #{i}: empty old_string"}, ensure_ascii=False
                    )
                if replace_all:
                    if old_s not in current:
                        return json.dumps(
                            {"error": f"edit #{i}: old_string not found"}, ensure_ascii=False
                        )
                    current = current.replace(old_s, new_s)
                else:
                    count = current.count(old_s)
                    if count == 0:
                        return json.dumps(
                            {"error": f"edit #{i}: old_string not found"}, ensure_ascii=False
                        )
                    if count > 1:
                        return json.dumps(
                            {
                                "error": (
                                    f"edit #{i}: old_string matches {count}x — add "
                                    "context or set replace_all=true"
                                )
                            },
                            ensure_ascii=False,
                        )
                    current = current.replace(old_s, new_s, 1)
                applied += 1
            p.write_text(current, encoding="utf-8")
            diff_text = self._make_unified_diff(old_content, current, str(p))
            return json.dumps(
                {
                    "ok": True,
                    "path": str(p.resolve()),
                    "edits_applied": applied,
                    "diff": diff_text,
                    "summary": f"Applied {applied} edits to {path}",
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    _GREP_IGNORE_DIRS = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".idea", ".vscode",
    }

    def _tool_grep(
        self,
        pattern: str,
        path: str = ".",
        glob: str = "**/*",
        max_results: int = 100,
        ignore_case: bool = False,
    ) -> str:
        if not pattern:
            return json.dumps({"error": "pattern is required"}, ensure_ascii=False)
        try:
            flags = re.IGNORECASE if ignore_case else 0
            regex = re.compile(pattern, flags)
        except re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"}, ensure_ascii=False)
        try:
            root = Path(path).expanduser().resolve()
            if not root.exists():
                return json.dumps({"error": f"Path not found: {path}"}, ensure_ascii=False)
            if root.is_file():
                candidates = [root]
            else:
                try:
                    candidates = list(root.glob(glob))
                except Exception as e:
                    return json.dumps({"error": f"Glob error: {e}"}, ensure_ascii=False)

            matches = []
            files_searched = 0
            for f in candidates:
                if not f.is_file():
                    continue
                parts = f.parts
                if any(p in self._GREP_IGNORE_DIRS for p in parts):
                    continue
                if f.suffix.lower() in {
                    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip",
                    ".gz", ".tar", ".so", ".pyc", ".db", ".sqlite",
                    ".bin", ".exe", ".dll", ".o", ".a",
                }:
                    continue
                files_searched += 1
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append({
                            "file": str(f.relative_to(root) if root.is_dir() else f.name),
                            "line": i,
                            "text": line[:300],
                        })
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break

            return json.dumps(
                {
                    "pattern": pattern,
                    "ignore_case": ignore_case,
                    "files_searched": files_searched,
                    "match_count": len(matches),
                    "matches": matches,
                    "truncated": len(matches) >= max_results,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


    def _tool_todo_write(self, todos: list) -> str:
        if not isinstance(todos, list):
            return json.dumps({"error": "todos must be an array"}, ensure_ascii=False)
        cleaned: list[dict] = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            status = item.get("status", "pending")
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            cleaned.append({
                "id": str(item.get("id") or f"t{i}"),
                "content": content,
                "status": status,
            })
        self.todos = cleaned
        self.event_bus.publish(
            Event(
                type=EventType.STATUS_UPDATE,
                data={"tasks": [
                    {"name": t["content"], "status": t["status"], "id": t["id"]}
                    for t in cleaned
                ]},
            )
        )
        return json.dumps(
            {
                "ok": True,
                "todo_count": len(cleaned),
                "completed": sum(1 for t in cleaned if t["status"] == "completed"),
                "in_progress": sum(1 for t in cleaned if t["status"] == "in_progress"),
                "pending": sum(1 for t in cleaned if t["status"] == "pending"),
            },
            ensure_ascii=False,
        )

    def _tool_web_search(self, query: str, max_results: int = 10) -> str:
        # Preferred backend: ddgs (handles DuckDuckGo bot detection / vqd flow).
        # Fallback: Instant Answer API (no key, abstracts only) so search never breaks.
        if not query:
            return json.dumps({"error": "query is required"}, ensure_ascii=False)

        try:
            from ddgs import DDGS

            try:
                with DDGS() as ddgs:
                    raw_hits = list(ddgs.text(query, max_results=max_results))
                results = [
                    {
                        "title": h.get("title", "")[:200],
                        "url": h.get("href") or h.get("url", ""),
                        "snippet": (h.get("body") or h.get("snippet") or "")[:300],
                    }
                    for h in raw_hits
                    if h.get("title")
                ]
                return json.dumps(
                    {
                        "query": query,
                        "result_count": len(results),
                        "results": results,
                        "source": "ddgs",
                    },
                    ensure_ascii=False,
                )
            except Exception as exc:
                primary_error: Optional[str] = f"ddgs failed: {exc}"
        except ImportError:
            primary_error = (
                "ddgs not installed (run `pip install ddgs` for full web "
                "search); falling back to DuckDuckGo Instant Answer API."
            )

        import ssl
        import urllib.error
        import urllib.parse
        import urllib.request

        api_url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
            {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "0",
                "t": "DRX-Operator",
            }
        )
        try:
            req = urllib.request.Request(
                api_url, headers={"User-Agent": "DRX-Operator/0.5"}
            )
            try:
                resp = urllib.request.urlopen(req, timeout=15)
            except urllib.error.URLError as e:
                if "CERTIFICATE_VERIFY_FAILED" in str(e):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    resp = urllib.request.urlopen(req, timeout=15, context=ctx)
                else:
                    raise
            try:
                body = resp.read().decode("utf-8", errors="replace")
            finally:
                resp.close()
            payload = json.loads(body)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"web search failed: {e}",
                    "query": query,
                    "hint": "install `ddgs` for full search (`pip install ddgs`)",
                },
                ensure_ascii=False,
            )

        results: list[dict] = []
        abstract = (payload.get("AbstractText") or "").strip()
        if abstract:
            results.append({
                "title": payload.get("Heading", query),
                "url": payload.get("AbstractURL", ""),
                "snippet": abstract[:300],
            })
        for topic in payload.get("RelatedTopics", [])[: max_results - len(results)]:
            if not isinstance(topic, dict):
                continue
            if "Topics" in topic:
                for sub in topic["Topics"]:
                    if not isinstance(sub, dict):
                        continue
                    text = (sub.get("Text") or "").strip()
                    if text:
                        results.append({
                            "title": text.split(" - ", 1)[0][:200],
                            "url": sub.get("FirstURL", ""),
                            "snippet": text[:300],
                        })
                    if len(results) >= max_results:
                        break
            else:
                text = (topic.get("Text") or "").strip()
                if text:
                    results.append({
                        "title": text.split(" - ", 1)[0][:200],
                        "url": topic.get("FirstURL", ""),
                        "snippet": text[:300],
                    })
            if len(results) >= max_results:
                break

        return json.dumps(
            {
                "query": query,
                "result_count": len(results),
                "results": results,
                "source": "duckduckgo-instant-answer",
                "note": primary_error,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _tool_cve_lookup(cve_id: str) -> str:
        
        import ssl
        import urllib.error
        import urllib.parse
        import urllib.request

        if not cve_id:
            return json.dumps({"error": "cve_id is required"}, ensure_ascii=False)

        cve_id = cve_id.strip().upper()
        if not re.fullmatch(r"CVE-\d{4}-\d{4,}", cve_id):
            return json.dumps(
                {"error": f"invalid CVE id format: {cve_id!r}"},
                ensure_ascii=False,
            )

        url = (
            "https://services.nvd.nist.gov/rest/json/cves/2.0?"
            + urllib.parse.urlencode({"cveId": cve_id})
        )
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "DRX-Operator/0.5 (CVE lookup)"}
            )
            try:
                resp = urllib.request.urlopen(req, timeout=20)
            except urllib.error.URLError as e:
                if "CERTIFICATE_VERIFY_FAILED" in str(e):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    resp = urllib.request.urlopen(req, timeout=20, context=ctx)
                else:
                    raise
            try:
                body = resp.read().decode("utf-8", errors="replace")
            finally:
                resp.close()
            payload = json.loads(body)
        except urllib.error.HTTPError as e:
            return json.dumps(
                {"error": f"NVD HTTP {e.code}: {e.reason}", "cve_id": cve_id},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {"error": f"NVD request failed: {e}", "cve_id": cve_id},
                ensure_ascii=False,
            )

        vulns = payload.get("vulnerabilities") or []
        if not vulns:
            return json.dumps(
                {"error": "CVE not found in NVD", "cve_id": cve_id},
                ensure_ascii=False,
            )
        cve = vulns[0].get("cve") or {}

        description = ""
        for d in cve.get("descriptions", []):
            if d.get("lang") == "en":
                description = d.get("value", "")
                break

        metrics = cve.get("metrics") or {}
        cvss = None
        for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            entries = metrics.get(key) or []
            if entries:
                data = entries[0].get("cvssData") or {}
                cvss = {
                    "version": data.get("version"),
                    "vector": data.get("vectorString"),
                    "baseScore": data.get("baseScore"),
                    "baseSeverity": (
                        data.get("baseSeverity")
                        or entries[0].get("baseSeverity")
                    ),
                    "exploitabilityScore": entries[0].get("exploitabilityScore"),
                    "impactScore": entries[0].get("impactScore"),
                }
                break

        cwes: list[str] = []
        for w in cve.get("weaknesses", []):
            for d in w.get("description", []):
                v = d.get("value")
                if v and v not in cwes:
                    cwes.append(v)

        refs = [
            {
                "url": r.get("url"),
                "source": r.get("source"),
                "tags": r.get("tags", []),
            }
            for r in (cve.get("references") or [])[:10]
        ]

        affected: list[str] = []
        for conf in cve.get("configurations", []):
            for node in conf.get("nodes", []):
                for cpe in node.get("cpeMatch", []):
                    name = cpe.get("criteria")
                    if name and name not in affected:
                        affected.append(name)
                        if len(affected) >= 20:
                            break
                if len(affected) >= 20:
                    break
            if len(affected) >= 20:
                break

        return json.dumps(
            {
                "cve_id": cve.get("id", cve_id),
                "published": cve.get("published"),
                "lastModified": cve.get("lastModified"),
                "description": description,
                "cvss": cvss,
                "cwes": cwes,
                "references": refs,
                "affected_cpe": affected,
                "source": "NVD",
            },
            ensure_ascii=False,
        )


    def _tool_shell_open(self, command: str, name: str = "") -> str:
        if not command:
            return json.dumps({"error": "command is required"}, ensure_ascii=False)
        try:
            sess = self.shells.open(command, name=name)
            initial = sess.peek(timeout=0.5)
            return json.dumps(
                {
                    "ok": True,
                    "session_id": sess.session_id,
                    "name": sess.name,
                    "command": sess.command,
                    "initial_output": initial[:4000],
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_shell_exec(
        self,
        session_id: str,
        input_text: str,
        timeout: float = 10.0,
        idle_timeout: float = 0.4,
    ) -> str:
        if not session_id:
            return json.dumps({"error": "session_id is required"}, ensure_ascii=False)
        sess = self.shells.get(session_id)
        if sess is None:
            return json.dumps(
                {"error": f"session {session_id!r} not found"}, ensure_ascii=False
            )
        try:
            output = sess.send(input_text, timeout=timeout, idle_timeout=idle_timeout)
            return json.dumps(
                {
                    "session_id": session_id,
                    "alive": sess.is_alive(),
                    "output": output[:8000],
                    "output_length": len(output),
                    "truncated": len(output) > 8000,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_shell_signal(self, session_id: str, sig_name: str = "SIGINT") -> str:
        sess = self.shells.get(session_id)
        if sess is None:
            return json.dumps(
                {"error": f"session {session_id!r} not found"}, ensure_ascii=False
            )
        try:
            sess.signal(sig_name)
            return json.dumps(
                {"ok": True, "session_id": session_id, "signal": sig_name},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_shell_close(self, session_id: str) -> str:
        if self.shells.close(session_id):
            return json.dumps({"ok": True, "session_id": session_id}, ensure_ascii=False)
        return json.dumps(
            {"error": f"session {session_id!r} not found"}, ensure_ascii=False
        )

    def _tool_shell_list(self) -> str:
        return json.dumps({"sessions": self.shells.list()}, ensure_ascii=False)


    def _tool_oob_start(self, port: Optional[int], bind: str) -> str:
        try:
            info = self.oob.start(port=port, bind=bind or "0.0.0.0")
            info["hint"] = (
                "Embed callback_url in your payloads. For external targets, "
                "tunnel with `ngrok http " + str(info.get("bind", "?").split(":")[-1]) + "` "
                "and use the public URL instead."
            )
            return json.dumps(info, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_oob_logs(
        self, since_ts: float, token_only: bool, last_n: Optional[int]
    ) -> str:
        try:
            data = self.oob.interactions(
                since_ts=since_ts, token_only=token_only, last_n=last_n
            )
            return json.dumps(
                {"count": len(data), "interactions": data},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _tool_oob_stop(self) -> str:
        try:
            self.oob.stop()
            return json.dumps({"ok": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


    _WORDLIST_ROOTS = (
        "/usr/share/wordlists",
        "/usr/share/seclists",
        "/usr/share/dict",
        "/opt/SecLists",
        "/opt/wordlists",
        "~/wordlists",
        "~/SecLists",
        "./wordlists",
    )
    _WORDLIST_CATEGORY_HINTS = {
        "passwords": ("password", "rockyou", "darkweb", "cain", "leak"),
        "web":       ("dirb", "dir", "fuzz", "web", "common", "raft", "burp"),
        "dns":       ("dns", "subdomain", "hosts", "namelist"),
        "usernames": ("user", "names"),
        "sql":       ("sql", "sqli", "xss"),
    }

    @classmethod
    def _classify_wordlist(cls, path_str: str) -> str:
        lower = path_str.lower()
        for cat, keys in cls._WORDLIST_CATEGORY_HINTS.items():
            if any(k in lower for k in keys):
                return cat
        return "misc"

    def _tool_wordlist_list(self, category: str = "") -> str:
        category = (category or "").strip().lower()
        roots = []
        for r in self._WORDLIST_ROOTS:
            p = Path(r).expanduser()
            if p.is_dir():
                roots.append(p)

        found: list[dict] = []
        seen: set[Path] = set()
        for root in roots:
            try:
                for p in root.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.resolve() in seen:
                        continue
                    if p.suffix.lower() not in {".txt", ".lst", ".gz", ""}:
                        if p.suffix:
                            continue
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    if size == 0 or size > 1 * 1024 * 1024 * 1024:
                        continue
                    cat = self._classify_wordlist(str(p))
                    if category and cat != category:
                        continue
                    seen.add(p.resolve())
                    found.append({
                        "path": str(p),
                        "size": size,
                        "size_human": self._human_size(size),
                        "category": cat,
                    })
                    if len(found) >= 500:
                        break
            except Exception:
                continue
            if len(found) >= 500:
                break

        found.sort(key=lambda d: (d["category"], -d["size"]))
        return json.dumps(
            {
                "roots_scanned": [str(r) for r in roots],
                "count": len(found),
                "wordlists": found[:200],
                "truncated": len(found) > 200,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _human_size(n: int) -> str:
        for unit in ("B", "K", "M", "G"):
            if n < 1024:
                return f"{n:.0f}{unit}"
            n /= 1024
        return f"{n:.1f}T"

    def _tool_wordlist_top(self, path: str, n: int = 100) -> str:
        if not path:
            return json.dumps({"error": "path is required"}, ensure_ascii=False)
        try:
            p = Path(path).expanduser()
            if not p.is_file():
                return json.dumps(
                    {"error": f"not a file: {path}"}, ensure_ascii=False
                )
            lines: list[str] = []
            with p.open("r", encoding="utf-8", errors="replace") as fp:
                for i, line in enumerate(fp):
                    if i >= n:
                        break
                    lines.append(line.rstrip("\n"))
            return json.dumps(
                {
                    "path": str(p.resolve()),
                    "lines_returned": len(lines),
                    "lines": lines,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


    def _tool_parse_nmap(self, output: str, update_kb: bool = False) -> str:
        if not output:
            return json.dumps({"error": "output is required"}, ensure_ascii=False)
        try:
            text = output.lstrip()
            if text.startswith("<?xml") or "<nmaprun" in text[:200]:
                parsed = self._parse_nmap_xml(output)
            else:
                parsed = self._parse_nmap_text(output)
        except Exception as e:
            return json.dumps({"error": f"parse failed: {e}"}, ensure_ascii=False)

        if update_kb and not parsed.get("error"):
            for host in parsed.get("hosts", []):
                self.knowledge_base.update_target(
                    host["host"],
                    open_ports=[p["port"] for p in host["ports"]],
                    services={
                        str(p["port"]): " ".join(
                            x for x in [p.get("service"), p.get("product"), p.get("version")] if x
                        ).strip()
                        for p in host["ports"]
                    },
                )
            parsed["kb_updated"] = True

        return json.dumps(parsed, ensure_ascii=False)

    @staticmethod
    def _parse_nmap_xml(xml_str: str) -> dict:
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as e:
            return {"error": f"XML parse error: {e}"}

        hosts = []
        for host_el in root.findall("host"):
            status_el = host_el.find("status")
            if status_el is not None and status_el.get("state") not in (None, "up"):
                continue
            addr = None
            for a in host_el.findall("address"):
                atype = a.get("addrtype")
                if atype in ("ipv4", "ipv6"):
                    addr = a.get("addr")
                    break
            if not addr:
                continue

            hostnames = [
                h.get("name")
                for h in host_el.findall(".//hostname")
                if h.get("name")
            ]

            ports = []
            for port_el in host_el.findall(".//port"):
                st = port_el.find("state")
                if st is None or st.get("state") != "open":
                    continue
                svc_el = port_el.find("service")
                info = {
                    "port": int(port_el.get("portid", 0)),
                    "protocol": port_el.get("protocol", "tcp"),
                    "service": svc_el.get("name") if svc_el is not None else None,
                    "product": svc_el.get("product") if svc_el is not None else None,
                    "version": svc_el.get("version") if svc_el is not None else None,
                    "extrainfo": (
                        svc_el.get("extrainfo") if svc_el is not None else None
                    ),
                }
                scripts = [
                    {"id": s.get("id"), "output": (s.get("output") or "")[:500]}
                    for s in port_el.findall("script")
                    if s.get("id")
                ]
                if scripts:
                    info["scripts"] = scripts
                ports.append(info)

            os_info = None
            os_match = host_el.find("os/osmatch")
            if os_match is not None:
                try:
                    os_info = {
                        "name": os_match.get("name"),
                        "accuracy": int(os_match.get("accuracy") or 0),
                    }
                except Exception:
                    pass

            hosts.append({
                "host": addr,
                "hostnames": hostnames,
                "ports": ports,
                "os": os_info,
            })

        return {"format": "xml", "hosts": hosts, "host_count": len(hosts)}

    @staticmethod
    def _parse_nmap_text(text: str) -> dict:
        host_re = re.compile(
            r"Nmap scan report for (?:(\S+) \()?([0-9.]+|[0-9a-f:]+)\)?"
        )
        port_re = re.compile(
            r"^(\d+)/(tcp|udp)\s+(open|filtered|closed|open\|filtered)\s+(\S+)(?:\s+(.+))?$"
        )

        hosts: list[dict] = []
        current: dict | None = None
        for line in text.splitlines():
            stripped = line.strip()
            m = host_re.search(stripped)
            if m:
                if current is not None:
                    hosts.append(current)
                current = {
                    "host": m.group(2),
                    "hostnames": [m.group(1)] if m.group(1) else [],
                    "ports": [],
                }
                continue
            pm = port_re.match(stripped)
            if pm and current is not None:
                state = pm.group(3)
                if state != "open":
                    continue
                version_str = (pm.group(5) or "").strip()
                product = None
                version = None
                if version_str:
                    parts = version_str.split(" ", 1)
                    product = parts[0]
                    if len(parts) > 1:
                        version = parts[1]
                current["ports"].append({
                    "port": int(pm.group(1)),
                    "protocol": pm.group(2),
                    "service": pm.group(4),
                    "product": product,
                    "version": version,
                })
        if current is not None:
            hosts.append(current)
        return {"format": "text", "hosts": hosts, "host_count": len(hosts)}

    def _tool_read_artifact(self, artifact_id: str, offset: int = 0, limit: int = 6000) -> str:
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"}, ensure_ascii=False)
        artifact_id = artifact_id.replace("artifact://", "").strip()
        meta = self.artifacts.meta(artifact_id)
        if meta is None:
            return json.dumps(
                {"error": f"artifact {artifact_id!r} not found"}, ensure_ascii=False
            )
        text = self.artifacts.read(artifact_id, offset=offset, limit=limit)
        if text is None:
            return json.dumps(
                {"error": f"artifact {artifact_id!r} unreadable"}, ensure_ascii=False
            )
        total = meta.get("size", 0)
        return json.dumps(
            {
                "artifact_id": artifact_id,
                "tool": meta.get("tool"),
                "total_size": total,
                "offset": offset,
                "returned": len(text),
                "truncated": offset + len(text) < total,
                "content": text,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _tool_parse_http(raw: str) -> str:
        if not raw:
            return json.dumps({"error": "raw is required"}, ensure_ascii=False)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        sep_candidates = ["\r\n\r\n", "\n\n"]
        sep = next((s for s in sep_candidates if s in raw), None)
        if sep is not None:
            header_part, body = raw.split(sep, 1)
        else:
            header_part, body = raw, ""

        lines = re.split(r"\r?\n", header_part)
        if not lines:
            return json.dumps({"error": "empty input"}, ensure_ascii=False)
        first = lines[0].strip()
        headers: dict[str, str] = {}
        for ln in lines[1:]:
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            headers[k.strip()] = v.strip()

        result: dict[str, Any]
        if first.startswith("HTTP/"):
            parts = first.split(" ", 2)
            status_code = 0
            try:
                if len(parts) > 1 and parts[1].isdigit():
                    status_code = int(parts[1])
            except Exception:
                pass
            result = {
                "kind": "response",
                "protocol": parts[0] if parts else "",
                "status": status_code,
                "reason": parts[2] if len(parts) > 2 else "",
            }
        else:
            parts = first.split(" ", 2)
            result = {
                "kind": "request",
                "method": parts[0] if parts else "",
                "path": parts[1] if len(parts) > 1 else "",
                "protocol": parts[2] if len(parts) > 2 else "",
            }

        result["headers"] = headers
        result["body"] = body[:8000]
        result["body_length"] = len(body)
        return json.dumps(result, ensure_ascii=False)


    def _tool_generate_report(
        self,
        path: Optional[str] = None,
        fmt: str = "markdown",
        title: str = "",
        include_session_usage: bool = True,
    ) -> str:
        try:
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            if not title:
                title = f"DRX-Operator Pentest Report {stamp}"

            targets = self.knowledge_base.list_targets() or []
            owned = self.knowledge_base.owned_targets() or []
            todos = list(self.todos or [])

            findings_by_host: dict[str, list] = {}
            for t in targets:
                host = t.get("host")
                if not host:
                    continue
                fs = self.knowledge_base.get_findings(host) or []
                if fs:
                    findings_by_host[host] = fs

            lines: list[str] = []
            lines.append(f"# {title}")
            lines.append("")
            lines.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}")
            lines.append(f"**Mode:** {self.mode}")
            lines.append("")

            lines.append("## Executive Summary")
            lines.append("")
            lines.append(f"- Targets touched: **{len(targets)}**")
            lines.append(f"- Targets owned: **{len(owned)}**")
            total_findings = sum(len(v) for v in findings_by_host.values())
            lines.append(f"- Findings recorded: **{total_findings}**")
            lines.append(
                f"- LLM requests: **{self.session_usage.get('requests', 0)}**, "
                f"cost: **${self.session_usage.get('cost_usd', 0):.4f}**"
            )
            lines.append("")

            if targets:
                lines.append("## Targets")
                lines.append("")
                lines.append("| Host | Open Ports | Services | Owned |")
                lines.append("|------|-----------|----------|-------|")
                for t in targets:
                    host = t.get("host", "?")
                    ports = ", ".join(str(p) for p in (t.get("open_ports") or []))
                    services = ", ".join(
                        f"{k}={v}" for k, v in (t.get("services") or {}).items()
                    )
                    own = "✓" if t.get("owned") else ""
                    lines.append(f"| `{host}` | {ports or '—'} | {services or '—'} | {own} |")
                lines.append("")

            if findings_by_host:
                lines.append("## Findings")
                lines.append("")
                for host, fs in findings_by_host.items():
                    lines.append(f"### {host}")
                    lines.append("")
                    for i, f in enumerate(fs, 1):
                        claim = getattr(f, "claim", str(f))
                        conf = getattr(f, "confidence", None)
                        cve = getattr(f, "cve", "") or ""
                        sev = getattr(f, "severity", "") or "info"
                        verified = getattr(f, "verified", False)
                        evidence = getattr(f, "evidence", []) or []
                        lines.append(
                            f"**{i}. {claim}**  "
                            f"_severity:_ `{sev}`  "
                            + (f"_CVE:_ `{cve}`  " if cve else "")
                            + (f"_confidence:_ `{conf}`  " if conf is not None else "")
                            + ("`verified`" if verified else "")
                        )
                        if evidence:
                            lines.append("")
                            for ev in evidence:
                                etype = getattr(ev, "type", "?")
                                eval_ = getattr(ev, "value", "")
                                src = getattr(ev, "source", "")
                                lines.append(
                                    f"  - **{etype}**: `{str(eval_)[:200]}` "
                                    + (f"_via_ `{src}`" if src else "")
                                )
                        lines.append("")
                lines.append("")

            if todos:
                lines.append("## Task Checklist")
                lines.append("")
                glyph = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
                for t in todos:
                    lines.append(
                        f"- {glyph.get(t.get('status', 'pending'), '[ ]')} "
                        f"{t.get('content', '')}"
                    )
                lines.append("")

            if include_session_usage:
                lines.append("## Session Usage")
                lines.append("")
                su = self.session_usage
                lines.append(f"- LLM requests: `{su.get('requests', 0)}`")
                lines.append(f"- Prompt tokens: `{su.get('prompt_tokens', 0)}`")
                lines.append(f"- Completion tokens: `{su.get('completion_tokens', 0)}`")
                lines.append(f"- Total tokens: `{su.get('total_tokens', 0)}`")
                if su.get("cache_hit_tokens"):
                    lines.append(f"- Cache-hit tokens: `{su['cache_hit_tokens']}`")
                lines.append(f"- Estimated cost: `${su.get('cost_usd', 0):.4f}`")
                if su.get("by_model"):
                    lines.append("")
                    lines.append("### Per-model")
                    for model, m in (su.get("by_model") or {}).items():
                        lines.append(
                            f"- `{model}` — {m.get('requests', 0)} req, "
                            f"{m.get('total', 0)} tokens, "
                            f"${m.get('cost', 0):.4f}"
                        )
                lines.append("")

            lines.append("---")
            lines.append("_Generated by DRX-Operator._")
            md_body = "\n".join(lines)

            if fmt == "html":
                content = self._render_markdown_to_html(md_body, title)
                default_ext = ".html"
            else:
                content = md_body
                default_ext = ".md"

            if not path:
                reports_dir = Path("reports")
                reports_dir.mkdir(parents=True, exist_ok=True)
                path = str(reports_dir / f"report-{stamp}{default_ext}")
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

            return json.dumps(
                {
                    "ok": True,
                    "path": str(p.resolve()),
                    "format": fmt,
                    "bytes": len(content.encode("utf-8")),
                    "preview": md_body[:1500],
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @staticmethod
    def _render_markdown_to_html(md: str, title: str) -> str:
        
        import html as _html
        out = ["<!DOCTYPE html><html><head><meta charset='utf-8'>",
               f"<title>{_html.escape(title)}</title>",
               "<style>",
               "body{font-family:-apple-system,sans-serif;max-width:880px;margin:2em auto;padding:0 1em;color:#24292f;}",
               "h1,h2,h3{color:#1f2328;} code{background:#f6f8fa;padding:2px 6px;border-radius:4px;}",
               "table{border-collapse:collapse;margin:1em 0;} th,td{border:1px solid #d0d7de;padding:6px 12px;}",
               "th{background:#f6f8fa;text-align:left;}",
               "</style></head><body>"]
        in_table = False
        for line in md.splitlines():
            if line.startswith("# "):
                out.append(f"<h1>{_html.escape(line[2:])}</h1>")
            elif line.startswith("## "):
                out.append(f"<h2>{_html.escape(line[3:])}</h2>")
            elif line.startswith("### "):
                out.append(f"<h3>{_html.escape(line[4:])}</h3>")
            elif line.startswith("|"):
                if not in_table:
                    out.append("<table>")
                    in_table = True
                if "---" in line:
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
                cell_tag = "th" if line.count("**") >= 2 else "td"
                row = "".join(f"<{cell_tag}>{_html.escape(c)}</{cell_tag}>" for c in cells)
                out.append(f"<tr>{row}</tr>")
            else:
                if in_table:
                    out.append("</table>")
                    in_table = False
                if not line.strip():
                    out.append("<br>")
                else:
                    out.append(f"<p>{_html.escape(line)}</p>")
        if in_table:
            out.append("</table>")
        out.append("</body></html>")
        return "\n".join(out)


    async def _tool_task(self, description: str, agent_type: str = "general") -> str:
        if not description:
            return json.dumps({"error": "description is required"}, ensure_ascii=False)

        sub_system = (
            f"你是一个专注的子任务 Agent（type={agent_type or 'general'}）。"
            "主 Agent 委派你完成一个**独立**的子任务。\n"
            "你看不到主对话历史，所有需要的信息都在用户消息里。\n"
            "你拥有与主 Agent 相同的工具集（除了 task —— 禁止递归）。\n"
            "完成任务后返回简洁、结构化的最终答复（包括关键证据），"
            "主 Agent 会以你的答复为准。失败请如实汇报。"
        )
        tools = [
            t for t in self._build_tool_schemas()
            if t["function"]["name"] != "task"
        ]

        _intent_holder: dict[str, str] = {}

        async def _sub_executor(name: str, tool_args: dict) -> str:
            res = await self._execute_tool(name, tool_args)
            iid = _intent_holder.get("id")
            if iid:
                self.frontier.tick(iid, 1)
            return res

        sub = SubAgent(
            agent_type=agent_type or "general",
            target="(sub-agent)",
            task=description,
            event_bus=self.event_bus,
            llm_provider=self.llm_provider,
            tool_executor=_sub_executor,
            tool_schemas=tools,
            system_prompt=sub_system,
            ttl=300,
            max_iterations=12,
            parallel_tool_calls=True,
            usage_callback=self._record_usage,
        )
        intent_id = self.frontier.add_intent(
            hypothesis=description[:200],
            action=f"task:{agent_type or 'general'}",
            priority=TaskPriority.RECON.value,
            max_steps=sub.max_iterations,
            expiry_s=float(sub.ttl),
        )
        if intent_id:
            self.frontier.claim(intent_id)
            _intent_holder["id"] = intent_id
            ctx = self._focused_context(intent_id, scope=None)
            if ctx:
                sub.system_prompt = sub.system_prompt + "\n\n" + ctx

        self.active_sub_agents[sub.agent_id] = sub
        task = asyncio.create_task(sub.run())
        self.active_sub_agent_tasks[sub.agent_id] = task
        try:
            result = await task
        finally:
            self.active_sub_agent_tasks.pop(sub.agent_id, None)
            self.active_sub_agents.pop(sub.agent_id, None)
        if intent_id:
            if result.status is SubAgentStatus.DONE:
                self.frontier.complete(intent_id, (result.text or "")[:200])
            else:
                self.frontier.kill(intent_id, result.error or result.status.value)

        # L7 cross-agent: the sub-agent's full final answer becomes a shared
        # artifact (master keeps a pointer); findings flow via the shared KB.
        full = result.text or "(sub-agent produced no final answer)"
        artifact_id = ""
        result_field = full
        if len(full) > self.artifact_offload_threshold:
            artifact_id = self.artifacts.store(
                full, tool=f"task:{agent_type}", kind="subagent_transcript"
            )
            if artifact_id:
                result_field = (
                    full[: self.artifact_offload_threshold]
                    + f"\n…[完整结果 → artifact://{artifact_id}]"
                )
        return json.dumps(
            {
                "agent_id": result.agent_id,
                "status": result.status.value,
                "iterations": result.scripts_executed,
                "result": result_field,
                "artifact": artifact_id or None,
                "error": result.error or None,
            },
            ensure_ascii=False,
        )


    _MEMORY_FILE_NAMES = ("DRX.md", "AGENTS.md", "CLAUDE.md")

    @classmethod
    def _project_memory_path(cls) -> Optional[Path]:
        
        cwd = Path.cwd().resolve()
        for parent in [cwd, *cwd.parents]:
            for name in cls._MEMORY_FILE_NAMES:
                p = parent / name
                if p.is_file():
                    return p
            if (parent / ".git").is_dir() or parent == parent.parent:
                break
        return None

    @classmethod
    def _load_project_memory(cls) -> str:
        p = cls._project_memory_path()
        if p is None:
            return ""
        try:
            text = p.read_text(encoding="utf-8")
            if len(text) > 8000:
                text = text[:8000] + "\n…(truncated)"
            return text.strip()
        except Exception:
            return ""

    def reload_project_memory(self) -> bool:
        """Re-read the memory file from disk. Returns True if loaded."""
        self.project_memory_path = self._project_memory_path()
        self.project_memory = self._load_project_memory()
        return bool(self.project_memory)


    # Per-1M-token USD pricing; lower-cased model substring → (in, out).
    # Unknown models cost-track at 0 but still show token counts.
    _MODEL_PRICING: dict[str, tuple[float, float]] = {
        "deepseek-chat":         (0.27, 1.10),
        "deepseek-reasoner":     (0.55, 2.19),
        "gpt-4o":                (2.50, 10.00),
        "gpt-4o-mini":           (0.15, 0.60),
        "gpt-4-turbo":           (10.00, 30.00),
        "gpt-4":                 (30.00, 60.00),
        "gpt-3.5":               (0.50, 1.50),
        "claude-sonnet-4-5":     (3.00, 15.00),
        "claude-sonnet-4":       (3.00, 15.00),
        "claude-sonnet":         (3.00, 15.00),
        "claude-opus":           (15.00, 75.00),
        "claude-haiku":          (0.80, 4.00),
    }

    @classmethod
    def _price_for(cls, model: str) -> tuple[float, float]:
        if not model:
            return 0.0, 0.0
        m = model.lower()
        # Longest-match wins so "deepseek-reasoner" beats "deepseek".
        for key in sorted(cls._MODEL_PRICING.keys(), key=len, reverse=True):
            if key in m:
                return cls._MODEL_PRICING[key]
        return 0.0, 0.0

    # Per-model context window; lower-cased substring → window, unknown models fall back to _DEFAULT_WINDOW.
    _MODEL_CONTEXT_WINDOW: dict[str, int] = {
        "deepseek-v4":       1000000,
        "deepseek-v4-flash": 1000000,
        "deepseek-v4-pro":   1000000,
        "deepseek-v4-chat":  1000000,
        "deepseek-v3":       65536,
        "deepseek-chat":     65536,
        "deepseek-reasoner": 65536,
        "gpt-4o":            128000,
        "gpt-4o-mini":       128000,
        "gpt-4-turbo":       128000,
        "gpt-4":             8192,
        "gpt-3.5":           16385,
        "claude-sonnet-4-5": 200000,
        "claude-sonnet-4":   200000,
        "claude-sonnet":     200000,
        "claude-opus":       200000,
        "claude-haiku":      200000,
    }
    _DEFAULT_WINDOW: int = 32000

    def _model_window(self, model: Optional[str]) -> int:
        # Explicit per-session override (llm.context_window) wins — operators can declare new windows without waiting for this table.
        if getattr(self, "model_context_window_override", 0):
            return self.model_context_window_override
        m = (model or "").lower()
        for key in sorted(self._MODEL_CONTEXT_WINDOW.keys(), key=len, reverse=True):
            if key in m:
                return self._MODEL_CONTEXT_WINDOW[key]
        return self._DEFAULT_WINDOW

    def _current_model(self) -> str:
        
        prov = self.llm_provider
        for attr in ("providers",):
            inner = getattr(prov, attr, None)
            if inner:
                prov = inner[0]
                break
        cfg = getattr(prov, "config", None)
        return getattr(cfg, "model", "") if cfg else ""

    def _effective_input_budget(self) -> int:
        
        if self.context_token_limit and self.context_token_limit > 0:
            return self.context_token_limit
        model = self._current_model()
        window = self._model_window(model)
        max_tokens = 4096
        cfg = None
        prov = self.llm_provider
        inner = getattr(prov, "providers", None)
        if inner:
            cfg = getattr(inner[0], "config", None)
        else:
            cfg = getattr(prov, "config", None)
        if cfg is not None:
            max_tokens = int(getattr(cfg, "max_tokens", 4096) or 4096)
        try:
            tool_tokens = self._estimate_one(
                {"content": json.dumps(self._build_tool_schemas(), ensure_ascii=False)}
            )
        except Exception:
            tool_tokens = 1500
        reserve = max_tokens + tool_tokens + 2000
        budget = int((window - reserve) * self.context_window_fraction)
        return max(budget, 4000)

    def _record_usage(self, usage: Optional[dict], model: Optional[str]) -> None:
        if not usage:
            return
        # post_llm hook fires on the loop so the LLM event consumer is never blocked.
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self.hooks.dispatch(
                "post_llm", {"usage": usage, "model": model}
            ))
        except Exception:
            pass
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)

        in_rate, out_rate = self._price_for(model or "")
        cost = (prompt * in_rate + completion * out_rate) / 1_000_000.0

        su = self.session_usage
        su["prompt_tokens"] += prompt
        su["completion_tokens"] += completion
        su["total_tokens"] += total
        su["cache_hit_tokens"] += cache_hit
        su["cost_usd"] += cost
        su["requests"] += 1

        if model:
            slot = su["by_model"].setdefault(
                model, {"prompt": 0, "completion": 0, "total": 0, "cost": 0.0, "requests": 0}
            )
            slot["prompt"] += prompt
            slot["completion"] += completion
            slot["total"] += total
            slot["cost"] += cost
            slot["requests"] += 1

        now = time.time()
        self._recent_request_ts.append(now)
        self._recent_request_ts = [t for t in self._recent_request_ts if now - t < 60]
        rate = len(self._recent_request_ts)

        self.event_bus.publish(
            Event(
                type=EventType.STATUS_UPDATE,
                data={
                    "cost": f"${su['cost_usd']:.4f}",
                    "cache_hits": su["cache_hit_tokens"],
                    "rate": rate,
                    "active_targets": len(self.knowledge_base.list_targets()),
                    "tokens_in": su["prompt_tokens"],
                    "tokens_out": su["completion_tokens"],
                    "tokens_total": su["total_tokens"],
                    "requests": su["requests"],
                    "mode": self.mode,
                },
            )
        )


    def _estimate_one(self, m: dict) -> int:
        
        chars = 0
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for block in c:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    chars += len(block.get("text", ""))
                elif block.get("type") in ("image_url", "image"):
                    # Images are charged a flat ~1000-token penalty so vision turns aren't under-counted.
                    chars += 4000
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments") or ""
            if isinstance(args, str):
                chars += len(args)
        return chars // 4

    def _estimate_messages_tokens(self, messages: list[dict]) -> int:
        
        # Provider counters understand string content only; fall back to the
        # local heuristic for list/multimodal messages (avoids under-counts).
        has_complex = any(not isinstance(m.get("content"), str) for m in messages)
        if self.llm_provider is not None and not has_complex:
            try:
                return int(self.llm_provider.count_tokens(messages))
            except Exception:
                pass
        return sum(self._estimate_one(m) for m in messages)

    async def _maybe_compact_context(self) -> None:
        
        if (
            self._compaction_inflight
            or self.llm_provider is None
            or len(self.messages) <= self.context_keep_recent + 2
        ):
            return

        budget = self._effective_input_budget()
        before = self._estimate_messages_tokens(self.messages)
        if before < budget:
            return

        self._compaction_inflight = True
        try:
            low_water = int(budget * self.context_compact_to_ratio)

            # L2: micro-compaction (dedup, no LLM)
            deduped = self._micro_compact()

            # L1: retro-offload big old tool results
            offloaded = self._offload_old_tool_results()
            after_cheap = self._estimate_messages_tokens(self.messages)
            if after_cheap < low_water:
                if deduped or offloaded:
                    self.publish_action(
                        f"⚙ 上下文压缩(快速 L1/L2): {before} → {after_cheap} tokens "
                        f"(去重 {deduped} / 存档 {offloaded}，无 LLM)"
                    )
                return

            # L4: full compaction (LLM nine-segment)
            self.publish_action(
                f"⚙ 上下文接近模型窗口 (~{after_cheap}/{budget} tokens)，正在深度压缩…"
            )
            recent_budget = int(budget * self.context_recent_budget_ratio)
            pinned, to_summarize, recent = self._split_by_budget(recent_budget)
            if not to_summarize:
                return

            kb_snapshot = self._render_kb_snapshot()
            artifact_index = self._render_artifact_index()
            progress = await self._update_progress_doc(self._progress_doc, to_summarize)
            if not progress:
                return  # LLM failed — keep L1/L2 result, lose nothing
            self._progress_doc = progress
            self._compaction_count += 1

            marker = (
                "[压缩上下文 #%d — 下面是当前任务的「九段进度文档」+ 权威知识库快照 "
                "+ 可取回产物索引。结构化事实以知识库快照为准；需要被压缩掉的原始数据"
                "用 read_artifact(id) 取回。]" % self._compaction_count
            )
            block = (
                marker + "\n\n" + kb_snapshot + "\n\n"
                + "## 进度文档（九段）\n" + progress
            )
            if artifact_index:
                block += "\n\n" + artifact_index
            summary_msg = {"role": "system", "content": block}

            self.messages = pinned + [summary_msg] + recent
            after = self._estimate_messages_tokens(self.messages)
            self.publish_action(
                f"⚙ 上下文已深度压缩: {before} → {after} tokens "
                f"(摘要 {len(to_summarize)} 条 / 保留 {len(recent)} 条 / "
                f"KB+进度文档+{len(self.artifacts.list())} 个产物指针已保留)"
            )
        except Exception as exc:
            logger.exception("Compaction failed: %s", exc)
        finally:
            self._compaction_inflight = False

    # L2: micro-compaction — dedup heavy old tool results, no LLM

    def _micro_compact(self) -> int:
        
        seen: dict[tuple, list[int]] = {}
        keep_tail = self.context_keep_recent_tools
        tool_idx = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        protect = set(tool_idx[-keep_tail:]) if keep_tail else set()
        for i in tool_idx:
            m = self.messages[i]
            content = m.get("content")
            if not isinstance(content, str):
                continue
            fp = (m.get("name", ""), hash(content))
            seen.setdefault(fp, []).append(i)

        collapsed = 0
        for fp, idxs in seen.items():
            if len(idxs) < 2:
                continue
            for i in idxs[:-1]:
                if i in protect or self.messages[i].get("_collapsed"):
                    continue
                name = self.messages[i].get("name", "?")
                self.messages[i]["content"] = (
                    f"[去重：{name} 的此次输出与后续某次完全相同，已折叠]"
                )
                self.messages[i]["_collapsed"] = True
                collapsed += 1
        return collapsed

    # L1: artifact offload — big old tool results to disk

    def _offload_old_tool_results(self) -> int:
        
        tool_idx = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        if len(tool_idx) <= self.context_keep_recent_tools:
            return 0
        offload_idx = tool_idx[: -self.context_keep_recent_tools]
        cap = self.context_tool_result_cap
        n = 0
        for i in offload_idx:
            m = self.messages[i]
            if m.get("_offloaded") or m.get("_collapsed"):
                continue
            content = m.get("content")
            if not isinstance(content, str) or len(content) <= cap:
                continue
            pointer = self.artifacts.make_pointer(
                content, tool=m.get("name", ""),
                head=(cap * 2) // 3, tail=cap // 3,
            )
            if pointer is None:
                continue
            m["content"] = pointer
            m["_offloaded"] = True
            n += 1
        return n

    def _render_artifact_index(self) -> str:
        arts = self.artifacts.list()
        if not arts:
            return ""
        lines = ["## 可取回产物索引（read_artifact(id) 取全文）"]
        for a in arts[-30:]:
            lines.append(
                f"- {a['id']} [{a.get('tool', '?')}] {a.get('size', 0)}字符 "
                f"— {a.get('preview', '')[:80]}"
            )
        return "\n".join(lines)


    def _split_by_budget(self, recent_budget: int):
        
        msgs = self.messages
        n = len(msgs)
        if n == 0:
            return [], [], []

        used = 0
        split = n
        kept = 0
        for i in range(n - 1, -1, -1):
            t = self._estimate_one(msgs[i])
            over_budget = used + t > recent_budget
            enough_kept = kept >= self.context_keep_recent
            if over_budget and enough_kept and (n - i) > 1:
                split = i + 1
                break
            used += t
            kept += 1
            split = i

        # Never start `recent` with an orphan tool result (its assistant tool_call stays in to_summarize).
        while 0 < split < n and msgs[split].get("role") == "tool":
            split -= 1
        # Never end the old region on an assistant-with-tool_calls — pull it forward into recent too.
        while split > 0 and msgs[split - 1].get("role") == "assistant" \
                and msgs[split - 1].get("tool_calls"):
            split -= 1

        old = msgs[:split]
        recent = msgs[split:]

        pinned: list[dict] = []
        if old and old[0].get("role") == "user" and isinstance(old[0].get("content"), str):
            pinned = [old[0]]
            old = old[1:]
        return pinned, old, recent

    def _render_kb_snapshot(self) -> str:
        
        lines = ["## 知识库快照（权威结构化状态）"]
        targets = self.knowledge_base.list_targets() or []
        if targets:
            lines.append(f"### 目标 ({len(targets)})")
            for t in targets:
                ports = ",".join(str(p) for p in (t.get("open_ports") or []))
                svcs = "; ".join(
                    f"{k}={v}" for k, v in (t.get("services") or {}).items()
                )
                owned = " **[OWNED]**" if t.get("owned") else ""
                vulns = ",".join(t.get("vulns") or [])
                line = f"- {t.get('host', '?')}{owned}"
                if ports:
                    line += f" ports=[{ports}]"
                if svcs:
                    line += f" svc=[{svcs}]"
                if vulns:
                    line += f" vulns=[{vulns}]"
                notes = (t.get("notes") or "").strip()
                if notes:
                    line += f" notes={notes[:120]}"
                lines.append(line)

        finding_lines: list[str] = []
        for t in targets:
            for f in self.knowledge_base.get_findings(t.get("host", "")) or []:
                claim = getattr(f, "claim", str(f))
                sev = getattr(f, "severity", "") or "info"
                cve = getattr(f, "cve", "") or ""
                conf = getattr(f, "confidence", None)
                tag = f"sev={sev}"
                if cve:
                    tag += f" cve={cve}"
                if conf is not None:
                    tag += f" conf={conf}"
                finding_lines.append(f"- [{t.get('host')}] {claim[:160]} ({tag})")
        if finding_lines:
            lines.append(f"### 发现 ({len(finding_lines)})")
            lines.extend(finding_lines[:40])

        try:
            creds = self.knowledge_base.list_credentials()
        except Exception:
            creds = []
        if creds:
            lines.append(f"### 凭据 ({len(creds)})")
            for c in creds[:30]:
                v = "✓verified" if getattr(c, "verified", False) else "unverified"
                secret = getattr(c, "secret", "")
                preview = secret[:32] + ("…" if len(secret) > 32 else "")
                lines.append(
                    f"- {c.host} {c.username}:{preview} "
                    f"({c.type}/{c.service or '?'}:{c.port or '?'}) {v}"
                )

        if self.todos:
            lines.append("### Todos")
            glyph = {"completed": "x", "in_progress": "~", "pending": " "}
            for td in self.todos:
                lines.append(
                    f"- [{glyph.get(td.get('status', 'pending'), ' ')}] "
                    f"{td.get('content', '')}"
                )

        if len(lines) == 1:
            lines.append("(空)")
        return "\n".join(lines)

    _NINE_SEGMENTS = (
        "1. 目标与用户意图\n"
        "2. 资产（主机/端口/服务/版本）\n"
        "3. 漏洞与发现（含 CVE / 严重度 / 证据）\n"
        "4. 凭据（用户名/类型/服务/是否验证）\n"
        "5. 已完成步骤\n"
        "6. 进行中的工作\n"
        "7. 下一步计划\n"
        "8. 关键数据与产物（命令/URL/payload/artifact://指针）\n"
        "9. 待解决问题与障碍"
    )

    async def _update_progress_doc(
        self, prev_doc: str, to_summarize: list[dict]
    ) -> str:
        
        sum_system = (
            "你在为一个红队渗透 Agent 维护一份**九段结构进度文档**。下面给你：\n"
            "(A) 现有的进度文档（可能为空）；(B) 新增的一段对话历史。\n"
            "把两者**合并并更新**成一份新的进度文档，严格用以下九段标题组织：\n\n"
            f"{self._NINE_SEGMENTS}\n\n"
            "要求：\n"
            "- 每段下用短句/分点；没有内容的段写「(无)」但保留标题\n"
            "- 绝不丢失：目标/端口/服务/版本/漏洞/CVE/凭据/文件路径/payload/命令/URL\n"
            "- 第8段保留所有 artifact:// 指针，注明各自内容\n"
            "- 合并重复、删除寒暄和冗长正文；整体控制在 800 字内\n"
            "只输出更新后的九段文档正文，不要额外前缀或解释。"
        )
        transcript_parts: list[str] = []
        for m in to_summarize:
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                transcript_parts.append(f"[{role}] {content}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        transcript_parts.append(f"[{role}] {block.get('text', '')}")
                    elif isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                        transcript_parts.append(f"[{role}] (图片)")
            for call in m.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                transcript_parts.append(
                    f"[→tool] {fn.get('name', '?')}({(fn.get('arguments') or '')[:300]})"
                )
        transcript = "\n".join(transcript_parts)[:48000]

        user_block = (
            f"(A) 现有进度文档:\n{prev_doc or '（无）'}\n\n"
            f"(B) 新增历史:\n{transcript}"
        )
        sum_messages = [
            {"role": "system", "content": sum_system},
            {"role": "user", "content": user_block},
        ]

        parts: list[str] = []
        try:
            async for ev in self.llm_provider.chat(
                sum_messages, tools=None, stream=False
            ):
                kind = getattr(ev, "type", None)
                kv = kind.value if hasattr(kind, "value") else kind
                if kv == "text" and ev.content:
                    parts.append(ev.content)
                elif kv == "done":
                    meta = ev.metadata or {}
                    self._record_usage(meta.get("usage"), meta.get("model"))
                    break
                elif kv == "error":
                    return ""
        except Exception as exc:
            logger.warning("Summarization LLM call failed: %s", exc)
            return ""
        return "".join(parts).strip()

    async def _dream(self) -> None:
        
        if self.llm_provider is None:
            self.publish_action("未配置 LLM，无法做梦。")
            return
        self.publish_action("💤 做梦中：二次剪枝 + 整合进度文档…")

        async with self._get_chat_lock():
            # Tighten the progress doc itself (second pass).
            if self._progress_doc:
                tighter = await self._update_progress_doc(self._progress_doc, [])
                if tighter:
                    self._progress_doc = tighter

            # Force full compaction by lowering the budget to 0 (folds a not-yet-full context into the doc).
            saved = self.context_token_limit
            self.context_token_limit = 1
            try:
                await self._maybe_compact_context()
            finally:
                self.context_token_limit = saved

        used = self._estimate_messages_tokens(self.messages)
        self.publish_action(
            f"💤 做梦完成：当前 {used} tokens，进度文档已收紧，"
            f"{len(self.artifacts.list())} 个产物可 read_artifact 取回。"
        )


    async def _chat_with_image(self, prompt: str, image_path: str) -> None:
        
        import base64
        import mimetypes

        p = Path(image_path).expanduser()
        if not p.is_file():
            self.event_bus.publish(
                Event(type=EventType.ERROR, data={"message": f"image not found: {image_path}"})
            )
            return
        try:
            raw = p.read_bytes()
        except Exception as e:
            self.event_bus.publish(
                Event(type=EventType.ERROR, data={"message": f"failed to read image: {e}"})
            )
            return

        mime, _ = mimetypes.guess_type(str(p))
        if not mime or not mime.startswith("image/"):
            mime = "image/png"

        b64 = base64.b64encode(raw).decode("ascii")
        # OpenAI vision content-blocks; the Anthropic provider translates
        # image_url blocks into image source blocks.
        content_blocks = [
            {"type": "text", "text": prompt or "What do you see in this image?"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            },
        ]
        self.publish_action(
            f"📎 已附加图片 {p.name} ({len(raw)} bytes, {mime})"
        )
        # Append under the chat lock so the image can't be injected into another loop's tool sequence.
        async with self._get_chat_lock():
            self.messages.append({"role": "user", "content": content_blocks})
            await self._run_chat_locked()

    def _get_chat_lock(self) -> asyncio.Lock:
        if self._chat_lock is None:
            self._chat_lock = asyncio.Lock()
        return self._chat_lock

    def _signal_interrupt(self) -> None:
        
        self._interrupt = True
        fut = self._iter_continue_future
        if fut is not None and not fut.done():
            fut.set_result(False)
        fut = self._tool_approval_future
        if fut is not None and not fut.done():
            fut.set_result(False)
        fut = self._safety_approval_future
        if fut is not None and not fut.done():
            fut.set_result(False)
        if self._safety_approval_request_id:
            self.safety_gate.deny(self._safety_approval_request_id)
        for sub in list(self.active_sub_agents.values()):
            sub.request_stop()
        for task in list(self.active_sub_agent_tasks.values()):
            if not task.done():
                task.cancel()

    def shutdown(self) -> None:
        """Release subprocess-backed resources (PTY shells, OOB listener)."""
        for task in list(self.active_sub_agent_tasks.values()):
            if not task.done():
                task.cancel()
        self.active_sub_agent_tasks.clear()
        self.active_sub_agents.clear()
        try:
            self.shells.close_all()
        except Exception:
            logging.getLogger(__name__).exception("Shell session cleanup failed")
        try:
            self.oob.stop()
        except Exception:
            logging.getLogger(__name__).exception("OOB listener cleanup failed")

    async def _chat_with_llm(self, user_text: str, _skip_user_message: bool = False) -> None:
        
        async with self._get_chat_lock():
            if not _skip_user_message:
                self.messages.append({"role": "user", "content": user_text})
            await self._run_chat_locked()

    async def _run_chat_locked(self) -> None:
        
        tools = self._build_tool_schemas()
        system_prompt = self._build_system_prompt()
        self._chat_active = True
        self._interrupt = False
        try:
            await self._chat_loop(tools, system_prompt)
        finally:
            self._chat_active = False
            self._interrupt = False

    async def _chat_loop(self, tools, system_prompt) -> None:
        iteration = 0
        next_checkpoint = self.iteration_soft_threshold
        while True:
            if self._interrupt:
                self.publish_action(f"⏹ 已在第 {iteration} 步停止当前任务。")
                return
            iteration += 1
            await self._maybe_compact_context()
            if (
                self.iteration_soft_threshold > 0
                and iteration > next_checkpoint
            ):
                should_continue = await self._ask_continue_iteration(iteration - 1)
                if not should_continue:
                    self.publish_action(
                        f"用户取消，已在第 {iteration - 1} 步停止本轮推理。"
                    )
                    return
                next_checkpoint += self.iteration_soft_threshold

            self.frontier.prune_expired()
            if self._pending_observer_msg:
                self.messages.append(
                    {"role": "user", "content": self._pending_observer_msg}
                )
                self._pending_observer_msg = None
            request_messages = [
                {"role": "system", "content": system_prompt + "\n\n" + self.frontier.view()},
                *self.messages,
            ]
            self.event_bus.publish(
                Event(
                    type=EventType.STATUS_UPDATE,
                    data={"text": f"LLM thinking… (step {iteration})"},
                )
            )

            text_parts: list[str] = []
            pending_calls: list[dict] = []
            assistant_message: Optional[dict] = None
            error_seen: Optional[str] = None

            import uuid as _uuid
            stream_id = _uuid.uuid4().hex[:8]
            stream_opened = False

            try:
                async for ev in self.llm_provider.chat(
                    request_messages, tools=tools, stream=True
                ):
                    kind = getattr(ev, "type", None)
                    kind_value = kind.value if hasattr(kind, "value") else kind
                    if kind_value == "text":
                        if ev.content:
                            text_parts.append(ev.content)
                            self._publish_stream_delta(stream_id, ev.content, stream_opened)
                            stream_opened = True
                    elif kind_value == "tool_call":
                        pending_calls.append({
                            "id": (ev.metadata or {}).get("tool_call_id", ""),
                            "name": ev.tool_name,
                            "input": ev.tool_input or {},
                        })
                    elif kind_value == "error":
                        error_seen = ev.content or "unknown LLM error"
                        break
                    elif kind_value == "done":
                        assistant_message = (ev.metadata or {}).get("assistant_message")
                        meta = ev.metadata or {}
                        self._record_usage(meta.get("usage"), meta.get("model"))
                        break
            except Exception as exc:
                logger.exception("LLM call failed")
                if stream_opened:
                    self._publish_stream_end(stream_id)
                self.event_bus.publish(
                    Event(type=EventType.ERROR, data={"message": f"LLM call failed: {exc}"})
                )
                return

            raw_text = "".join(text_parts)
            text_reply = raw_text.strip()
            if stream_opened:
                # Finalize the bubble with the raw (un-stripped) text so delta-arrived trailing whitespace isn't lost from the display.
                self._publish_stream_end(stream_id, raw_text)

            if error_seen:
                self.event_bus.publish(
                    Event(type=EventType.ERROR, data={"message": f"LLM error: {error_seen}"})
                )
                return

            if pending_calls:
                if assistant_message is None:
                    assistant_message = {
                        "role": "assistant",
                        "content": text_reply,
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
                self.messages.append(assistant_message)

                # Tool calls in one assistant turn run in parallel (OpenAI
                # spec permits it); the LLM must not issue conflicting writes.
                if len(pending_calls) > 1:
                    results = await asyncio.gather(
                        *[
                            self._execute_tool(c["name"], c["input"])
                            for c in pending_calls
                        ],
                        return_exceptions=True,
                    )
                    for call, res in zip(pending_calls, results):
                        if isinstance(res, Exception):
                            res_text = json.dumps(
                                {"error": f"tool raised: {res}"}, ensure_ascii=False
                            )
                        else:
                            res_text = res
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"] or "",
                            "name": call["name"],
                            "content": res_text,
                        })
                else:
                    for call in pending_calls:
                        result_text = await self._execute_tool(
                            call["name"], call["input"]
                        )
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"] or "",
                            "name": call["name"],
                            "content": result_text,
                        })
                continue

            final = text_reply or "(LLM 没有返回内容)"
            self.messages.append({"role": "assistant", "content": final})
            return

    def _publish_stream_delta(
        self, stream_id: str, delta: str, already_open: bool
    ) -> None:
        
        self.event_bus.publish(
            Event(
                type=EventType.AGENT_MESSAGE,
                data={
                    "role": "assistant",
                    "source": "agent",
                    "type": "think",
                    "streaming": True,
                    "stream_id": stream_id,
                    "delta": delta,
                    "first": not already_open,
                },
            )
        )

    def _publish_stream_end(self, stream_id: str, full_text: str = "") -> None:
        
        self.event_bus.publish(
            Event(
                type=EventType.AGENT_MESSAGE,
                data={
                    "role": "assistant",
                    "source": "agent",
                    "type": "think",
                    "streaming": True,
                    "stream_id": stream_id,
                    "final": True,
                    "text": full_text,
                    "content": full_text,
                },
            )
        )

    async def _ask_tool_permission(
        self, tool_name: str, preview: str, decision: Any
    ) -> bool:
        
        async with self._tool_approval_lock:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._tool_approval_future = fut
            self._tool_approval_args = (tool_name, decision.args_repr or "")

            self.event_bus.publish(
                Event(
                    type=EventType.APPROVAL_REQUEST,
                    data={
                        "request_id": f"perm:{tool_name}",
                        "operation": f"{tool_name} — {preview}",
                        "risk_level": "L2",
                        "target": "(permission gate)",
                        "requires_approval": True,
                        "requires_confirmation_phrase": False,
                        "rule": decision.reason,
                    },
                )
            )
            self.publish_action(
                f"工具 {tool_name} 需要授权: {decision.reason} "
                f"[y]批准本次 / [a]总是允许本工具 / [n]拒绝"
            )

            try:
                approved = await fut
            finally:
                self._tool_approval_future = None
                self._tool_approval_args = None
        return bool(approved)

    async def _ask_continue_iteration(self, steps_so_far: int) -> bool:
        
        if self._iter_continue_future is not None and not self._iter_continue_future.done():
            self._iter_continue_future.set_result(False)

        loop = asyncio.get_running_loop()
        self._iter_continue_future = loop.create_future()

        self.event_bus.publish(
            Event(
                type=EventType.APPROVAL_REQUEST,
                data={
                    "request_id": "iter_continue",
                    "operation": (
                        f"已执行 {steps_so_far} 步工具调用，是否继续推理？"
                    ),
                    "risk_level": "L1",
                    "target": "(iteration soft-limit)",
                    "requires_approval": True,
                    "requires_confirmation_phrase": False,
                },
            )
        )
        self.publish_action(
            f"已执行 {steps_so_far} 步工具调用。继续推理请输入 [y]，停止请输入 [n]。"
        )

        try:
            approved = await self._iter_continue_future
        finally:
            self._iter_continue_future = None
        return bool(approved)

    async def _handle_scan_command(self, text: str) -> None:
        
        target = text.replace("/scan", "").strip()
        if not target:
            self.publish_action("Usage: /scan <target-ip-or-hostname>")
            return

        self.publish_action(f"Dispatching reconnaissance against {target}")
        self.knowledge_base.update_target(target)
        await self._dispatch_sub_agent(
            "recon", target, f"Comprehensive scan of {target}", TaskPriority.RECON
        )

    async def _handle_exploit_command(self, text: str) -> None:
        
        parts = text.split()
        if len(parts) < 2:
            self.publish_action("Usage: /exploit <target> [--cve CVE-ID]")
            return

        target = parts[1]
        cve = None
        if "--cve" in parts:
            idx = parts.index("--cve")
            if idx + 1 < len(parts):
                cve = parts[idx + 1]

        cve_desc = f" targeting {cve}" if cve else ""
        self.publish_action(f"Dispatching exploitation against {target}{cve_desc}")

        check = self.safety_gate.check(
            f"exploit:{target}", RiskLevel.L2, target
        )
        if not check.approved and check.requires_approval:
            approved = await self._await_safety_approval(
                request_id=check.request_id,
                operation=f"exploit:{target}",
                risk_level=RiskLevel.L2,
                target=target,
                requires_approval=check.requires_approval,
                requires_confirmation_phrase=check.requires_confirmation_phrase,
            )
            if not approved:
                self.publish_action(f"Exploitation against {target} denied by user")
                return

        await self._dispatch_sub_agent(
            "exploit", target, f"Exploit {target}{cve_desc}", TaskPriority.EXPLOIT
        )

    async def _handle_target_command(self, text: str) -> None:
        
        parts = text.split()
        if len(parts) >= 2:
            target = parts[1]
            info = self.knowledge_base.get_target(target)
            if info:
                self.publish_think(
                    f"Target {target}: ports={info.get('open_ports', [])}, "
                    f"services={list(info.get('services', {}).keys())}, "
                    f"owned={info.get('owned', False)}"
                )
            else:
                self.publish_think(f"Target {target} not found in knowledge base")
        else:
            targets = self.knowledge_base.list_targets()
            if targets:
                summary = "; ".join(
                    f"{t['host']} (ports={len(t.get('open_ports', []))})"
                    for t in targets
                )
                self.publish_think(f"Known targets: {summary}")
            else:
                self.publish_think("No targets in knowledge base")

    async def _handle_status_command(self, text: str) -> None:
        
        pending = self.scheduler.pending
        active = len(self.active_sub_agents)
        owned = len(self.knowledge_base.owned_targets())
        total_targets = len(self.knowledge_base.list_targets())

        status_text = (
            f"Agent status: running={self.running}, "
            f"pending tasks={pending}, active sub-agents={active}, "
            f"targets={total_targets}, owned={owned}"
        )
        self.publish_think(status_text)


    async def _react_cycle(self, target: str, context: dict) -> None:
        
        if not self.running:
            return

        # ReAct heuristic stub: Plan → Think → Act → Observe → Reflect.
        plan = self._plan(target, context)
        self.publish_think(f"PLAN: {plan}")

        analysis = self._think(target, context)
        self.publish_think(f"THINK: {analysis}")

        action = self._decide_action(analysis)
        self.publish_action(f"ACTION: {action}")

        observe_result = await self._execute_action(action)

        self._reflect(target, action, observe_result)

    def _plan(self, target: str, context: dict) -> str:
        
        targets = self.knowledge_base.list_targets()
        pending_sub_agents = list(self.active_sub_agents.keys())
        pending_approvals = self.safety_gate.get_pending()

        parts = [f"Current state: {len(targets)} target(s)"]
        if pending_sub_agents:
            parts.append(f", {len(pending_sub_agents)} active sub-agent(s)")
        if pending_approvals:
            parts.append(f", {len(pending_approvals)} pending approval(s)")
        if self.scheduler.pending:
            parts.append(f", {self.scheduler.pending} queued task(s)")

        message = context.get("message", "")
        if message:
            parts.append(f". Processing: {message[:120]}")

        plan = " ".join(parts)

        target_data = self.knowledge_base.get_target(target)
        if target_data and target_data.get("services"):
            services = list(target_data["services"].keys())
            skills = self.skills_registry.match(services)
            if skills:
                skill_names = [s.get("name", "?") for s in skills[:3]]
                plan += f" | Matching skills: {', '.join(skill_names)}"

        return plan

    def _think(self, target: str, context: dict) -> str:
        
        target_data = self.knowledge_base.get_target(target)
        findings = self.knowledge_base.get_findings(target)

        evidence_lines = []

        if target_data:
            ports = target_data.get("open_ports", [])
            services = target_data.get("services", {})
            if ports:
                evidence_lines.append(
                    f"Observed ports: {', '.join(str(p) for p in ports)}"
                )
            if services:
                service_summary = "; ".join(
                    f"{svc}" for svc in services.keys()
                )
                evidence_lines.append(f"Observed services: {service_summary}")

        if findings:
            evidence_lines.append(
                f"{len(findings)} existing finding(s) on {target}"
            )

        if not evidence_lines:
            return (
                "Insufficient evidence for analysis. "
                "Dispatch deeper reconnaissance to gather service versions, "
                "port states, and response data before forming hypotheses."
            )

        analysis = "Evidence-driven analysis:\n- " + "\n- ".join(evidence_lines)

        for finding in findings:
            if finding.cve and finding.verified:
                analysis += (
                    f"\nVerified CVE {finding.cve} on {target} "
                    f"(confidence: {finding.confidence})"
                )

        return analysis

    def _decide_action(self, analysis: str) -> dict:
        
        if "Insufficient evidence" in analysis:
            return {
                "type": "dispatch_sub_agent",
                "params": {
                    "agent_type": "recon",
                    "operation": "nmap_scan",
                    "description": "Reconnaissance to gather evidence",
                },
            }

        return {
            "type": "write_script",
            "params": {
                "language": "python",
                "description": "Analyze collected data",
            },
        }

    async def _execute_action(self, action: dict) -> Any:
        
        action_type = action.get("type")
        params = action.get("params", {})

        if action_type == "dispatch_sub_agent":
            return await self._dispatch_sub_agent(
                agent_type=params.get("agent_type", "recon"),
                target=params.get("target", "unknown"),
                task=params.get("description", "No description"),
                priority=params.get("priority", TaskPriority.RECON),
            )

        if action_type == "write_script":
            code = params.get("code", "print('hello')")
            language = params.get("language", "python")
            return await self._execute_script(code, language)

        if action_type == "request_approval":
            return await self._request_approval(
                operation=params.get("operation", "unknown"),
                target=params.get("target", "unknown"),
            )

        return None

    def _reflect(self, target: str, action: dict, result: Any) -> None:
        
        if result is None:
            self.publish_action("Action produced no result — switching path")
            return

        if isinstance(result, SandboxResult):
            if result.status == "timeout" or result.status == "error":
                retry_key = f"{target}:{action.get('type', 'unknown')}"
                count = self._retry_counts.get(retry_key, 0) + 1
                self._retry_counts[retry_key] = count

                if count < 3:
                    self.publish_think(
                        f"Script {result.status} (attempt {count}/3) — retrying"
                    )
                else:
                    self.publish_action(
                        f"Script failed after 3 attempts — switching path"
                    )
            else:
                self.publish_think("Script executed successfully")

        elif isinstance(result, SubAgentResult):
            if result.status.value == "done":
                self.publish_think(
                    f"Sub-agent {result.agent_id} completed: "
                    f"{result.scripts_executed} script(s), "
                    f"{len(result.findings)} finding(s)"
                )
                if result.new_targets:
                    for host in result.new_targets:
                        self.knowledge_base.update_target(host)
            elif result.error:
                self.publish_action(
                    f"Sub-agent {result.agent_id} failed: {result.error[:200]}"
                )


    async def _dispatch_sub_agent(
        self,
        agent_type: str,
        target: str,
        task: str,
        priority: TaskPriority = TaskPriority.RECON,
    ) -> SubAgentResult:
        
        await self.scheduler.wait_for_slot()

        sub_system = (
            f"你是一个专注的 {agent_type} 子任务 Agent。"
            "主 Agent 委派你完成一个**独立**的子任务。\n"
            "你看不到主对话历史，所有需要的信息都在用户消息里。\n"
            "你拥有与主 Agent 相同的工具集（除了 task —— 禁止递归）。\n"
            "完成任务后返回简洁、结构化的最终答复（包括关键证据），"
            "主 Agent 会以你的答复为准。失败请如实汇报。"
        )
        sub_system += SUB_AGENT_DISCIPLINE
        sub_system += "\n" + self.blackboard.render(1500) + "\n"
        if self.skills_registry is not None:
            skills = self.skills_registry.match([agent_type, target]) or []
            if not skills:
                skills = self.skills_registry.list_by_category(agent_type)
            skill_blocks = [
                (s.get("system_prompt") or "")[:2000]
                for s in skills[:3]
                if s.get("system_prompt")
            ]
            if skill_blocks:
                sub_system += "\n【专业技能参考】\n" + "\n---\n".join(skill_blocks)
        tools = [
            t for t in self._build_tool_schemas()
            if t["function"]["name"] != "task"
        ]

        _intent_holder: dict[str, str] = {}

        async def _sub_executor(name: str, tool_args: dict) -> str:
            res = await self._execute_tool(name, tool_args)
            iid = _intent_holder.get("id")
            if iid:
                self.frontier.tick(iid, 1)
            return res

        sub = SubAgent(
            agent_type=agent_type,
            target=target,
            task=task,
            event_bus=self.event_bus,
            llm_provider=self.llm_provider,
            tool_executor=_sub_executor,
            tool_schemas=tools,
            system_prompt=sub_system,
            ttl=300,
            max_iterations=12,
            parallel_tool_calls=True,
            usage_callback=self._record_usage,
        )

        if not self.scheduler.try_acquire(target):
            self.publish_action(
                f"Sub-agent {sub.agent_id} skipped — target {target} at capacity"
            )
            return SubAgentResult(
                agent_id=sub.agent_id,
                status=sub.status,
                scripts_executed=0,
                error="Target at concurrency capacity",
            )

        findings_before = self.knowledge_base.finding_total()
        targets_before = {t["host"] for t in self.knowledge_base.list_targets()}

        intent_id = self.frontier.add_intent(
            hypothesis=task[:200],
            action=f"{agent_type}@{target}",
            priority=priority.value,
            max_steps=sub.max_iterations,
            expiry_s=float(sub.ttl),
        )
        if intent_id:
            self.frontier.claim(intent_id)
            _intent_holder["id"] = intent_id
            ctx = self._focused_context(intent_id, scope=target)
            if ctx:
                sub.system_prompt = sub.system_prompt + "\n\n" + ctx

        self.active_sub_agents[sub.agent_id] = sub
        task = asyncio.create_task(sub.run())
        self.active_sub_agent_tasks[sub.agent_id] = task
        try:
            result = await task
        finally:
            self.scheduler.task_completed(target)
            self.active_sub_agent_tasks.pop(sub.agent_id, None)
            self.active_sub_agents.pop(sub.agent_id, None)
        if intent_id:
            if result.status is SubAgentStatus.DONE:
                self.frontier.complete(intent_id, (result.text or "")[:200])
            else:
                self.frontier.kill(intent_id, result.error or result.status.value)
        result.findings = self.knowledge_base.findings_since(findings_before)
        result.new_targets = [
            h
            for h in {t["host"] for t in self.knowledge_base.list_targets()}
            - targets_before
        ]
        return result


    async def _execute_script(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        
        self._script_counter += 1
        tool_name = f"execute_{language}_script#{self._script_counter}"
        self.event_bus.publish(
            Event(
                type=EventType.TOOL_CALL,
                data={
                    "tool": tool_name,
                    "script_num": self._script_counter,
                    "language": language,
                    "code": code[:500],
                    "status": "running",
                },
            )
        )

        if language == "python":
            result = self.python_sandbox.run(code)
        elif language == "bash":
            result = self.bash_sandbox.run(code)
        else:
            raise ValueError(f"Unsupported script language: {language}")

        output = result.stdout or ""
        if result.stderr:
            output = (output + "\n" + result.stderr) if output else result.stderr
        self.event_bus.publish(
            Event(
                type=EventType.TOOL_RESULT,
                data={
                    "tool": tool_name,
                    "script_num": self._script_counter,
                    "status": result.status,
                    "output": output[:2000],
                    "stdout": result.stdout[:2000],
                    "stderr": result.stderr[:2000],
                },
            )
        )
        return result


    async def _await_safety_approval(
        self,
        request_id: Optional[str],
        operation: str,
        risk_level: RiskLevel,
        target: str,
        requires_approval: bool,
        requires_confirmation_phrase: bool,
        timeout: float = 600.0,
    ) -> bool:
        async with self._safety_approval_lock:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._safety_approval_future = fut
            self._safety_approval_request_id = request_id
            self._safety_approval_requires_phrase = requires_confirmation_phrase

            self.event_bus.publish(
                Event(
                    type=EventType.APPROVAL_REQUEST,
                    data={
                        "request_id": request_id,
                        "operation": operation,
                        "risk_level": risk_level.value,
                        "target": target,
                        "requires_approval": requires_approval,
                        "requires_confirmation_phrase": requires_confirmation_phrase,
                    },
                )
            )
            if requires_confirmation_phrase:
                self.publish_action(
                    f"破坏性操作 {operation}（target={target}）需要确认。"
                    f"请输入精确短语「{DESTROY_CONFIRMATION_PHRASE}」以继续。"
                )
            else:
                self.publish_action(
                    f"操作 {operation}（target={target}）需要审批 "
                    f"[y]批准 / [n]拒绝 / [v]查看详情"
                )

            try:
                approved = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                if request_id:
                    self.safety_gate.deny(request_id)
                self.publish_action(f"审批超时，操作 {operation} 已拒绝。")
                approved = False
            finally:
                self._safety_approval_future = None
                self._safety_approval_request_id = None
                self._safety_approval_requires_phrase = False
        return bool(approved)

    def _publish_approval_details(self, request_id: Optional[str]) -> None:
        for req in self.safety_gate.get_pending():
            if req.request_id == request_id:
                self.publish_action(
                    f"审批详情 — 操作: {req.operation} | 目标: {req.target} | "
                    f"风险等级: {req.risk_level.value}"
                )
                return
        self.publish_action(f"未找到待审批请求 {request_id}（可能已处理）。")

    async def _request_approval(
        self,
        operation: str,
        risk_level: RiskLevel = RiskLevel.L2,
        target: str = "unknown",
        timeout: float = 600.0,
    ) -> bool:
        check: CheckResult = self.safety_gate.check(operation, risk_level, target)

        if check.approved:
            logger.info("Operation auto-approved: %s on %s", operation, target)
            return True

        return await self._await_safety_approval(
            request_id=check.request_id,
            operation=operation,
            risk_level=risk_level,
            target=target,
            requires_approval=check.requires_approval,
            requires_confirmation_phrase=check.requires_confirmation_phrase,
            timeout=timeout,
        )

    async def _handle_approval_response(self, event: Event) -> None:
        
        raw_response = (event.data.get("response") or "").strip().lower()
        raw_response_exact = (event.data.get("response") or "").strip()
        approved_field = event.data.get("approved")
        always = raw_response in ("a", "always", "总是")
        if approved_field is None and raw_response:
            approved_field = raw_response in ("y", "yes", "继续") or always

        # Tool-permission prompt takes priority over the iteration prompt.
        if (
            self._tool_approval_future is not None
            and not self._tool_approval_future.done()
        ):
            granted = bool(approved_field)
            if granted and always and self._tool_approval_args is not None:
                tool_name, _ = self._tool_approval_args
                self.permissions.grant_session(tool_name)
                self.publish_action(f"已永久授权工具 {tool_name}（本会话内）")
            self._tool_approval_future.set_result(granted)
            return

        # Iteration-continue prompt takes priority when active.
        if (
            self._iter_continue_future is not None
            and not self._iter_continue_future.done()
            and event.data.get("request_id") in (None, "iter_continue")
        ):
            self._iter_continue_future.set_result(bool(approved_field))
            return

        if (
            self._safety_approval_future is not None
            and not self._safety_approval_future.done()
        ):
            req_id = self._safety_approval_request_id
            if raw_response == "v":
                self._publish_approval_details(req_id)
                return
            if self._safety_approval_requires_phrase:
                # L4: only the exact (case-sensitive) confirmation phrase approves.
                if raw_response_exact == DESTROY_CONFIRMATION_PHRASE:
                    if req_id:
                        self.safety_gate.approve(req_id)
                    self._safety_approval_future.set_result(True)
                else:
                    if req_id:
                        self.safety_gate.deny(req_id)
                    self.publish_action(
                        f"拒绝：破坏性操作需要输入精确确认短语「{DESTROY_CONFIRMATION_PHRASE}」。"
                    )
                    self._safety_approval_future.set_result(False)
                return
            if bool(approved_field):
                if req_id:
                    self.safety_gate.approve(req_id)
                self._safety_approval_future.set_result(True)
            else:
                if req_id:
                    self.safety_gate.deny(req_id)
                self._safety_approval_future.set_result(False)
            return

        request_id = event.data.get("request_id")
        approved = bool(approved_field)

        if approved:
            if request_id and self.safety_gate.approve(request_id):
                self.publish_action(
                    f"Operation approved (request {request_id})"
                )
            elif request_id:
                self.publish_action(
                    f"Approval request {request_id} not found (already processed)"
                )
        else:
            if request_id:
                self.safety_gate.deny(request_id)
                self.publish_action(
                    f"Operation denied (request {request_id})"
                )


    def create_evidence(
        self,
        evidence_type: str,
        value: str,
        source: str = "",
        cve: str = "",
        payload: str = "",
    ) -> Evidence:
        
        return Evidence(
            type=evidence_type,
            value=value,
            source=source,
            cve=cve,
            payload=payload,
        )

    def add_finding(
        self,
        target: str,
        claim: str,
        confidence: float,
        evidence_list: list[Evidence],
        cve: str = "",
        severity: str = "info",
    ) -> Finding:
        """Create and register a Finding in the KnowledgeBase."""
        if not evidence_list:
            logger.warning("Finding '%s' added without evidence", claim)

        finding = Finding(
            claim=claim,
            confidence=confidence,
            evidence=evidence_list,
            verified=bool(cve),
            cve=cve,
            severity=severity,
        )
        self.knowledge_base.add_finding(target, finding)
        return finding


    def publish_think(self, text: str) -> None:
        """Publish an AGENT_MESSAGE event with think content."""
        self.event_bus.publish(
            Event(
                type=EventType.AGENT_MESSAGE,
                data={
                    "role": "assistant",
                    "source": "agent",
                    "text": text,
                    "content": text,
                    "type": "think",
                },
            )
        )

    def publish_action(self, text: str) -> None:
        """Publish an AGENT_MESSAGE event with action content."""
        self.event_bus.publish(
            Event(
                type=EventType.AGENT_MESSAGE,
                data={
                    "role": "assistant",
                    "source": "agent",
                    "text": text,
                    "content": text,
                    "type": "action",
                },
            )
        )

