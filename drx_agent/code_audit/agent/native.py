"""Native tool declarations, role permissions and history validation."""

from __future__ import annotations

import json
from typing import Any

from .. import tools as tools_pkg
from ..forum import is_tool as forum_is_tool
from .context import add_message, estimate_text_tokens
from .events import phase_audit, phase_moderator, phase_recon

# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def _definition(name: str, description: str, schema: dict[str, Any], *, strict: bool = False):
    from ..llm import ToolDefinition

    kwargs = {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": schema,
        "strict": strict,
    }
    try:
        return ToolDefinition(**kwargs)
    except TypeError:  # pragma: no cover - older client signature
        kwargs.pop("strict")
        return ToolDefinition(**kwargs)


def special_tool_definitions() -> list[Any]:
    return [
        _definition(
            "read_handoff",
            "读取前一阶段完整结构化交接；超长结果按工具 buffer 分页。",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _definition(
            "load_skill",
            "按名称加载本次任务需要的可用技能。",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "audit_plan_done",
            "提交侦察交接并结束当前侦察 worker。audit_files 必须是实际文件路径。",
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "audit_map": {"type": "string"},
                    "audit_files": {"type": "array", "items": {"type": "string"}},
                    "execute_instructions": {"type": "string"},
                },
                "required": ["summary", "audit_map", "audit_files"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "end_audit",
            "请求团队关闭并明确投票；待定、拒绝或超时后继续自己的审计，选择其他未覆盖代码建立待办并读取源码。不催票、不重复关闭、不围绕同伴结论重复复核。",
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "next_steps": {"type": "string"},
                    "vote": {"type": "string", "enum": ["approve", "reject"]},
                },
                "required": ["summary", "vote"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "verify_finding",
            "启动独立验证 Agent 复核候选漏洞，不直接登记漏洞。",
            {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "title": {"type": "string"},
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "evidence": {"type": "string"},
                    "impact": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "cwe": {"type": "string"},
                },
                "required": ["title", "path", "evidence"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "moderator_idle",
            "结束本次管理员激活，等待新活动。",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _definition(
            "moderator_review_state",
            "读取成员与漏洞快照、稳定 finding_key。",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _definition(
            "moderator_revoke_finding",
            "依据源码反证撤销漏洞，保留原记录。",
            {
                "type": "object",
                "properties": {
                    "finding_key": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["finding_key", "reason", "evidence"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "moderator_assign",
            "审计阶段建议后续复核；侦察阶段仅记录待审计建议随交接传递，不重开侦察。",
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["agent_id", "content"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "moderator_irc_send",
            "中断当前阶段成员的模型请求或工具等待，安全边界优先投递。queued/delivered 不代表已回答。",
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "content": {"type": "string", "maxLength": 4096},
                },
                "required": ["agent_id", "content"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "moderator_irc_read",
            "读取持久 IRC 状态及相关回复、目标实时不可变进度；after_id 是变更游标，支持最多120秒等待；长结果按 buffer 分页。",
            {
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer"},
                    "after_id": {"type": "integer"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                    "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 120},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            "worker_irc_reply",
            "明确回复已经投递给自己的 IRC 消息 ID；公开保存并通知管理员。不替代阶段完成或投票。",
            {
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer"},
                    "content": {"type": "string", "maxLength": 4096},
                },
                "required": ["message_id", "content"],
                "additionalProperties": False,
            },
        ),
        _definition(
            "moderator_decide",
            "仅对 audit 阶段结束审查作出批准或继续工作决定；侦察不需要批准。",
            {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "enum": ["audit"]},
                    "decision": {"type": "string", "enum": ["approve", "request_work"]},
                    "reason": {"type": "string"},
                    "assignments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_id": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["agent_id", "content"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["stage", "decision", "reason"],
                "additionalProperties": False,
            },
        ),
    ]


def _coerce_definition(item: Any):
    if hasattr(item, "name"):
        return item
    from ..llm import ToolDefinition

    data = dict(item)
    return ToolDefinition(
        type=data.get("type", "function"),
        name=data.get("name", ""),
        description=data.get("description", ""),
        parameters=data.get("parameters", {}),
        strict=bool(data.get("strict", False)),
    )


def tool_definitions(agent: Any) -> list[Any]:
    if agent.tools_disabled:
        return []
    definitions = [
        _coerce_definition(item)
        for item in list(tools_pkg.definitions())
        + list(_forum_definitions())
        + special_tool_definitions()
    ]
    return [item for item in definitions if tool_permission(agent, item.name) == ""]


def registry_buffer(agent: Any):
    return getattr(getattr(agent, "tools", None), "_buffer", None)


def clear_registry_buffer(agent: Any) -> None:
    buffer = registry_buffer(agent)
    if buffer is not None and hasattr(buffer, "clear"):
        buffer.clear()


def bound_registry_result(agent: Any, name: str, full: str) -> str:
    buffer = registry_buffer(agent)
    if buffer is not None and hasattr(buffer, "bound_result"):
        return buffer.bound_result(name, full)
    limit = int(getattr(agent.cfg.agent, "max_tool_result_chars", 12000) or 12000)
    from ..text import sanitize_text

    if len(sanitize_text(full).encode("utf-8")) <= limit:
        return full
    return json.dumps({"ok": True, "truncated": True, "preview": full[: limit // 2]}, ensure_ascii=False)


def _forum_definitions() -> list[Any]:
    from .. import forum as forum_pkg

    return list(forum_pkg.definitions())


def tool_definition_tokens(agent: Any) -> int:
    definitions = tool_definitions(agent)
    if not definitions:
        return 0
    payload = [
        {
            "type": getattr(item, "type", "function"),
            "name": getattr(item, "name", ""),
            "description": getattr(item, "description", ""),
            "parameters": getattr(item, "parameters", {}),
            "strict": bool(getattr(item, "strict", False)),
        }
        for item in definitions
    ]
    return estimate_text_tokens(json.dumps(payload, ensure_ascii=False)) + 8


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

MODERATOR_TOOLS = {
    "moderator_irc_send",
    "moderator_irc_read",
    "moderator_idle",
    "moderator_decide",
    "moderator_assign",
    "moderator_review_state",
    "moderator_revoke_finding",
    "read_file",
    "list_files",
    "search_content",
    "search_context",
    "read_tool_buffer",
    "read_handoff",
    "review_state",
    "load_skill",
}

VERIFIER_TOOLS = {
    "read_file",
    "list_files",
    "search_content",
    "search_context",
    "read_tool_buffer",
    "read_handoff",
    "review_state",
    "load_skill",
    "forum_post",
    "forum_read",
    "forum_threads",
    "forum_wait",
    "forum_roster",
}

IRC_ONLY_TOOLS = {
    "worker_irc_reply",
    "read_file",
    "list_files",
    "search_content",
    "search_context",
    "read_tool_buffer",
    "read_handoff",
    "review_state",
    "forum_read",
    "forum_threads",
    "forum_roster",
    "load_skill",
}

RECON_TOOLS = {
    "review_state",
    "list_files",
    "read_file",
    "search_content",
    "search_context",
    "git_inspect",
    "todo_create",
    "todo_update",
    "file_review_update",
    "project_note_update",
    "audit_plan_done",
    "load_skill",
}


def moderator_allowed_tool(name: str) -> bool:
    return forum_is_tool(name) or name in MODERATOR_TOOLS


def tool_permission(agent: Any, name: str) -> str:
    if agent.cfg.agent.infinite_mode and name in ("end_audit", "moderator_decide"):
        return "无限模式没有结束审计工具；继续审计，停止由用户或预算控制。"
    if agent.tools_disabled:
        return "工具调用已禁用；请根据已有证据直接给出结论。"
    if name == "worker_irc_reply" and (
        agent.phase == phase_moderator or agent.verifying or agent.irc_tool is None
    ):
        return "只有收到已投递 IRC 的原始成员可回复。"
    if agent.irc_only:
        if name not in IRC_ONLY_TOOLS:
            return "已完成成员仅被唤醒回答 IRC；不能重写阶段状态或重复投票，后续工作由管理员明确重开。"
    if (name.startswith("moderator_") or name in ("forum_moderate", "forum_announce")) and agent.phase != phase_moderator:
        return "此工具仅允许论坛管理员调用。"
    if agent.verifying:
        if name not in VERIFIER_TOOLS:
            return "验证 Agent 只能读取源码与状态、加载技能和参与论坛，不能更改审计状态、提交漏洞或结束阶段。"
    if name == "load_skill" and not agent.prompts.skills:
        return "没有可加载的技能。"
    if forum_is_tool(name) and agent.board is None:
        return "论坛不可用。"
    return phase_tool_correction(agent, name)


def phase_tool_correction(agent: Any, name: str) -> str:
    if agent.phase == phase_moderator:
        if moderator_allowed_tool(name):
            return ""
        return "管理员只能读取源码、审查论坛并调用管理员工具；不能替普通 Agent 提交漏洞或投票结束阶段。"
    if forum_is_tool(name) or name in ("read_tool_buffer", "read_handoff", "worker_irc_reply"):
        return ""
    if agent.phase == phase_recon:
        if name in RECON_TOOLS:
            return ""
        return (
            "当前处于规划建图阶段，禁止调用 %s。继续建图并通过原生 audit_plan_done 提交侦察交接。" % name
        )
    if name == "audit_plan_done":
        return "当前已经处于执行审计阶段，不能再次调用 audit_plan_done。请从当前原生工具定义中选择。"
    return ""


# ---------------------------------------------------------------------------
# Validation and history
# ---------------------------------------------------------------------------

def validate_native_response(response: Any, history: list[Any]) -> None:
    calls = list(getattr(response, "calls", None) or [])
    if len(calls) > 1 and not getattr(response, "multi_call_fallback", False):
        raise ValueError("模型返回 %d 个原生工具调用；每回合只允许一个，整批未执行" % len(calls))
    for call in calls:
        arguments = (getattr(call, "arguments", "") or "").strip()
        name = (getattr(call, "name", "") or "").strip()
        call_id = (getattr(call, "call_id", "") or "").strip()
        if not call_id or not name:
            raise ValueError("原生工具调用缺少 call_id/name 或完整 JSON 对象参数；未执行")
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError("原生工具调用缺少 call_id/name 或完整 JSON 对象参数；未执行") from exc
        if not isinstance(parsed, dict) or not arguments.startswith("{"):
            raise ValueError("原生工具调用缺少 call_id/name 或完整 JSON 对象参数；未执行")
        for message in history or []:
            if getattr(message, "type", "") == "function_call" and getattr(message, "call_id", "") == call_id:
                raise ValueError('原生工具调用重复 call_id "%s"；未执行' % call_id)


def validate_native_history(messages: list[Any]) -> None:
    pending: dict[str, bool] = {}
    seen: set[str] = set()
    for message in messages or []:
        kind = getattr(message, "type", "") or ""
        if kind == "function_call":
            call_id = getattr(message, "call_id", "") or ""
            if call_id in seen:
                raise ValueError("native history contains duplicate or unpaired function call")
            arguments = getattr(message, "arguments", "") or ""
            if not call_id or not (getattr(message, "name", "") or ""):
                raise ValueError("invalid native history call")
            try:
                parsed = json.loads(arguments)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid native history call") from exc
            if not isinstance(parsed, dict):
                raise ValueError("invalid native history call")
            pending[call_id] = True
            seen.add(call_id)
        elif kind == "function_call_output":
            if getattr(message, "call_id", "") not in pending:
                raise ValueError("native history contains orphan function output")
            pending.pop(getattr(message, "call_id", ""), None)
        else:
            if pending:
                raise ValueError("native history interrupts a pending function call")
    if pending:
        raise ValueError("native history ends with an unpaired function call")


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def join_assistant_message(thinking: str, content: str) -> str:
    if not thinking:
        return content
    return "<think>" + thinking + "</think>" + content


def record_response(agent: Any, response: Any) -> tuple[Any, bool]:
    from ..llm import Message

    text = join_assistant_message(
        getattr(response, "thinking", "") or "", getattr(response, "content", "") or ""
    )
    if text:
        add_message(agent, Message(role="assistant", content=text))
    calls = list(getattr(response, "calls", None) or [])
    if not calls:
        return None, False
    for item in calls:
        add_message(
            agent,
            Message(
                type="function_call",
                id=getattr(item, "id", "") or "",
                call_id=getattr(item, "call_id", "") or "",
                name=getattr(item, "name", "") or "",
                arguments=getattr(item, "arguments", "") or "",
            ),
        )
    return calls[0], True


def record_tool_output(agent: Any, call: Any, result: str, full: str) -> None:
    from ..llm import Message

    call_id = getattr(call, "call_id", "") or ""
    agent.messages.append(
        Message(type="function_call_output", call_id=call_id, content=result)
    )
    agent.append_trace_message(
        Message(type="function_call_output", call_id=call_id, content=full)
    )


def record_skipped_calls(agent: Any, calls: list[Any], reason: str = "未执行：每回合只允许一个工具调用，请下一回合重新发起。") -> None:
    """Close native history for calls that were not executed this turn."""
    for skipped in list(calls)[1:]:
        payload = json.dumps({"ok": False, "error": reason}, ensure_ascii=False)
        record_tool_output(agent, skipped, payload, payload)


def reject_tool(agent: Any, call: Any, reason: str) -> None:
    result = json.dumps({"ok": False, "error": reason}, ensure_ascii=False, indent=2)
    bounded = bound_registry_result(agent, getattr(call, "name", ""), result)
    record_tool_output(agent, call, bounded, result)


def audit_plan_accepted(result: str) -> bool:
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return False
    return bool(parsed.get("ok"))


async def execute_native_tool(agent: Any, emit, call: Any, ctx: Any = None) -> str:
    if call is None:
        return ""
    name = getattr(call, "name", "")
    if name != "read_tool_buffer":
        clear_registry_buffer(agent)
    allowed = any(item.name == name for item in tool_definitions(agent))
    if not allowed:
        reason = tool_permission(agent, name)
        if not reason:
            reason = "不存在或当前角色不可用的工具：" + name + "。请从当前原生工具定义中选择。"
        reject_tool(agent, call, reason)
        return ""
    if name == "end_audit":
        blocker = agent.end_audit_needs_confirmation()
        if blocker:
            reject_tool(agent, call, blocker)
            return ""
    else:
        agent.pending_end_audit = False
    from .events import Event

    emit(Event(kind="tool", content="calling " + name))
    result, full = await agent.call_tool(emit, call)
    record_tool_output(agent, call, result, full)
    return full


def _ctx_error(ctx: Any):
    return getattr(ctx, "error", None) if ctx is not None else None
