"""Thread moderation, pin limits and restore validation."""

from __future__ import annotations

from .notifications import compact_json

MAX_PINNED_POSTS = 3
MAX_MODERATION_REASON = 512


def copy_moderation(dst, src) -> None:
    dst.closed = src.closed
    dst.pinned = src.pinned
    dst.announcement = src.announcement
    dst.moderation_reason = src.moderation_reason


def same_moderation(a, b) -> bool:
    return (
        a.closed == b.closed
        and a.pinned == b.pinned
        and a.announcement == b.announcement
        and a.moderation_reason == b.moderation_reason
    )


def pinned_count_locked(board) -> int:
    thread_ids: list[int] = []
    for message in board._msgs:
        if not message.pinned:
            continue
        if message.thread_id in thread_ids:
            continue
        if len(thread_ids) == MAX_PINNED_POSTS:
            return len(thread_ids)
        thread_ids.append(message.thread_id)
    return len(thread_ids)


def _failure(message: str) -> str:
    return compact_json({"ok": False, "error": message})


def validate_restored_messages(messages: list):
    normalized = []
    parents: dict[int, int] = {}
    states: dict[int, object] = {}
    previous = 0
    for original in messages:
        message = original.copy()
        if (
            message.id <= previous
            or not message.agent_id
            or not message.stage
            or not message.content.strip()
            or len(message.content.encode("utf-8")) > 16 * 1024
            or len(message.topic.encode("utf-8")) > 512
            or message.reply_to < 0
            or message.reply_to >= message.id
            or message.thread_id < 0
            or message.thread_id > message.id
        ):
            raise ValueError("无效的论坛会话记录")
        if message.agent_name:
            from .names import valid_name

            if not valid_name(message.agent_name):
                raise ValueError("无效的论坛作者名字")
        if (
            len(message.moderation_reason.encode("utf-8")) > MAX_MODERATION_REASON
            or (
                message.moderation_reason
                and not message.moderation_reason.strip()
            )
            or (message.closed and not message.moderation_reason)
        ):
            raise ValueError("无效的线程管理理由")
        parent = parents.get(message.reply_to, 0)
        if message.thread_id == 0:
            message.thread_id = message.id
            if message.reply_to > 0:
                message.thread_id = parent or message.reply_to
        if (message.reply_to == 0 and message.thread_id != message.id) or (
            message.reply_to > 0
            and (
                message.thread_id > message.reply_to
                or (parent != 0 and parent != message.thread_id)
            )
        ):
            raise ValueError("无效的论坛线程关系")
        state = states.get(message.thread_id)
        if state is not None and not same_moderation(state, message):
            raise ValueError("论坛线程管理状态不一致")
        if (
            message.id == message.thread_id
            and message.announcement
            and (message.agent_id != "moderator" or message.stage != "moderator")
        ):
            raise ValueError("公告作者不是论坛管理员")
        states[message.thread_id] = message
        parents[message.id] = message.thread_id
        normalized.append(message)
        previous = message.id
    pinned = sum(1 for state in states.values() if state.pinned)
    if pinned > MAX_PINNED_POSTS:
        raise ValueError("论坛会话置顶超过3帖；请先取消多余置顶")
    return normalized, previous


def moderator_call(board, agent_id: str, stage: str, name: str, args) -> str:
    if name == "forum_announce":
        if not isinstance(args, dict):
            return _failure("无效公告参数")
        topic = args.get("topic", "")
        content = args.get("content", "")
        pinned = args.get("pinned", False)
        if not isinstance(topic, str) or not isinstance(content, str) or not isinstance(pinned, bool):
            return _failure("无效公告参数")
        try:
            message = board._post(
                agent_id, stage, "*", 0, topic, content, announcement=True, pinned=pinned
            )
        except ValueError as exc:
            return _failure(str(exc))
        return compact_json({"ok": True, "message": message.to_dict()})

    if not isinstance(args, dict):
        return _failure("无效线程管理参数")
    thread_id = args.get("thread_id", 0)
    action = args.get("action", "")
    reason = args.get("reason", "")
    if isinstance(thread_id, bool) or not isinstance(thread_id, int) or thread_id <= 0:
        return _failure("无效线程管理参数")
    if not isinstance(action, str) or not isinstance(reason, str):
        return _failure("无效线程管理参数")
    reason = reason.strip()
    if not reason or len(reason.encode("utf-8")) > MAX_MODERATION_REASON:
        return _failure("reason 必须非空且不超过 512 字节")
    if action not in ("close", "reopen", "pin", "unpin"):
        return _failure("action 必须为 close/reopen/pin/unpin")

    with board.cond:
        state = None
        for message in board._msgs:
            if message.id == thread_id or message.thread_id == thread_id:
                state = message
                break
        if state is None:
            return _failure("线程不存在或已淘汰")
        if action == "pin" and not state.pinned and pinned_count_locked(board) >= MAX_PINNED_POSTS:
            return _failure("置顶数量已达3帖，请先 unpin 一帖再置顶；本次未修改")
        if action == "close":
            state.closed = True
        elif action == "reopen":
            state.closed = False
        elif action == "pin":
            state.pinned = True
        else:
            state.pinned = False
        state.moderation_reason = reason
        thread_id = state.thread_id
        for message in board._msgs:
            if message.thread_id != thread_id:
                continue
            board._bytes -= board.message_bytes(message)
            copy_moderation(message, state)
            board._bytes += board.message_bytes(message)
        board._trim_locked()
        board._signal_locked()

    try:
        log_message = board.post(
            agent_id,
            stage,
            "*",
            0,
            "论坛管理记录",
            "@全体成员 线程 #%d：%s。理由：%s" % (thread_id, action, reason),
        )
    except ValueError as exc:
        return _failure("管理动作已应用，但记录失败：%s" % exc)
    return compact_json(
        {
            "ok": True,
            "thread_id": thread_id,
            "closed": state.closed,
            "pinned": state.pinned,
            "announcement": state.announcement,
            "moderation_reason": state.moderation_reason,
            "log_message_id": log_message.id,
        }
    )
