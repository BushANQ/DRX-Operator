"""forum_read body paging, visibility filters and serialized budgets."""

from __future__ import annotations

import json

import pytest

from drx_agent.code_audit.forum import Board


def make_board() -> Board:
    board = Board(0.02)
    for agent_id in ("a", "b", "c"):
        board.register(agent_id, "audit")
        assert board.register_name(agent_id, "tester-" + agent_id)
    return board


def call(board, agent_id, name, args, stage="audit"):
    return json.loads(board.call(None, agent_id, stage, name, args))


def test_long_body_discovery_and_exact_utf8_reconstruction():
    board = make_board()
    content = "中\"\\<>&\n" * 900
    message = board.post("a", "audit", "*", 0, '"' * 200, content)

    raw = board.call(None, "b", "audit", "forum_read", {"limit": 64})
    assert len(raw.encode("utf-8")) <= 4096
    discovery = json.loads(raw)
    assert discovery["ok"] is True
    assert len(discovery["messages"]) == 1
    assert discovery["next_id"] == message.id
    assert discovery["has_more"] is False

    preview = discovery["messages"][0]
    assert preview["has_more"] is True
    assert preview["total_bytes"] == len(content.encode("utf-8"))
    assert preview["next_offset"] == len(preview["content"].encode("utf-8"))
    assert preview["next_offset"] <= 512
    assert preview["content"] == content.encode("utf-8")[: preview["next_offset"]].decode("utf-8")

    rebuilt = preview["content"]
    offset = preview["next_offset"]
    while offset < len(content.encode("utf-8")):
        page = call(
            board,
            "b",
            "forum_read",
            {"message_id": message.id, "offset": offset, "max_bytes": 4096},
        )
        assert page["ok"] is True
        assert page["message"]["id"] == message.id
        assert page["offset"] == offset
        assert page["total_bytes"] == len(content.encode("utf-8"))
        assert page["next_offset"] == offset + len(page["message"]["content"].encode("utf-8"))
        assert page["next_offset"] > offset
        assert page["has_more"] == (page["next_offset"] < len(content.encode("utf-8")))
        rebuilt += page["message"]["content"]
        offset = page["next_offset"]
    assert rebuilt == content
    assert board.messages()[0].content == content

    end = call(board, "b", "forum_read", {"message_id": message.id, "offset": len(content.encode("utf-8"))})
    assert end["ok"] is True
    assert end["has_more"] is False
    assert end["next_offset"] == len(content.encode("utf-8"))
    assert end["message"]["content"] == ""


def test_body_rejects_invalid_offsets_and_mixed_cursors():
    board = make_board()
    message = board.post("a", "audit", "*", 0, "", "中文 evidence")
    invalid = [
        {"message_id": message.id, "offset": -1},
        {"message_id": message.id, "offset": 1},
        {"message_id": message.id, "offset": len(message.content.encode("utf-8")) + 1},
        {"message_id": message.id, "max_bytes": 1},
        {"message_id": message.id, "after_id": message.id},
        {"message_id": 0},
        {"offset": 3},
    ]
    for args in invalid:
        page = call(board, "b", "forum_read", args)
        assert page["ok"] is False
        assert page["error"]
    page = call(board, "b", "forum_read", {"message_id": message.id, "offset": 3, "max_bytes": 3})
    assert page["ok"] is True
    assert page["message"]["content"] == "文"
    assert page["next_offset"] == 6
    assert page["has_more"] is True


def test_body_visibility_thread_filters_and_expired_ids():
    board = make_board()
    root = board.post("a", "audit", "b", 0, "private routing", "root body")
    reply = board.post("b", "audit", "a", root.id, "", "reply body")
    nested = board.post("a", "audit", "b", reply.id, "", "nested body")
    other = board.post("c", "audit", "*", 0, "", "other body")
    for args in (
        {"message_id": root.id},
        {"message_id": root.id, "all": True, "from": "b"},
        {"message_id": nested.id, "all": True, "thread_id": other.id},
    ):
        page = call(board, "c", "forum_read", args)
        assert page["ok"] is False
        raw = json.dumps(page, ensure_ascii=False)
        assert "root body" not in raw
        assert "nested body" not in raw
    page = call(
        board,
        "c",
        "forum_read",
        {"message_id": nested.id, "all": True, "from": "a", "thread_id": reply.id},
    )
    assert page["ok"] is True
    assert page["message"]["content"] == nested.content

    retained = make_board()
    retained.restore([other])
    page = call(retained, "b", "forum_read", {"message_id": root.id, "offset": 3, "all": True})
    assert page["ok"] is False
    assert "expired" in page["error"]


def test_discovery_and_wait_respect_serialized_budget_without_skipping():
    board = make_board()
    ids = []
    for _ in range(18):
        message = board.post("a", "audit", "*", 0, "\\\"" * 80, "\\\"\n" * 300)
        ids.append(message.id)
    offset = 0
    seen = 0
    while True:
        raw = board.call(None, "b", "audit", "forum_wait", {"after_id": offset, "limit": 64})
        assert len(raw.encode("utf-8")) <= 4096
        page = json.loads(raw)
        assert page["ok"] is True
        assert page["messages"]
        for message in page["messages"]:
            assert message["id"] == ids[seen]
            assert message["has_more"] is True
            assert message["next_offset"] == len(message["content"].encode("utf-8"))
            seen += 1
        assert page["next_id"] == page["messages"][-1]["id"]
        assert page["next_id"] > offset
        offset = page["next_id"]
        if not page["has_more"]:
            break
    assert seen == len(ids)

    for index in range(128):
        agent_id = "peer-%03d-%s" % (index, "x" * 48)
        board.register(agent_id, "audit")
        board.set_status(agent_id, "completed")
    board.set_status("a", "completed")
    board.set_status("c", "completed")
    raw = board.call(None, "b", "audit", "forum_wait", {"after_id": offset})
    page = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 4096
    assert page["peers_done"] is True
    assert page["roster_has_more"] is True
    assert page["timed_out"] is False


def test_roster_shape_and_sorting():
    board = make_board()
    board.register("moderator", "moderator")
    page = call(board, "a", "forum_roster", {})
    ids = [agent["id"] for agent in page["agents"]]
    assert ids == sorted(ids)
    moderator = [agent for agent in page["agents"] if agent["id"] == "moderator"][0]
    assert moderator["name"] == "论坛管理员"
    assert moderator["status"] == "running"


@pytest.fixture(autouse=True)
def _noop():
    yield
