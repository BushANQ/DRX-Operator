"""Forum moderation: permissions, pin slots, announcements and restore."""

from __future__ import annotations

import json
import threading

import pytest

from drx_agent.code_audit.forum import Board


def make_board() -> Board:
    board = Board(0.02)
    for agent_id in ("a", "b", "c"):
        board.register(agent_id, "audit")
        board.register_name(agent_id, "tester-" + agent_id)
    return board


def call(board, agent_id, stage, name, args):
    return json.loads(board.call(None, agent_id, stage, name, args))


def test_moderator_permissions_and_closed_nested_replies():
    board = make_board()
    root = board.post("a", "audit", "*", 0, "evidence", "original evidence")
    reply = board.post("b", "audit", "*", root.id, "", "nested evidence")
    args = {"thread_id": reply.id, "action": "close", "reason": "Superseded by verified evidence"}
    assert call(board, "moderator", "moderator", "forum_moderate", args)["ok"] is False

    board.register("moderator", "moderator")
    board.register("impostor", "moderator")
    before = board.messages()
    for agent_id, stage in (("a", "audit"), ("moderator", "audit"), ("impostor", "moderator")):
        assert call(board, agent_id, stage, "forum_moderate", args)["ok"] is False
        assert call(
            board,
            agent_id,
            stage,
            "forum_announce",
            {"topic": "forged", "content": "forged announcement", "pinned": True},
        )["ok"] is False
    assert call(
        board,
        "moderator",
        "moderator",
        "forum_moderate",
        {"thread_id": root.id, "action": "close", "reason": " "},
    )["ok"] is False
    assert board.messages() == before

    seen = {}

    def hook(message):
        board.messages()
        seen["message"] = message

    board.set_on_post(hook)
    assert call(board, "moderator", "moderator", "forum_moderate", args)["ok"] is True
    assert seen["message"].agent_id == "moderator"
    assert seen["message"].agent_name == "论坛管理员"
    assert "Superseded by verified evidence" in seen["message"].content

    for target in (root.id, reply.id):
        with pytest.raises(ValueError):
            board.post("a", "audit", "*", target, "", "forbidden reply")
        with pytest.raises(ValueError):
            board.post("moderator", "moderator", "*", target, "", "privileged bypass")

    for message in board.messages()[:2]:
        assert message.closed is True
        assert message.moderation_reason == "Superseded by verified evidence"
    assert call(
        board,
        "moderator",
        "moderator",
        "forum_moderate",
        {"thread_id": root.id, "action": "reopen", "reason": "New evidence available"},
    )["ok"] is True
    board.post("c", "audit", "*", reply.id, "", "new evidence")


def test_pinned_announcement_survives_root_eviction_and_restore():
    board = make_board()
    board.register("moderator", "moderator")
    hook_messages = []
    board.set_on_post(hook_messages.append)
    result = call(
        board,
        "moderator",
        "moderator",
        "forum_announce",
        {"topic": "Review scope", "content": "@all Please review uncovered code", "pinned": True},
    )
    assert result["ok"] is True
    root = result["message"]
    assert hook_messages[-1].pinned is True
    assert hook_messages[-1].announcement is True
    reply = board.post("a", "audit", "*", root["id"], "", "review evidence")
    board.post("b", "audit", "*", 0, "newer", "newer root")
    posts = board.list_posts(1, 10, "").posts
    assert posts[0].id == root["id"]
    assert posts[0].root.announcement is True

    assert call(
        board,
        "moderator",
        "moderator",
        "forum_moderate",
        {"thread_id": root["id"], "action": "close", "reason": "Review completed"},
    )["ok"] is True
    saved = board.messages()[1:]
    restored = make_board()
    restored.register("moderator", "moderator")
    restored.restore(saved)
    post = restored.list_posts(1, 10, "").posts[0]
    assert post.id == root["id"]
    assert post.root_missing is True
    assert post.root.closed is True
    assert post.root.pinned is True
    assert post.root.announcement is True
    assert post.root.moderation_reason == "Review completed"
    with pytest.raises(ValueError):
        restored.post("b", "audit", "*", reply.id, "", "forbidden after restore")
    page = call(restored, "b", "audit", "forum_threads", {"limit": 1})
    assert page["posts"][0]["announcement"] is True
    assert page["posts"][0]["closed"] is True
    assert page["posts"][0]["pinned"] is True
    assert call(
        restored,
        "moderator",
        "moderator",
        "forum_moderate",
        {"thread_id": root["id"], "action": "unpin", "reason": "No longer current"},
    )["ok"] is True
    assert restored.list_posts(1, 10, "").posts[0].id != root["id"]


def test_three_pin_slots_concurrent_admission_and_reply_bump():
    board = make_board()
    board.register("moderator", "moderator")
    roots = [board.post("a", "audit", "*", 0, "root%d" % index, "evidence") for index in range(8)]
    threads = []
    for root in roots:
        thread = threading.Thread(
            target=lambda root_id=root.id: board.call(
                None,
                "moderator",
                "moderator",
                "forum_moderate",
                {"thread_id": root_id, "action": "pin", "reason": "important"},
            )
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    posts = board.list_posts(1, 64, "").posts
    pinned = [post.id for post in posts if post.root.pinned]
    ordinary = [post.id for post in posts if not post.root.pinned]
    assert len(pinned) == 3
    before = board.messages()
    assert call(
        board, "moderator", "moderator", "forum_announce", {"content": "fourth", "pinned": True}
    )["ok"] is False
    assert board.messages() == before
    assert call(
        board,
        "moderator",
        "moderator",
        "forum_moderate",
        {"thread_id": pinned[2], "action": "pin", "reason": "same slot"},
    )["ok"] is True
    board.post("b", "audit", "*", pinned[2], "", "latest pinned reply")
    assert board.list_posts(1, 64, "").posts[0].id == pinned[2]
    board.post("b", "audit", "*", ordinary[0], "", "latest normal reply")
    posts = board.list_posts(1, 64, "").posts
    assert posts[3].id == ordinary[0]
    assert posts[0].root.pinned is True
    assert call(
        board,
        "moderator",
        "moderator",
        "forum_moderate",
        {"thread_id": pinned[0], "action": "unpin", "reason": "free slot"},
    )["ok"] is True
    assert call(
        board, "moderator", "moderator", "forum_announce", {"content": "replacement", "pinned": True}
    )["ok"] is True
    restored = make_board()
    restored.restore(board.messages())
    assert len([post for post in restored.list_posts(1, 64, "").posts if post.root.pinned]) == 3


def test_restore_rejects_contradictory_thread_state():
    board = make_board()
    root = board.post("a", "audit", "*", 0, "", "original")
    board.post("b", "audit", "*", root.id, "", "reply")
    before = board.messages()
    bad = [message.copy() for message in before]
    bad[0].closed = True
    bad[0].moderation_reason = "closed with evidence"
    with pytest.raises(ValueError):
        board.restore(bad)
    assert board.messages() == before
    bad = [message.copy() for message in before]
    bad[1].thread_id = bad[1].id
    with pytest.raises(ValueError):
        board.restore(bad)
