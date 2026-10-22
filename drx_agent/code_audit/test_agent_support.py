"""Shared fakes and fixtures for the audit agent tests.

The production ``llm``, ``tools`` and ``forum`` modules are owned by other
teams.  Until they are available, this module installs minimal contract
implementations so the agent engine can be exercised in isolation.  When the
real modules are importable they are used unchanged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import types
from dataclasses import dataclass, field
from typing import Any

from drx_agent.code_audit import config as config_mod
from drx_agent.code_audit import prompt as prompt_mod


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


# ---------------------------------------------------------------------------
# llm stub
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on sibling team progress
    from drx_agent.code_audit import llm as llm_pkg
except Exception:  # noqa: BLE001
    llm_pkg = None

if llm_pkg is None:
    llm = _module("drx_agent.code_audit.llm")

    class LLMError(Exception):
        pass

    class APIError(LLMError):
        pass

    class NetworkError(LLMError):
        pass

    class IncompleteGeneration(LLMError):
        pass

    class ToolProtocolError(LLMError):
        pass

    @dataclass
    class Message:
        role: str = ""
        content: str = ""
        type: str = ""
        call_id: str = ""
        name: str = ""
        arguments: str = ""
        id: str = ""

    @dataclass
    class ToolDefinition:
        type: str = "function"
        name: str = ""
        description: str = ""
        parameters: Any = field(default_factory=dict)
        strict: bool = False

    @dataclass
    class FunctionCall:
        id: str = ""
        call_id: str = ""
        name: str = ""
        arguments: str = ""

    @dataclass
    class ToolResponse:
        content: str = ""
        thinking: str = ""
        calls: list[FunctionCall] = field(default_factory=list)
        response_id: str = ""

    @dataclass
    class Delta:
        content: str = ""
        thinking: str = ""
        progress: Any = None

    @dataclass
    class GenerationProgress:
        output_tokens: int = 0
        reasoning_tokens: int = 0
        tool_tokens: int = 0
        estimated: bool = True
        received_at: str = ""

    @dataclass
    class UsageUpdate:
        request_id: int = 0
        total_tokens: int = 0
        estimated: bool = True
        final: bool = False

    def validate_tool_history(messages) -> None:
        pending = ""
        seen = set()
        for message in messages or []:
            kind = getattr(message, "type", "") or ""
            if kind == "function_call":
                if pending or getattr(message, "call_id", "") in seen:
                    raise ValueError("native history contains duplicate or unpaired function call")
                pending = getattr(message, "call_id", "")
                seen.add(pending)
            elif kind == "function_call_output":
                if not pending or getattr(message, "call_id", "") != pending:
                    raise ValueError("native history contains orphan function output")
                pending = ""
            elif pending:
                raise ValueError("native history interrupts a pending function call")

    def estimate_text_tokens(text: str) -> int:
        from drx_agent.code_audit.agent.context import estimate_text_tokens as _impl

        return _impl(text)

    def estimate_tokens(messages) -> int:
        from drx_agent.code_audit.agent.context import estimate_tokens as _impl

        return _impl(messages)

    llm.LLMError = LLMError
    llm.APIError = APIError
    llm.NetworkError = NetworkError
    llm.IncompleteGeneration = IncompleteGeneration
    llm.ToolProtocolError = ToolProtocolError
    llm.Message = Message
    llm.ToolDefinition = ToolDefinition
    llm.FunctionCall = FunctionCall
    llm.ToolResponse = ToolResponse
    llm.Delta = Delta
    llm.GenerationProgress = GenerationProgress
    llm.UsageUpdate = UsageUpdate
    llm.validate_tool_history = validate_tool_history
    llm.estimate_text_tokens = estimate_text_tokens
    llm.estimate_tokens = estimate_tokens


# ---------------------------------------------------------------------------
# tools stub
# ---------------------------------------------------------------------------

if llm_pkg is not None:
    llm = llm_pkg

try:  # pragma: no cover
    from drx_agent.code_audit import tools as tools_pkg
except Exception:  # noqa: BLE001
    tools_pkg = None

if tools_pkg is not None:
    tools = tools_pkg

if tools_pkg is None:
    tools = _module("drx_agent.code_audit.tools")

    @dataclass
    class Result:
        ok: bool = True
        data: Any = None
        error: str = ""
        truncated: bool = False
        message: str = ""

        def to_dict(self) -> dict[str, Any]:
            data = {"ok": self.ok}
            if self.data is not None:
                data["data"] = self.data
            if self.error:
                data["error"] = self.error
            if self.truncated:
                data["truncated"] = True
            if self.message:
                data["message"] = self.message
            return data

    @dataclass
    class Todo:
        id: int = 0
        title: str = ""
        status: str = "pending"
        priority: str = ""

    @dataclass
    class Finding:
        id: int = 0
        key: str = ""
        severity: str = ""
        title: str = ""
        path: str = ""
        line: int = 0
        evidence: str = ""
        impact: str = ""
        recommendation: str = ""
        cwe: str = ""

    @dataclass
    class FileReview:
        path: str = ""
        status: str = "unseen"
        note: str = ""

    @dataclass
    class VariableReview:
        name: str = ""
        path: str = ""
        status: str = ""
        note: str = ""

    @dataclass
    class FlowReview:
        name: str = ""
        status: str = ""
        entry: str = ""
        files: list[str] = field(default_factory=list)
        variables: list[str] = field(default_factory=list)
        evidence: str = ""
        next_step: str = ""
        note: str = ""

    @dataclass
    class AuditState:
        ended: bool = False
        summary: str = ""
        next_steps: str = ""

    @dataclass
    class ProjectNote:
        note: str = ""

    @dataclass
    class Snapshot:
        todos: list[Todo] = field(default_factory=list)
        findings: list[Finding] = field(default_factory=list)
        project: ProjectNote = field(default_factory=ProjectNote)
        files: list[FileReview] = field(default_factory=list)
        variables: list[VariableReview] = field(default_factory=list)
        flows: list[FlowReview] = field(default_factory=list)
        audit: AuditState = field(default_factory=AuditState)

    def finding_key(finding: Finding) -> str:
        data = dataclasses.asdict(finding)
        data["key"] = ""
        return hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    TOOL_NAMES = [
        "list_files",
        "read_file",
        "search_content",
        "search_context",
        "git_inspect",
        "todo_create",
        "todo_update",
        "file_review_update",
        "variable_review_update",
        "flow_review_update",
        "flow_review_delete",
        "review_state",
        "project_note_update",
        "report_finding",
        "end_audit",
        "read_tool_buffer",
        "load_skill",
        "verify_finding",
        "audit_plan_done",
    ]

    def definitions():
        return [
            llm.ToolDefinition(
                type="function",
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
            )
            for name in TOOL_NAMES
            if name not in ("load_skill", "verify_finding", "audit_plan_done")
        ]

    def is_known_tool(name: str) -> bool:
        return name in TOOL_NAMES

    def is_terminal_tool(name: str) -> bool:
        return name == "end_audit"

    class Registry:
        def __init__(self, workspace: str, max_tool_result_chars: int = 12000) -> None:
            self.workspace = os.path.abspath(workspace)
            self.max_tool_result_chars = max(1024, min(65536, int(max_tool_result_chars)))
            self.todos: list[Todo] = []
            self.findings: list[Finding] = []
            self.project = ProjectNote()
            self.files: list[FileReview] = []
            self.variables: list[VariableReview] = []
            self.flows: list[FlowReview] = []
            self.audit = AuditState()
            self._buffer: tuple[str, str] | None = None
            self._buffer_seq = 0
            self.inventory = self._scan()

        def _scan(self) -> list[str]:
            paths = []
            for root, dirs, files in os.walk(self.workspace):
                dirs[:] = [item for item in dirs if not item.startswith(".")]
                for name in files:
                    rel = os.path.relpath(os.path.join(root, name), self.workspace)
                    paths.append(rel.replace("\\", "/"))
            return sorted(paths)

        def set_workspace(self, path: str) -> None:
            self.workspace = os.path.abspath(path)
            self.inventory = self._scan()

        def fork(self) -> "Registry":
            return Registry(self.workspace, self.max_tool_result_chars)

        def close(self) -> None:
            self._buffer = None

        def clear_buffer(self) -> None:
            self._buffer = None

        def bound_result(self, tool: str, full: str) -> str:
            self._buffer = None
            if len(full.encode("utf-8")) <= self.max_tool_result_chars:
                return full
            self._buffer_seq += 1
            buffer_id = "buffer-test-%d" % self._buffer_seq
            self._buffer = (buffer_id, full)
            return json.dumps(
                {
                    "ok": True,
                    "truncated": True,
                    "buffer_id": buffer_id,
                    "tool": tool,
                    "total_bytes": len(full.encode("utf-8")),
                    "offset": 0,
                    "next_offset": 0,
                    "eof": False,
                    "preview": full[: self.max_tool_result_chars // 2],
                },
                ensure_ascii=False,
            )

        def snapshot(self) -> Snapshot:
            return Snapshot(
                todos=list(self.todos),
                findings=list(self.findings),
                project=ProjectNote(self.project.note),
                files=list(self.files),
                variables=list(self.variables),
                flows=list(self.flows),
                audit=AuditState(self.audit.ended, self.audit.summary, self.audit.next_steps),
            )

        def restore_snapshot(self, snapshot: Snapshot) -> None:
            self.todos = list(snapshot.todos)
            self.findings = list(snapshot.findings)
            self.project = ProjectNote(snapshot.project.note)
            self.files = list(snapshot.files)
            self.variables = list(snapshot.variables)
            self.flows = list(snapshot.flows)
            self.audit = AuditState(snapshot.audit.ended, snapshot.audit.summary, snapshot.audit.next_steps)

        def tool_prompt(self) -> str:
            return "工具使用原则：每个回合只调用一个原生工具。"

        def review_prompt(self, limit: int) -> str:
            files = "".join("- [%s] %s\n" % (item.status, item.path) for item in self.files[:limit or 120])
            return "# 当前排查状态\n\n" + (files or "暂无已纳入审计范围的文件。\n")

        def apply_audit_scope(self, paths) -> list[str]:
            applied = []
            for path in paths or []:
                normalized = str(path).replace("\\", "/").lstrip("./")
                if normalized not in self.inventory:
                    continue
                applied.append(normalized)
                for item in self.files:
                    if item.path == normalized:
                        if item.status == "skipped":
                            item.status = "reviewing"
                        if not item.note:
                            item.note = "规划阶段纳入 one-shot 审计范围"
                        break
                else:
                    self.files.append(
                        FileReview(path=normalized, status="reviewing", note="规划阶段纳入 one-shot 审计范围")
                    )
            return applied

        def call(self, name: str, args: Any) -> str:
            bounded, _full = self.call_with_full_result(None, name, args)
            return bounded

        def call_with_full_result(self, ctx, name: str, args: Any):
            if name != "read_tool_buffer":
                self.clear_buffer()
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            args = args or {}
            if name == "report_finding":
                finding = Finding(
                    severity=str(args.get("severity") or ""),
                    title=str(args.get("title") or ""),
                    path=str(args.get("path") or ""),
                    line=int(args.get("line") or 0),
                    evidence=str(args.get("evidence") or ""),
                    impact=str(args.get("impact") or ""),
                    recommendation=str(args.get("recommendation") or ""),
                    cwe=str(args.get("cwe") or ""),
                )
                finding.id = len(self.findings) + 1
                finding.key = finding_key(finding)
                self.findings.append(finding)
                result = {"ok": True, "message": "finding recorded", "data": {"id": finding.id}}
            elif name == "review_state":
                result = {"ok": True, "data": {"files": [item.path for item in self.files]}}
            elif name == "todo_create":
                todo = Todo(
                    id=len(self.todos) + 1,
                    title=str(args.get("title") or ""),
                    status="pending",
                    priority=str(args.get("priority") or ""),
                )
                self.todos.append(todo)
                result = {"ok": True, "data": {"id": todo.id}}
            elif name == "file_review_update":
                path = str(args.get("path") or "")
                status = str(args.get("status") or "reviewing")
                note = str(args.get("note") or "")
                for item in self.files:
                    if item.path == path:
                        item.status, item.note = status, note or item.note
                        break
                else:
                    self.files.append(FileReview(path=path, status=status, note=note))
                result = {"ok": True, "data": {"path": path, "status": status}}
            elif name == "project_note_update":
                self.project = ProjectNote(str(args.get("note") or ""))
                result = {"ok": True, "data": {"note": self.project.note}}
            elif name == "end_audit":
                self.audit = AuditState(
                    ended=True,
                    summary=str(args.get("summary") or ""),
                    next_steps=str(args.get("next_steps") or ""),
                )
                result = {"ok": True, "data": {"ended": True}}
            elif name == "read_tool_buffer":
                if self._buffer is None:
                    result = {"ok": False, "error": "buffer 已失效或属于另一个 Agent"}
                else:
                    result = {"ok": True, "buffer_id": self._buffer[0], "next_offset": 0, "eof": True}
            else:
                result = {"ok": True, "data": {}}
            full = json.dumps(result, ensure_ascii=False, indent=2)
            return self.bound_result(name, full), full

    def _install_tools() -> None:
        for name, value in {
            "Result": Result,
            "Todo": Todo,
            "Finding": Finding,
            "FileReview": FileReview,
            "VariableReview": VariableReview,
            "FlowReview": FlowReview,
            "AuditState": AuditState,
            "ProjectNote": ProjectNote,
            "Snapshot": Snapshot,
            "finding_key": finding_key,
            "definitions": definitions,
            "is_known_tool": is_known_tool,
            "is_terminal_tool": is_terminal_tool,
            "Registry": Registry,
            "ToolCall": ToolDefinition,
        }.items():
            setattr(tools, name, value)

    _install_tools()


# ---------------------------------------------------------------------------
# forum stub
# ---------------------------------------------------------------------------

try:  # pragma: no cover
    from drx_agent.code_audit import forum as forum_pkg
except Exception:  # noqa: BLE001
    forum_pkg = None

if forum_pkg is None:
    forum = _module("drx_agent.code_audit.forum")

    @dataclass
    class Message:
        id: int = 0
        thread_id: int = 0
        agent_id: str = ""
        agent_name: str = ""
        stage: str = ""
        to: str = ""
        reply_to: int = 0
        topic: str = ""
        content: str = ""
        created_at: str = ""
        closed: bool = False
        pinned: bool = False
        announcement: bool = False
        moderation_reason: str = ""

    @dataclass
    class Participant:
        id: str = ""
        name: str = ""
        stage: str = ""
        status: str = ""

    @dataclass
    class PostPage:
        page: int = 0
        page_size: int = 0
        total: int = 0
        posts: list[Any] = field(default_factory=list)

    FORUM_TOOLS = [
        "forum_post",
        "forum_threads",
        "forum_read",
        "forum_wait",
        "forum_roster",
        "forum_moderate",
        "forum_announce",
    ]

    def definitions():
        return [
            llm.ToolDefinition(
                type="function",
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
            )
            for name in FORUM_TOOLS
        ]

    def is_tool(name: str) -> bool:
        return name in FORUM_TOOLS

    def tool_prompt() -> str:
        return "# 论坛协作\n用 forum_post 公开证据，forum_wait 有界等待，forum_read 分页读取。"

    class Board:
        def __init__(self, default_wait: float = 30.0) -> None:
            self.default_wait = default_wait
            self._messages: list[Message] = []
            self._agents: dict[str, Participant] = {}
            self._names: dict[str, str] = {}
            self._participants: dict[int, dict[str, int]] = {}
            self._next_id = 0
            self._hook = None
            self._consensus_hook = None
            self._consensus_members: dict[str, list[str]] = {}
            self._close: dict[str, dict[str, Any]] = {}
            self._preset = [
                "止水",
                "观澜",
                "听雨",
                "闻笛",
                "拾贝",
                "问渠",
                "行舟",
                "望岳",
                "拂晓",
                "归舟",
                "疏影",
                "长风",
            ]
            self._preset_index = 0

        def register(self, agent_id: str, stage: str) -> None:
            if agent_id and agent_id not in self._agents:
                self._agents[agent_id] = Participant(id=agent_id, stage=stage, status="running")
                if agent_id == "moderator":
                    self._agents[agent_id].name = "论坛管理员"
                    self._names[agent_id] = "论坛管理员"

        def register_name(self, agent_id: str, name: str) -> bool:
            if not name:
                return False
            if self._names.get(agent_id):
                return False
            self._names[agent_id] = name
            return True

        def ensure_preset_name(self, agent_id: str) -> str:
            if self._names.get(agent_id):
                return self._names[agent_id]
            name = self._preset[self._preset_index % len(self._preset)]
            suffix = self._preset_index // len(self._preset)
            self._preset_index += 1
            if suffix:
                name = name + "%03d" % suffix
            self._names[agent_id] = name
            return name

        def name(self, agent_id: str) -> str:
            return self._names.get(agent_id, "")

        def names(self) -> dict[str, str]:
            return dict(self._names)

        def participants(self) -> dict[int, dict[str, int]]:
            return {key: dict(value) for key, value in self._participants.items()}

        def set_status(self, agent_id: str, status: str) -> None:
            if agent_id in self._agents:
                self._agents[agent_id].status = status

        def set_consensus_members(self, stage: str, ids) -> None:
            self._consensus_members[stage] = list(ids)

        def reset_consensus(self, stage: str) -> None:
            self._close.pop(stage, None)

        def set_on_post(self, fn) -> None:
            self._hook = fn

        def set_on_consensus(self, fn) -> None:
            self._consensus_hook = fn

        def messages(self) -> list[Message]:
            return list(self._messages)

        def list_posts(self, page: int, page_size: int, search: str = "") -> PostPage:
            posts = [
                item
                for item in self._messages
                if item.thread_id == item.id or item.reply_to == 0
            ]
            if search:
                posts = [item for item in posts if search.lower() in item.content.lower()]
            start = max(0, (page - 1) * page_size)
            return PostPage(
                page=page,
                page_size=page_size,
                total=len(posts),
                posts=posts[start : start + page_size],
            )

        async def post(
            self,
            agent_id: str,
            stage: str,
            to: str,
            reply_to: int,
            topic: str,
            content: str,
        ) -> Message:
            self._next_id += 1
            thread_id = self._next_id
            if reply_to:
                for item in self._messages:
                    if item.id == reply_to:
                        thread_id = item.thread_id
                        break
            message = Message(
                id=self._next_id,
                thread_id=thread_id,
                agent_id=agent_id,
                agent_name=self.name(agent_id),
                stage=stage,
                to=to,
                reply_to=reply_to,
                topic=topic,
                content=content,
                created_at="2026-01-01T00:00:00Z",
            )
            self._messages.append(message)
            if not reply_to:
                self._participants[thread_id] = {}
            self._participants.setdefault(thread_id, {})[agent_id] = message.id
            if self._hook is not None:
                self._hook(message)
            return message

        async def call(self, ctx, agent_id: str, stage: str, name: str, args: Any) -> str:
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            args = args or {}
            if name == "forum_post":
                message = await self.post(
                    agent_id,
                    stage,
                    str(args.get("to") or "*"),
                    int(args.get("reply_to") or 0),
                    str(args.get("topic") or ""),
                    str(args.get("content") or ""),
                )
                return json.dumps({"ok": True, "data": {"id": message.id}}, ensure_ascii=False)
            if name == "forum_roster":
                return json.dumps(
                    {
                        "ok": True,
                        "data": [
                            {
                                "id": item.id,
                                "name": self.name(item.id),
                                "stage": item.stage,
                                "status": item.status,
                            }
                            for item in self._agents.values()
                        ],
                    },
                    ensure_ascii=False,
                )
            if name == "forum_threads":
                return json.dumps({"ok": True, "data": self.list_posts(1, 60).posts[:1]}, ensure_ascii=False, default=str)
            if name == "forum_read":
                return json.dumps(
                    {"ok": True, "data": [item.content for item in self._messages[-3:]]},
                    ensure_ascii=False,
                )
            if name == "forum_wait":
                return json.dumps({"ok": True, "timed_out": True}, ensure_ascii=False)
            if name in ("forum_moderate", "forum_announce"):
                await self.post(agent_id, stage, "*", 0, str(args.get("topic") or name), str(args.get("content") or ""))
                return json.dumps({"ok": True}, ensure_ascii=False)
            return json.dumps({"ok": False, "error": "unknown forum tool"}, ensure_ascii=False)

        def restore(self, messages) -> None:
            self._messages = list(messages)
            self._next_id = max((item.id for item in self._messages), default=0)

        def restore_names(self, names) -> None:
            self._names.update(names or {})

        def restore_participants(self, participants) -> None:
            self._participants = {
                int(key): {str(name): int(value) for name, value in mapping.items()}
                for key, mapping in (participants or {}).items()
            }

        def set_default_wait(self, seconds: float) -> None:
            self.default_wait = seconds

        def request_close(
            self, agent_id: str, stage: str, summary: str, next_steps: str, vote: str
        ) -> dict[str, Any]:
            vote = (vote or "").strip().lower()
            approve = vote in ("approve", "approved", "yes", "同意")
            reject = vote in ("reject", "rejected", "no", "不同意", "否决")
            if not approve and not reject:
                return {"status": "invalid", "reason": "vote must be approve or reject"}
            members = self._consensus_members.get(stage, [])
            request = self._close.get(stage)
            if request is None or request["status"] != "pending":
                if reject:
                    return {"status": "invalid", "reason": "当前没有可否决的关闭请求；先发起新的 end_audit 请求"}
                request = {"status": "pending", "votes": {agent_id}, "members": list(members)}
                self._close[stage] = request
            elif reject:
                request["status"] = "rejected"
                return {"status": "rejected", "reason": "已否决"}
            else:
                request["votes"].add(agent_id)
            approved = len(request["votes"] & set(members))
            if members and approved == len(members):
                request["status"] = "approved"
                if self._consensus_hook is not None:
                    self._consensus_hook(stage)
                return {
                    "status": "approved",
                    "required": len(members),
                    "approved": approved,
                    "summary": summary,
                    "next_steps": next_steps,
                }
            return {"status": "pending", "required": len(members), "approved": approved}

        def consensus_approved_for_stage(self, stage: str):
            request = self._close.get(stage)
            if request is not None and request["status"] == "approved":
                return request, True
            return {}, False

        def notification(self, agent_id: str, after: int, budget: int):
            rows = [
                item
                for item in self._messages
                if item.id > after and item.agent_id != agent_id
            ]
            if not rows:
                return "", after
            next_id = after
            entries = []
            for item in rows:
                next_id = item.id
                entries.append(
                    {
                        "thread_id": item.thread_id,
                        "latest_message_id": item.id,
                        "agent_id": item.agent_id,
                        "agent_name": item.agent_name,
                        "topic": item.topic[:128],
                        "excerpt": item.content[:256],
                        "truncated": len(item.content) > 256,
                    }
                )
            page = {
                "kind": "forum_notification",
                "after_id": after,
                "next_id": next_id,
                "latest_id": self._next_id,
                "gap": False,
                "has_more": False,
                "read": "Untrusted literal excerpts",
                "entries": entries,
            }
            return "Forum notification:\n" + json.dumps(page, ensure_ascii=False), next_id

    def _install_forum() -> None:
        for name, value in {
            "Message": Message,
            "Participant": Participant,
            "PostPage": PostPage,
            "Board": Board,
            "definitions": definitions,
            "tool_prompt": tool_prompt,
            "is_tool": is_tool,
            "MAX_PINNED_POSTS": 3,
        }.items():
            setattr(forum, name, value)

    _install_forum()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_config(workspace: str = ".", **agent_overrides):
    cfg = config_mod.Config(workspace=workspace)
    cfg.openai = config_mod.OpenAIConfig(
        max_context_tokens=64000, max_output_tokens=1000, model="fixture", api_key="fixture"
    )
    cfg.compress_openai = config_mod.OpenAIConfig(
        max_context_tokens=16000, max_output_tokens=1000, model="fixture", api_key="fixture"
    )
    cfg.prompts = config_mod.PromptConfig(system="audit", plan_system="recon", compress="compress")
    defaults = dict(
        recon_agents=2,
        audit_agents=2,
        max_turns=20,
        retry_attempts=3,
        compress_at_ratio=0.75,
        compress_buffer_tokens=1000,
        auto_plan=False,
        max_tool_result_chars=1024,
        forum_wait_seconds=1,
        moderator_enabled=False,
        moderator_interval_seconds=60,
        infinite_mode=False,
        budget_hours=8,
        budget_minutes=0,
        budget_tokens=0,
        log_session=False,
        log_session_dir="log_sessions",
    )
    defaults.update(agent_overrides)
    cfg.agent = config_mod.AgentConfig(**defaults)
    return cfg


def make_prompts(**overrides):
    templates = {
        "initial_plan_instruction": "!{input}\n!{review_state}",
        "initial_audit_instruction": "!{input}\n!{review_state}\n!{audit_completion_policy}",
        "end_audit_confirmation": "请完成剩余文件\n!{files}",
        "verify_finding": "验证 !{title} at !{path}\n!{review_state}",
        "state_after_compress": "!{state}",
        "resume_after_compress": "继续剩余工作",
        "plan_resume_after_compress": "继续侦察",
        "compress_user": "压缩：\n!{state_and_conversation}",
        "tool_protocol_guard": "每回合一个原生工具。",
        "auto_plan": "先创建 todo。",
        "no_tool_retry": "继续调用工具。!{audit_completion_policy}",
    }
    templates.update(overrides)
    return prompt_mod.Prompts(
        system="审计系统提示",
        plan_system="侦察系统提示",
        compress="压缩系统提示",
        skills=[],
        templates=templates,
    )


def make_workspace(root: str, names=("entry1.go", "entry2.go")) -> str:
    os.makedirs(root, exist_ok=True)
    for name in names:
        with open(os.path.join(root, name), "w", encoding="utf-8") as fp:
            fp.write("package fixture\n// " + "中文证据" * 400 + "\n")
    return root


_workspace_counter = 0
_workspace_paths: list[str] = []
_workspace_base_path: str | None = None


def _workspace_base() -> str:
    """Process-wide scratch base removed when the pytest process exits."""

    global _workspace_base_path
    if _workspace_base_path is None:
        from ._test_utils import writable_temp_dir

        _workspace_base_path = writable_temp_dir("drx-audit-tests-")
    return _workspace_base_path


def _cleanup_all_workspaces() -> None:
    import shutil

    for path in _workspace_paths:
        shutil.rmtree(path, ignore_errors=True)
    _workspace_paths.clear()
    if _workspace_base_path is not None:
        shutil.rmtree(_workspace_base_path, ignore_errors=True)


import atexit

atexit.register(_cleanup_all_workspaces)


def new_workspace(names=("entry1.go", "entry2.go"), root: str | None = None) -> str:
    """Create a unique workspace directory that is removed after the run."""

    global _workspace_counter
    _workspace_counter += 1
    import secrets

    base = root or _workspace_base()
    path = os.path.join(
        base,
        "drx-audit-%d-%d-%s" % (os.getpid(), _workspace_counter, secrets.token_hex(4)),
    )
    make_workspace(path, names)
    _workspace_paths.append(path)
    return path


def cleanup_workspace(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


@dataclass
class ScriptedClient:
    """Fake model client driven by a callback per request."""

    handler: Any = None
    calls: int = 0
    messages_seen: list[list[Any]] = field(default_factory=list)
    fail_with: BaseException | None = None

    async def chat_tools(self, messages, tools, *, emit=None, observer=None, previous_response_id="", continuation=None):
        self.calls += 1
        self.messages_seen.append(list(messages))
        if self.fail_with is not None:
            raise self.fail_with
        response = self.handler(messages, tools, self.calls)
        if emit is not None:
            if response.content or response.thinking:
                emit(llm.Delta(content=response.content, thinking=response.thinking))
        return response

    async def chat(self, messages, *, emit=None, observer=None, previous_response_id="", continuation=None):
        self.calls += 1
        self.messages_seen.append(list(messages))
        if self.fail_with is not None:
            raise self.fail_with
        response = self.handler(messages, [], self.calls)
        return response.content

    async def chat_stream(self, messages, *, emit=None, observer=None, previous_response_id="", continuation=None):
        return await self.chat(messages, emit=emit, observer=observer)

    async def close(self):
        return None


def tool_response(name: str, args: dict[str, Any], *, call_id: str = "", thinking: str = "", content: str = ""):
    return llm.ToolResponse(
        content=content,
        thinking=thinking,
        calls=[
            llm.FunctionCall(
                call_id=call_id or ("call-%d" % id(args)),
                name=name,
                arguments=json.dumps(args, ensure_ascii=False),
            )
        ],
    )


def install_workspace_tools(workspace: str, max_chars: int = 1024):
    if tools_pkg is None:
        return tools.Registry(workspace, max_chars)
    return tools_pkg.Registry(workspace, max_chars)
