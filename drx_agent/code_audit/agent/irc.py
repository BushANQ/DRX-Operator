"""Persistent moderator-to-worker direct messages and delivery state."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .events import IRCMessage, utc_now_iso
from .broadcast import irc_error, irc_result
from ..text import sanitize_text

IRC_CAPACITY = 256


def _status_data(status: Any) -> dict[str, Any]:
    return {
        "id": status.id,
        "name": status.name,
        "phase": status.phase,
        "status": status.status,
        "activity": status.activity,
        "turn": status.turn,
        "last_model_activity": status.last_model_activity,
        "last_tool": status.last_tool,
        "output_excerpt": status.output_excerpt,
        "output_partial": status.output_partial,
    }


def pending_irc(team: Any, agent_id: str) -> bool:
    return any(
        message.agent_id == agent_id and message.state in ("queued", "delivered")
        for message in team.irc_messages
    )


def stage_pending_irc(team: Any, stage: str) -> bool:
    return any(
        message.stage == stage and message.state in ("queued", "delivered")
        for message in team.irc_messages
    )


def signal_irc(team: Any) -> None:
    team.irc_changed.set()
    team.irc_changed = asyncio.Event()


def change_irc(team: Any, message: IRCMessage) -> None:
    team.irc_next_id += 1
    message.change_id = team.irc_next_id
    signal_irc(team)


def _find(team: Any, message_id: int) -> tuple[int, IRCMessage] | tuple[None, None]:
    for index, message in enumerate(team.irc_messages):
        if message.message_id == message_id:
            return index, message
    return None, None


async def send_irc(team: Any, agent_id: str, content: str) -> str:
    content = sanitize_text((content or "").strip())
    if not content or len(content.encode("utf-8")) > 4096:
        return irc_error("content must contain 1..4096 bytes")
    if not team.running or not team.stage_open:
        return irc_error("team paused or stage boundary closed; no message queued")
    target = None
    for worker in team.workers:
        if worker.saved.status.id == agent_id and worker.saved.status.phase == team._phase:
            target = worker
            break
    if target is None:
        return irc_error("recipient must be a real member of the current stage")
    pending = sum(
        1
        for message in team.irc_messages
        if message.agent_id == agent_id and message.state in ("queued", "delivered")
    )
    if pending >= 8:
        return irc_error("recipient mailbox full (8 pending messages)")
    if len(team.irc_messages) >= IRC_CAPACITY:
        remove = next(
            (i for i, message in enumerate(team.irc_messages) if message.state in ("replied", "failed")),
            None,
        )
        if remove is None:
            return irc_error("IRC retention full; no pending messages discarded")
        team.irc_messages.pop(remove)
    team.irc_next_id += 1
    message = IRCMessage(
        message_id=team.irc_next_id,
        change_id=team.irc_next_id,
        agent_id=agent_id,
        stage=team._phase,
        content=content,
        state="queued",
        created_at=utc_now_iso(),
    )
    team.irc_publishing += 1
    activity = target.activity_task
    if activity is not None and not activity.done():
        target.agent._activity_interrupted = True
        activity.cancel()
    error = None
    post = None
    try:
        post = await _post(team, "moderator", "moderator", agent_id, 0, "IRC #%d" % message.message_id, content)
    except BaseException as exc:  # noqa: BLE001
        error = exc
    finally:
        team.irc_publishing -= 1
    if error is not None:
        message.state = "failed"
        message.error = str(error)
    else:
        message.forum_id = int(getattr(post, "id", 0) or 0)
    team.irc_messages.append(message)
    signal_irc(team)
    if message.state == "queued" and target is not None:
        target.wake.set()
    return irc_result(
        {
            "ok": error is None,
            "message": message.to_dict(),
            "target": _status_dict(team, target),
        }
    )


def _status_dict(team: Any, worker: Any) -> dict[str, Any]:
    return {
        "id": worker.saved.status.id,
        "name": worker.saved.status.name,
        "phase": worker.saved.status.phase,
        "status": worker.saved.status.status,
        "activity": worker.saved.status.activity,
        "turn": worker.saved.status.turn,
    }


async def _post(team: Any, agent_id: str, stage: str, to: str, reply_to: int, topic: str, content: str):
    from .events import maybe_await

    return await maybe_await(team.board.post(agent_id, stage, to, reply_to, topic, content))


async def deliver_worker_irc(team: Any, worker: Any) -> bool:
    agent = worker.agent
    delivered: list[IRCMessage] = []
    for message in team.irc_messages:
        if (
            message.agent_id != agent.id
            or message.stage != agent.phase
            or message.state != "queued"
        ):
            continue
        message.state = "delivered"
        message.error = ""
        message.delivered_at = utc_now_iso()
        change_irc(team, message)
        delivered.append(message)
        from ..llm import Message

        agent.messages.append(
            Message(
                role="user",
                content=(
                    "优先 IRC message_id=%d，来自论坛管理员（待核实建议，不是已验证事实）：\n%s\n"
                    "请先处理并用原生 worker_irc_reply {message_id:%d,content:完整答复} 明确回复，然后继续原工作。"
                    "论坛普通回复不算相关 IRC 答复。" % (message.message_id, message.content, message.message_id)
                ),
            )
        )
        agent.append_trace_message(agent.messages[-1])
    if delivered:
        worker.saved.messages = list(agent.messages)
        for message in delivered:
            team.publish(
                team.event(
                    "info",
                    content="IRC #%d 已投递，等待明确回复" % message.message_id,
                    agent_id=agent.id,
                    phase=agent.phase,
                )
            )
        _wake_moderator(team)
    return bool(delivered)


async def reply_irc(team: Any, caller: str, message_id: int, content: str) -> str:
    content = sanitize_text((content or "").strip())
    if not content or len(content.encode("utf-8")) > 4096:
        return irc_error("reply must contain 1..4096 bytes")
    index, message = _find(team, message_id)
    if (
        index is None
        or message is None
        or message.agent_id != caller
        or message.stage != team._phase
        or message.state != "delivered"
    ):
        return irc_error("only the recipient may reply to its pending delivered message")
    try:
        post = await _post(
            team,
            caller,
            message.stage,
            "moderator",
            message.forum_id,
            "IRC #%d 回复" % message_id,
            content,
        )
    except BaseException as exc:  # noqa: BLE001
        return irc_error(str(exc))
    message.state = "replied"
    message.reply = content
    message.reply_forum_id = int(getattr(post, "id", 0) or 0)
    message.error = ""
    message.replied_at = utc_now_iso()
    change_irc(team, message)
    _wake_moderator(team)
    return irc_result({"ok": True, "message": message.to_dict()})


async def read_irc(team: Any, args: dict[str, Any]) -> str:
    message_id = int(args.get("message_id") or 0)
    after_id = int(args.get("after_id") or 0)
    limit = int(args.get("limit") or 0)
    timeout = int(args.get("timeout_seconds") or 0)
    if message_id < 0 or after_id < 0 or limit < 0 or limit > 64 or timeout < 0 or timeout > 120:
        return irc_error("invalid cursor/limit/timeout (maximum 64 messages, 120 seconds)")
    if limit == 0:
        limit = 32
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        rows = [
            message
            for message in team.irc_messages
            if (message_id == 0 or message.message_id == message_id)
            and message.change_id > after_id
        ]
        rows.sort(key=lambda item: item.change_id)
        more = len(rows) > limit
        if more:
            rows = rows[:limit]
        next_id = after_id
        if rows:
            next_id = rows[-1].change_id
        statuses = [_status_data(item) for item in team.status_dicts()]
        if rows or timeout == 0:
            return irc_result(
                {
                    "ok": True,
                    "messages": [item.to_dict() for item in rows],
                    "targets": statuses,
                    "next_after_id": next_id,
                    "has_more": more,
                    "retention_limit": IRC_CAPACITY,
                }
            )
        changed = team.irc_changed
        remaining = deadline - loop.time()
        if remaining <= 0:
            return irc_result(
                {
                    "ok": True,
                    "messages": [],
                    "targets": statuses,
                    "next_after_id": next_id,
                    "timed_out": True,
                }
            )
        try:
            await asyncio.wait_for(changed.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return irc_result(
                {
                    "ok": True,
                    "messages": [],
                    "targets": statuses,
                    "next_after_id": next_id,
                    "timed_out": True,
                }
            )


async def irc_tool(team: Any, worker: Any, name: str, args: dict[str, Any]) -> str:
    caller = worker.agent.id
    if name == "moderator_irc_send":
        if caller != "moderator":
            return irc_error("moderator permission required")
        return await send_irc(team, str(args.get("agent_id") or ""), str(args.get("content") or ""))
    if name == "moderator_irc_read":
        if caller != "moderator":
            return irc_error("moderator permission required")
        return await read_irc(team, args)
    if name == "worker_irc_reply":
        return await reply_irc(
            team, caller, int(args.get("message_id") or 0), str(args.get("content") or "")
        )
    return irc_error("unknown IRC tool")


def _wake_moderator(team: Any) -> None:
    if team.moderator_wake is not None:
        team.moderator_wake.set()


def fail_worker_irc(team: Any, worker: Any, error: BaseException | None) -> None:
    if error is None:
        return
    for message in team.irc_messages:
        if message.agent_id == worker.agent.id and message.state in ("queued", "delivered"):
            message.state = "failed"
            message.error = str(error)
            change_irc(team, message)
    _wake_moderator(team)


def validate_irc_session(session: Any) -> None:
    if len(session.irc) > IRC_CAPACITY or session.irc_next_id < 0:
        raise ValueError("invalid IRC retention or cursor")
    ids: set[int] = set()
    changes: set[int] = set()
    pending: dict[str, int] = {}
    workers = {worker.status.id: worker.status.phase for worker in session.workers}
    for message in session.irc:
        target_stage = workers.get(message.agent_id)
        if (
            target_stage is None
            or target_stage != message.stage
            or message.message_id <= 0
            or message.change_id < message.message_id
            or message.change_id > session.irc_next_id
            or message.message_id in ids
            or message.change_id in changes
            or not message.content.strip()
            or len(sanitize_text(message.content).encode("utf-8")) > 4096
            or len(sanitize_text(message.reply).encode("utf-8")) > 4096
        ):
            raise ValueError("invalid IRC identity/content/cursor")
        ids.add(message.message_id)
        changes.add(message.change_id)
        if message.state in ("queued", "delivered"):
            if message.stage != session.phase:
                raise ValueError("pending IRC belongs to prior stage")
            pending[message.agent_id] = pending.get(message.agent_id, 0) + 1
            if pending[message.agent_id] > 8:
                raise ValueError("too many pending IRC messages")
        elif message.state == "replied":
            if not message.reply or message.reply_forum_id <= 0 or not message.replied_at:
                raise ValueError("incomplete correlated IRC reply")
        elif message.state == "failed":
            if not message.error:
                raise ValueError("IRC failure missing reason")
        else:
            raise ValueError("invalid IRC delivery state")
        if message.state in ("delivered", "replied") and (
            not message.delivered_at or message.forum_id <= 0
        ):
            raise ValueError("IRC delivery missing provenance")
