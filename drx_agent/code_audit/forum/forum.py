"""In-memory collaboration forum: retained posts, cursors, moderation and votes."""

from __future__ import annotations

import json
import threading
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from . import consensus as consensus_module
from . import moderation as moderation_module
from . import names as names_module
from . import notifications as notifications_module
from . import participants as participants_module
from . import posts as posts_module
from .definitions import FORUM_TOOL_PROMPT, definitions
from .notifications import excerpt, excerpt_from, compact_json

MAX_CONTENT = 16 * 1024
MAX_TOPIC = 512
MAX_MESSAGES = 4096
MAX_RETAINED_BYTES = 8 * 1024 * 1024
MAX_PAGE = 64
MAX_PAGE_BYTES = 4096
MAX_WAIT = 120.0

MAX_PINNED_POSTS = moderation_module.MAX_PINNED_POSTS

FORUM_TOOLS = (
    "forum_post",
    "forum_threads",
    "forum_read",
    "forum_wait",
    "forum_roster",
    "forum_moderate",
    "forum_announce",
)


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

    def copy(self) -> "Message":
        return Message(
            id=self.id,
            thread_id=self.thread_id,
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            stage=self.stage,
            to=self.to,
            reply_to=self.reply_to,
            topic=self.topic,
            content=self.content,
            created_at=self.created_at,
            closed=self.closed,
            pinned=self.pinned,
            announcement=self.announcement,
            moderation_reason=self.moderation_reason,
        )

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
        }
        if self.agent_name:
            data["agent_name"] = self.agent_name
        data["stage"] = self.stage
        data["to"] = self.to
        data["reply_to"] = self.reply_to
        data["topic"] = self.topic
        data["content"] = self.content
        data["created_at"] = self.created_at
        if self.closed:
            data["closed"] = True
        if self.pinned:
            data["pinned"] = True
        if self.announcement:
            data["announcement"] = True
        if self.moderation_reason:
            data["moderation_reason"] = self.moderation_reason
        return data


@dataclass
class Participant:
    id: str
    name: str = ""
    stage: str = ""
    status: str = "running"

    def to_dict(self) -> dict:
        data = {"id": self.id}
        if self.name:
            data["name"] = self.name
        data["stage"] = self.stage
        data["status"] = self.status
        return data


def is_tool(name: str) -> bool:
    return name in FORUM_TOOLS


def tool_prompt() -> str:
    return FORUM_TOOL_PROMPT


def _now_iso() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def message_bytes(message: Message) -> int:
    return (
        len(message.content.encode("utf-8"))
        + len(message.topic.encode("utf-8"))
        + len(message.agent_id.encode("utf-8"))
        + len(message.agent_name.encode("utf-8"))
        + len(message.stage.encode("utf-8"))
        + len(message.to.encode("utf-8"))
        + len(message.moderation_reason.encode("utf-8"))
        + 128
    )


def _failure(message: str) -> str:
    return compact_json({"ok": False, "error": message})


def _ctx_cancelled(ctx) -> bool:
    if ctx is None:
        return False
    for attribute in ("is_cancelled", "cancelled"):
        value = getattr(ctx, attribute, None)
        if callable(value):
            try:
                return bool(value())
            except TypeError:
                continue
        if isinstance(value, bool):
            return value
    value = getattr(ctx, "is_set", None)
    if callable(value):
        try:
            return bool(value())
        except TypeError:
            return False
    return False


