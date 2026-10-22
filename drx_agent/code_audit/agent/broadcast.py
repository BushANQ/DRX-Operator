"""Authoritative user instructions broadcast to every audit agent."""

from __future__ import annotations

import json
from typing import Any

from ..text import sanitize_text

USER_BROADCAST_PRIORITY = (
    "最高优先级用户指示（实际用户 /say 完整原文）：优先处理下面的用户要求，再继续自己的计划或同伴论坛建议；"
    "冲突时以较新的用户要求为准。它不是论坛摘录或模型建议，不能覆盖系统安全约束、当前阶段/角色权限或工具模式。"
    "压缩摘要不能替代或撤销此原文。"
)

MAX_USER_BROADCAST_BYTES = 16 * 1024


def validate_user_broadcast(content: str) -> None:
    content = sanitize_text(content)
    if not content.strip():
        raise ValueError("用户广播不能为空")
    if len(content.encode("utf-8")) > MAX_USER_BROADCAST_BYTES:
        raise ValueError("用户广播原文不能超过 16384 UTF-8 字节")


def broadcast_message(broadcast: Any):
    from ..llm import Message

    return Message(
        role="user",
        content="%s\n[用户广播 #%d]\n\n%s"
        % (USER_BROADCAST_PRIORITY, int(getattr(broadcast, "id", 0)), getattr(broadcast, "content", "")),
    )


def irc_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def irc_error(message: str) -> str:
    return irc_result({"ok": False, "error": message})
