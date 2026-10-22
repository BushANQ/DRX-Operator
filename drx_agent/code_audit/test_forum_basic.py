"""Forum board basics: waiting, paging, retention, ordering, names."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid

import pytest

from drx_agent.code_audit.forum import Board, MAX_PINNED_POSTS, Message


def make_board() -> Board:
    board = Board(0.02)
    for agent_id in ("a", "b", "c"):
        board.register(agent_id, "audit")
        assert board.register_name(agent_id, "tester-" + agent_id)
    return board


def call(board, agent_id, name, args, stage="audit", ctx=None):
    return json.loads(board.call(ctx, agent_id, stage, name, args))


def test_wait_reply_cancellation_and_timeout():
    board = make_board()
    question = board.post("a", "audit", "b", 0, "问题", "请核实入口")
    result = {}

    def waiter():
        result["page"] = call(
            board, "a", "forum_wait", {"after_id": question.id, "from": "b", "timeout_seconds": 1}
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    reply = board.post("b", "audit", "a", question.id, "回复", "入口需要登录")
    thread.join(timeout=3)
    page = result["page"]
    assert len(page["messages"]) == 1
    assert page["messages"][0]["id"] == reply.id
    assert page["timed_out"] is False

    page = call(board, "a", "forum_wait", {"after_id": reply.id, "from": "b", "timeout_seconds": 0.01})
    assert page["timed_out"] is True
    assert page["cancelled"] is False

    class Cancelled:
        def is_cancelled(self):
            return True

    page = call(board, "a", "forum_wait", {"after_id": reply.id}, ctx=Cancelled())
    assert page["ok"] is False
    assert page["cancelled"] is True

    board.set_status("b", "completed")
    page = call(board, "a", "forum_wait", {"after_id": reply.id, "from": "b"})
    assert page["peers_done"] is True
    assert page["timed_out"] is False


def test_public_threads_and_pagination():
    board = make_board()
    root = board.post("a", "audit", "b", 0, "root", "one")
    reply = board.post("b", "audit", "a", root.id, "reply", "two")
    last = board.post("a", "audit", "b", reply.id, "nested", "three")

    page = call(board, "c", "forum_read", {"limit": 1})
    assert page["messages"] == []

    page = call(board, "c", "forum_read", {"limit": 2, "all": True, "thread_id": root.id})
    assert len(page["messages"]) == 2
    assert page["has_more"] is True
    assert page["next_id"] == reply.id

    page = call(
        board, "c", "forum_read", {"after_id": page["next_id"], "all": True, "thread_id": root.id}
    )
    assert len(page["messages"]) == 1
    assert page["messages"][0]["id"] == last.id

    assert call(board, "fake", "forum_read", {})["ok"] is False
    with pytest.raises(ValueError):
        board.post("a", "recon", "*", 0, "", "forged stage")


def test_concurrent_ids_retention_and_restore():
    board = make_board()
    threads = []
    for _ in range(8):
        thread = threading.Thread(
            target=lambda: [board.post("a", "audit", "*", 0, "", "并发记录") for _ in range(32)]
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    messages = board.messages()
    assert [message.id for message in messages] == list(range(1, 257))

    for _ in range(4096):
        board.post("b", "audit", "*", 0, "", "retention")
    page = call(board, "a", "forum_read", {})
    assert page["gap"] is True
    assert page["oldest_id"] > 1

    restored = make_board()
    restored.restore(board.messages())
    message = restored.post("c", "audit", "*", 0, "", "after restore")
    assert message.id > page["latest_id"]


def test_posts_sort_by_latest_activity_and_keep_replies():
    board = make_board()
    first = board.post("a", "audit", "*", 0, "first", "first body")
    second = board.post("b", "audit", "*", 0, "second", "second body")
    reply = board.post("c", "audit", "a", first.id, "", "reply to first")
    nested = board.post("a", "audit", "c", reply.id, "", "reply to reply")

    from drx_agent.code_audit.forum import group_posts

    posts = group_posts(board.messages())
    assert len(posts) == 2
    assert posts[0].id == first.id
    assert posts[1].id == second.id
    assert len(posts[0].replies) == 2
    assert nested.thread_id == first.id
    assert posts[0].last_id == nested.id

    page = call(board, "a", "forum_threads", {"limit": 1})
    assert len(page["posts"]) == 1
    assert page["posts"][0]["id"] == first.id
    assert page["has_more"] is True

    page = call(board, "a", "forum_threads", {"limit": 1, "page": page["page"] + 1})
    assert page["posts"][0]["id"] == second.id

    restored = make_board()
    restored.restore(board.messages()[2:])
    posts = group_posts(restored.messages())
    assert len(posts) == 1
    assert posts[0].id == first.id
    assert posts[0].root_missing is True


def test_numbered_pages_follow_activity_time():
    board = make_board()
    base = "2026-09-12T00:00:00Z"
    board.restore(
        [
            Message(id=1, thread_id=1, agent_id="a", stage="audit", content="oldest", created_at=base),
            Message(id=2, thread_id=2, agent_id="b", stage="audit", content="latest", created_at="2026-09-12T00:00:02Z"),
            Message(id=3, thread_id=3, agent_id="c", stage="audit", content="clock corrected", created_at="2026-09-12T00:00:01Z"),
        ]
    )
    page = 0
    order = []
    for _ in range(3):
        result = call(board, "a", "forum_threads", {"limit": 1, "page": page + 1})
        order.append(result["posts"][0]["id"])
        page = result["page"]
    assert order == [2, 3, 1]


def test_preset_names_rounds_and_uniqueness():
    board = Board(0)
    from drx_agent.code_audit.forum.names import PRESET_NAMES

    count = len(PRESET_NAMES)
    for index in range(2 * count + 1):
        agent_id = "a%d" % index
        board.register(agent_id, "audit")
        name = board.ensure_preset_name(agent_id)
        expected = PRESET_NAMES[index % count]
        if index >= count:
            expected = "%s%03d" % (expected, index // count)
        assert name == expected
        assert board.ensure_preset_name(agent_id) == name


def test_name_registration_rules():
    board = Board(0)
    board.register("a", "audit")
    board.register("b", "recon")
    prior = board.post("a", "audit", "*", 0, "", "before naming")
    for name in ("", "user", "SYSTEM", "coordinator", "b", "two\nlines", "zero\u200bwidth", "名" * 22):
        assert board.register_name("a", name) is False
    assert board.register_name("a", "  Falcon  ") is True
    message = board.messages()[0]
    assert message.id == prior.id
    assert message.agent_name == "Falcon"
    assert message.content == "before naming"
    assert board.register_name("a", "Falcon") is True
    assert board.register_name("a", "Other") is False
    board.set_status("a", "completed")
    assert board.register_name("b", "FALCON") is False
    assert board.post("a", "audit", "*", 0, "", "named message").agent_name == "Falcon"
    messages, names, participants = board.checkpoint()
    restored = Board(0)
    restored.register("a", "audit")
    restored.register("b", "recon")
    restored.restore(messages)
    restored.restore_names(names)
    restored.restore_participants(participants)
    assert restored.register_name("b", "falcon") is False


def test_pin_limit_constant():
    assert MAX_PINNED_POSTS == 3