def _as_int(value) -> int:
    if isinstance(value, bool):
        raise ValueError("bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError("int")


def _as_float(value) -> float:
    if isinstance(value, bool):
        raise ValueError("bool")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError("float")


class Board:
    def __init__(self, default_wait: float = 30.0) -> None:
        if default_wait <= 0:
            default_wait = 30.0
        if default_wait > MAX_WAIT:
            default_wait = MAX_WAIT
        self._cond = threading.Condition(threading.RLock())
        self._default_wait = float(default_wait)
        self._next_id = 0
        self._msgs: list[Message] = []
        self._bytes = 0
        self._agents: dict[str, Participant] = {}
        self._participants: dict[int, dict[str, int]] = {}
        self._thread_counts: dict[int, int] = {}
        self._hook = None
        self._consensus: dict[str, dict] = {}
        self._consensus_members: dict[str, set[str]] = {}
        self._on_consensus = None
        self._next_consensus_id = 0

    # -- lifecycle -----------------------------------------------------

    @property
    def cond(self):
        return self._cond

    def set_default_wait(self, seconds: float) -> None:
        value = float(seconds)
        if value <= 0:
            value = 30.0
        if value > MAX_WAIT:
            value = MAX_WAIT
        with self._cond:
            self._default_wait = value

    def register(self, agent_id: str, stage: str) -> None:
        if not agent_id:
            return
        with self._cond:
            if agent_id in self._agents:
                return
            participant = Participant(id=agent_id, stage=stage, status="running")
            if agent_id == "moderator" and stage == "moderator":
                participant.name = "论坛管理员"
            self._agents[agent_id] = participant
            self._signal_locked()

    def set_status(self, agent_id: str, status: str) -> None:
        with self._cond:
            participant = self._agents.get(agent_id)
            if participant is None:
                return
            participant.status = status
            self._signal_locked()

    def set_on_post(self, callback) -> None:
        with self._cond:
            self._hook = callback

    def set_on_consensus(self, callback) -> None:
        with self._cond:
            self._on_consensus = callback

    def _signal_locked(self) -> None:
        self._cond.notify_all()

    def message_bytes(self, message: Message) -> int:
        return message_bytes(message)

    # -- posting -------------------------------------------------------

    def post(self, agent_id: str, stage: str, to: str, reply_to: int, topic: str, content: str) -> Message:
        return self._post(agent_id, stage, to, reply_to, topic, content, False, False)

    def _post(
        self,
        agent_id: str,
        stage: str,
        to: str,
        reply_to: int,
        topic: str,
        content: str,
        announcement: bool = False,
        pinned: bool = False,
    ) -> Message:
        if not content.strip() or len(content.encode("utf-8")) > MAX_CONTENT:
            raise ValueError("content 必须非空且不超过 %d 字节" % MAX_CONTENT)
        if len(topic.encode("utf-8")) > MAX_TOPIC:
            raise ValueError("topic 超过 %d 字节" % MAX_TOPIC)
        with self._cond:
            participant = self._agents.get(agent_id)
            if participant is None or (stage and stage != participant.stage):
                raise ValueError("未注册的 Agent 或阶段不匹配")
            if (
                pinned
                and reply_to == 0
                and moderation_module.pinned_count_locked(self) >= MAX_PINNED_POSTS
            ):
                raise ValueError("置顶数量已达3帖，请先取消一帖置顶")
            if to and to != "*" and to not in self._agents:
                raise ValueError("未知接收 Agent")
            if reply_to < 0 or (reply_to > 0 and not self._has_id_locked(reply_to)):
                raise ValueError("回复目标不存在或已淘汰")
            thread_id = self._next_id + 1
            parent = None
            if reply_to > 0:
                index = bisect_left([m.id for m in self._msgs], reply_to)
                parent = self._msgs[index]
                if parent.closed:
                    raise ValueError("线程已关闭，禁止回复；请阅读历史或另发有新证据的帖子")
                thread_id = parent.thread_id
            self._next_id += 1
            message = Message(
                id=self._next_id,
                thread_id=thread_id,
                agent_id=agent_id,
                agent_name=participant.name,
                stage=participant.stage,
                to=to,
                reply_to=reply_to,
                topic=topic,
                content=content,
                created_at=_now_iso(),
                announcement=announcement,
                pinned=pinned,
            )
            if parent is not None:
                moderation_module.copy_moderation(message, parent)
            self._msgs.append(message)
            participants_module.join_thread(self, message)
            self._thread_counts[message.thread_id] = (
                self._thread_counts.get(message.thread_id, 0) + 1
            )
            self._bytes += message_bytes(message)
            self._trim_locked()
            hook = self._hook
            self._signal_locked()
        if hook is not None:
            hook(message.copy())
        return message

    def _has_id_locked(self, message_id: int) -> bool:
        index = bisect_left([m.id for m in self._msgs], message_id)
        return index < len(self._msgs) and self._msgs[index].id == message_id

    def _trim_locked(self) -> None:
        while len(self._msgs) > MAX_MESSAGES or self._bytes > MAX_RETAINED_BYTES:
            thread = self._msgs[0].thread_id
            self._thread_counts[thread] = self._thread_counts.get(thread, 0) - 1
            if self._thread_counts[thread] <= 0:
                self._thread_counts.pop(thread, None)
                self._participants.pop(thread, None)
            self._bytes -= message_bytes(self._msgs[0])
            self._msgs.pop(0)

    # -- snapshots -----------------------------------------------------

    def messages(self) -> list[Message]:
        with self._cond:
            return [message.copy() for message in self._msgs]

    def restore(self, messages: list) -> None:
        normalized, previous = moderation_module.validate_restored_messages(list(messages))
        with self._cond:
            self._msgs = []
            self._bytes = 0
            self._participants = {}
            self._thread_counts = {}
            for message in normalized:
                self._msgs.append(message)
                participants_module.join_thread(self, message)
                self._thread_counts[message.thread_id] = (
                    self._thread_counts.get(message.thread_id, 0) + 1
                )
                self._bytes += message_bytes(message)
                self._trim_locked()
            if previous > self._next_id:
                self._next_id = previous
            self._signal_locked()

    def checkpoint(self):
        return participants_module.checkpoint(self)

    def names(self) -> dict[str, str]:
        with self._cond:
            return {
                agent_id: participant.name
                for agent_id, participant in self._agents.items()
                if participant.name
            }

    def participants(self) -> dict[int, dict[str, int]]:
        with self._cond:
            return {thread: dict(members) for thread, members in self._participants.items()}

    # -- names ---------------------------------------------------------

    def register_name(self, agent_id: str, name: str) -> bool:
        try:
            names_module.register_name(self, agent_id, name)
            return True
        except ValueError:
            return False

    def restore_names(self, names: dict[str, str]) -> None:
        names_module.restore_names(self, names)

    def ensure_preset_name(self, agent_id: str) -> str:
        return names_module.ensure_preset_name(self, agent_id)

    def name(self, agent_id: str) -> str:
        return names_module.name(self, agent_id)

    def restore_participants(self, saved: dict[int, dict[str, int]]) -> None:
        participants_module.restore_participants(self, saved)

    def notification(self, agent_id: str, after: int, budget: int) -> tuple[str, int]:
        return notifications_module.notification(self, agent_id, after, budget)

    # -- consensus -----------------------------------------------------

    def set_consensus_members(self, stage: str, ids) -> None:
        consensus_module.set_consensus_members(self, stage, ids)

    def reset_consensus(self, stage: str) -> None:
        consensus_module.reset_consensus(self, stage)

    def request_close(self, agent_id: str, stage: str, summary: str, next_steps: str, vote: str):
        return consensus_module.request_close(self, agent_id, stage, summary, next_steps, vote)

    def request_close_at(self, agent_id, stage, summary, next_steps, vote, now):
        return consensus_module.request_close_at(
            self, agent_id, stage, summary, next_steps, vote, now
        )

    def consensus_approved(self, agent_id: str, stage: str):
        return consensus_module.consensus_approved(self, agent_id, stage)

    def consensus_approved_for_stage(self, stage: str):
        return consensus_module.consensus_approved_for_stage(self, stage)

    # -- reading -------------------------------------------------------

    def _matches_read(self, message: Message, agent_id: str, query: dict, thread: dict) -> bool:
        in_thread = (
            query["thread_id"] == 0
            or message.thread_id == query["thread_id"]
            or thread.get(message.id)
            or thread.get(message.reply_to)
        )
        if query["thread_id"] > 0 and in_thread:
            thread[message.id] = True
        visible_from = not query["from"] or message.agent_id == query["from"]
        visible = (
            query["all"]
            or message.agent_id == agent_id
            or message.to == ""
            or message.to == "*"
            or message.to == agent_id
        )
        return bool(in_thread and visible_from and visible)

    def _read_locked(self, agent_id: str, query: dict) -> dict:
        page = {
            "ok": True,
            "messages": [],
            "next_id": query["after_id"],
            "latest_id": self._next_id,
            "oldest_id": 0,
            "gap": False,
            "has_more": False,
            "timed_out": False,
            "cancelled": False,
            "peers_done": False,
        }
        if self._msgs:
            page["oldest_id"] = self._msgs[0].id
            page["gap"] = query["after_id"] < page["oldest_id"] - 1
        thread: dict[int, bool] = {}
        if query["thread_id"] > 0:
            thread[query["thread_id"]] = True
            if not self._has_id_locked(query["thread_id"]):
                page["gap"] = True
        for message in self._msgs:
            if not self._matches_read(message, agent_id, query, thread):
                continue
            if message.id <= query["after_id"]:
                continue
            projection = message.to_dict()
            total_bytes = len(message.content.encode("utf-8"))
            content = excerpt(message.content, 512)
            projection["content"] = content
            projection["total_bytes"] = total_bytes
            projection["next_offset"] = len(content.encode("utf-8"))
            projection["has_more"] = projection["next_offset"] < total_bytes
            if len(page["messages"]) >= query["limit"]:
                page["has_more"] = True
                break
            page["messages"].append(projection)
            if len(page["messages"]) > 1 and len(compact_json(page).encode("utf-8")) > MAX_PAGE_BYTES - 64:
                page["messages"].pop()
                page["has_more"] = True
                break
            page["next_id"] = message.id
        if not page["has_more"] and query["after_id"] <= self._next_id:
            page["next_id"] = self._next_id
        return page

    def _read_body_locked(self, agent_id: str, query: dict) -> str:
        thread: dict[int, bool] = {query["thread_id"]: True}
        message_id = query["message_id"]
        for message in self._msgs:
            visible = self._matches_read(message, agent_id, query, thread)
            if message.id != message_id or not visible:
                continue
            data = message.content.encode("utf-8")
            offset = query["offset"]
            if offset > len(data) or (offset < len(data) and (data[offset] & 0xC0) == 0x80):
                return _failure(
                    "offset must be a valid UTF-8 byte boundary within the retained message"
                )
            body = {
                "ok": True,
                "message": message.to_dict(),
                "offset": offset,
                "next_offset": offset,
                "total_bytes": len(data),
                "has_more": False,
            }
            content = excerpt_from(message.content, offset, query["max_bytes"])
            body["message"]["content"] = content
            while True:
                body["message"]["content"] = content
                body["next_offset"] = offset + len(content.encode("utf-8"))
                body["has_more"] = body["next_offset"] < body["total_bytes"]
                if body["has_more"] and body["next_offset"] == offset:
                    return _failure(
                        "max_bytes or JSON page budget is too small for the next UTF-8 character"
                    )
                if len(compact_json(body).encode("utf-8")) <= MAX_PAGE_BYTES:
                    return compact_json(body)
                if not content:
                    return _failure("forum message metadata exceeds read page budget")
                content = excerpt(content, len(content.encode("utf-8")) // 2)
        return _failure(
            "message_id is missing, expired, or excluded by visibility filters"
        )

    def _encode_read_page(self, page: dict) -> str:
        while True:
            encoded = compact_json(page)
            if len(encoded.encode("utf-8")) <= MAX_PAGE_BYTES:
                return encoded
            if page.get("roster"):
                page["roster"] = page["roster"][: len(page["roster"]) // 2]
                page["roster_has_more"] = True
            elif len(page["messages"]) > 1:
                page["messages"].pop()
                page["next_id"] = page["messages"][-1]["id"]
                page["has_more"] = True
            elif len(page["messages"]) == 1 and page["messages"][0]["content"]:
                message = page["messages"][0]
                content = excerpt(message["content"], len(message["content"].encode("utf-8")) // 2)
                message["content"] = content
                message["next_offset"] = len(content.encode("utf-8"))
                message["has_more"] = message["next_offset"] < message["total_bytes"]
            elif len(page.get("error", "")) > 128:
                page["error"] = excerpt(page["error"], 128)
            else:
                return _failure("forum message metadata exceeds read page budget")

    def _peers_done_locked(self, agent_id: str, query: dict) -> bool:
        if query["from"]:
            participant = self._agents.get(query["from"])
            return participant is not None and participant.status in (
                "completed",
                "failed",
                "cancelled",
            )
        stage = self._agents[agent_id].stage
        for other_id, participant in self._agents.items():
            if (
                other_id != agent_id
                and participant.stage == stage
                and participant.status not in ("completed", "failed", "cancelled")
            ):
                return False
        return True

    def _roster_locked(self) -> list[dict]:
        return [
            self._agents[agent_id].to_dict()
            for agent_id in sorted(self._agents)
        ]

    # -- tool dispatch -------------------------------------------------

    def call(self, ctx, agent_id: str, stage: str, name: str, args) -> str:
        if not is_tool(name):
            return _failure("未知论坛工具")
        with self._cond:
            participant = self._agents.get(agent_id)
        if participant is None or participant.stage != stage:
            return _failure("未注册的 Agent 或阶段不匹配")

        raw_error = False
        data = {}
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError):
                raw_error = True
            else:
                if isinstance(parsed, dict):
                    data = parsed
                else:
                    raw_error = True
        elif isinstance(args, dict):
            data = args
        elif args is None:
            data = {}
        else:
            raw_error = True

        if name in ("forum_moderate", "forum_announce"):
            if agent_id != "moderator" or stage != "moderator":
                return _failure("仅论坛管理员可以管理线程或发布公告")
            if raw_error:
                return _failure(
                    "无效公告参数" if name == "forum_announce" else "无效线程管理参数"
                )
            return moderation_module.moderator_call(self, agent_id, stage, name, data)
        if name == "forum_threads":
            return posts_module.thread_page(self, None if raw_error else data)
        if name == "forum_post":
            if raw_error:
                return _failure("无效帖子参数")
            try:
                to = data.get("to", "")
                reply_to = _as_int(data.get("reply_to", 0))
                topic = data.get("topic", "")
                content = data.get("content", "")
            except ValueError:
                return _failure("无效帖子参数")
            if not isinstance(to, str) or not isinstance(topic, str) or not isinstance(content, str):
                return _failure("无效帖子参数")
            try:
                message = self.post(agent_id, stage, to, reply_to, topic, content)
            except ValueError as exc:
                return _failure(str(exc))
            return compact_json({"ok": True, "message": message.to_dict()})
        if name == "forum_roster":
            with self._cond:
                roster = self._roster_locked()
            return compact_json({"ok": True, "agents": roster})

        if raw_error:
            return _failure("无效游标、条数、正文偏移或等待时间")
        try:
            query = self._normalize_query(data)
        except ValueError:
            return _failure("无效游标、条数、正文偏移或等待时间")
        if query["message_id"] is not None and (name != "forum_read" or query["after_id"] != 0):
            return _failure(
                "message_id body paging requires forum_read and cannot use after_id"
            )
        if query["message_id"] is None and (query["offset"] != 0 or query["max_bytes"] != 0):
            return _failure("正文 offset / max_bytes 必须同时指定 message_id")
        if query["max_bytes"] == 0:
            query["max_bytes"] = 1024
        if query["max_bytes"] > MAX_PAGE_BYTES:
            query["max_bytes"] = MAX_PAGE_BYTES
        if query["limit"] == 0:
            query["limit"] = 16
        if query["limit"] > MAX_PAGE:
            query["limit"] = MAX_PAGE
        with self._cond:
            known_from = query["from"] in self._agents
            latest = self._next_id
        if query["from"] and not known_from:
            return _failure("未知发帖人过滤条件")
        if query["after_id"] > latest:
            return _failure("after_id 超过当前论坛游标")
        if name == "forum_read":
            with self._cond:
                if query["message_id"] is not None:
                    return self._read_body_locked(agent_id, query)
                return self._encode_read_page(self._read_locked(agent_id, query))
        return self._wait(ctx, agent_id, query)

    def _normalize_query(self, data: dict) -> dict:
        def integer(key: str, default: int = 0) -> int:
            if key not in data or data[key] is None:
                return default
            return _as_int(data[key])

        message_id = data.get("message_id")
        if message_id is not None:
            message_id = _as_int(message_id)
            if message_id <= 0:
                raise ValueError("message_id")
        timeout = data.get("timeout_seconds")
        timeout = 0.0 if timeout is None else _as_float(timeout)
        query = {
            "after_id": integer("after_id"),
            "limit": integer("limit"),
            "all": bool(data.get("all", False)),
            "thread_id": integer("thread_id"),
            "from": data.get("from", "") or "",
            "timeout_seconds": timeout,
            "message_id": message_id,
            "offset": integer("offset"),
            "max_bytes": integer("max_bytes"),
        }
        for key in ("after_id", "limit", "thread_id", "offset", "max_bytes"):
            if query[key] < 0:
                raise ValueError(key)
        if query["timeout_seconds"] < 0:
            raise ValueError("timeout_seconds")
        if not isinstance(query["from"], str):
            raise ValueError("from")
        return query

    def _wait(self, ctx, agent_id: str, query: dict) -> str:
        duration = self._default_wait
        if query["timeout_seconds"] > MAX_WAIT:
            query["timeout_seconds"] = MAX_WAIT
        if query["timeout_seconds"] > 0:
            duration = max(0.001, query["timeout_seconds"])
        deadline = time.monotonic() + duration
        while True:
            with self._cond:
                page = self._read_locked(agent_id, query)
                page["peers_done"] = self._peers_done_locked(agent_id, query)
                if page["peers_done"]:
                    page["roster"] = self._roster_locked()
            if _ctx_cancelled(ctx):
                page["ok"] = False
                page["cancelled"] = True
                page["error"] = "context canceled"
                return self._encode_read_page(page)
            if page["messages"] or page["gap"] or page["peers_done"]:
                return self._encode_read_page(page)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._cond:
                    page = self._read_locked(agent_id, query)
                    page["roster"] = self._roster_locked()
                    page["peers_done"] = self._peers_done_locked(agent_id, query)
                page["timed_out"] = not page["messages"]
                return self._encode_read_page(page)
            with self._cond:
                self._cond.wait(timeout=min(remaining, 0.05))

    def list_posts(self, page: int, page_size: int, search: str = "") -> posts_module.PostPage:
        query = posts_module.new_post_search(search, "phrase") if search else None
        return posts_module.list_posts(self, page, page_size, query)
