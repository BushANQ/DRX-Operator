"""Forum search: literal terms, original UTF-8 offsets and excerpts."""

from __future__ import annotations

import json

from drx_agent.code_audit.forum import Board


def make_board() -> Board:
    board = Board(0.02)
    for agent_id in ("a", "b", "c"):
        board.register(agent_id, "audit")
        board.register_name(agent_id, "tester-" + agent_id)
    return board


def call(board, agent_id, name, args):
    return json.loads(board.call(None, agent_id, "audit", name, args))


def test_search_locations_read_original_unicode_reply():
    board = make_board()
    root = board.post("a", "audit", "*", 0, "root", "not the match")
    prefix = "旧证据\n" * 80 + "说明：İK中文 "
    body = prefix + "Needle\nTAIL"
    reply = board.post("b", "audit", "a", root.id, "reply title", body)

    result = call(board, "c", "forum_threads", {"query": "K中文 NEEDLE", "match": "all"})
    assert result["ok"] is True
    assert len(result["posts"]) == 1
    post = result["posts"][0]
    assert post["excerpt_message_id"] == reply.id
    assert len(post["matches"]) == 2
    assert post["read_args"] is not None

    first, second = post["matches"]
    kelvin = body.encode("utf-8").index("K".encode("utf-8"))
    assert first["offset"] == kelvin
    assert first["end_offset"] == kelvin + len("K中文".encode("utf-8"))
    assert second["offset"] == len(prefix.encode("utf-8"))
    assert second["end_offset"] == len(prefix.encode("utf-8")) + len("Needle".encode("utf-8"))
    assert second["line"] == 81
    assert second["column"] == 9

    page = call(board, "c", "forum_read", post["read_args"])
    assert page["ok"] is True
    assert page["message"]["id"] == reply.id
    content = page["message"]["content"]
    assert content == body.encode("utf-8")[page["offset"]:page["next_offset"]].decode("utf-8")
    assert "Needle" in content


def test_search_terms_cannot_combine_different_replies():
    board = make_board()
    root = board.post("a", "audit", "*", 0, "title", "alpha")
    board.post("b", "audit", "*", root.id, "reply", "beta")
    same = board.post("a", "audit", "*", 0, "alpha title", "beta evidence")
    for mode, total in (("all", 1), ("any", 2), ("phrase", 0)):
        result = call(board, "c", "forum_threads", {"query": "alpha beta", "match": mode})
        assert result["ok"] is True
        assert result["total_posts"] == total
        if mode == "all":
            assert result["posts"][0]["id"] == same.id
            assert result["posts"][0]["matches"][0]["field"] == "topic"
            assert result["posts"][0]["matches"][1]["field"] == "content"
    for raw_args in (
        {"query": "x", "match": "regex"},
        {"query": "a b c d e f g h i", "match": "all"},
        {"query": "中" * 180},
    ):
        result = call(board, "c", "forum_threads", raw_args)
        assert result["ok"] is False


def test_thread_search_pages_and_reply_excerpt():
    board = make_board()
    first = None
    for index in range(61):
        message = board.post("a", "audit", "*", 0, "title %d" % index, "body")
        if index == 0:
            first = message
    page = board.list_posts(1, 0, "")
    assert len(page.posts) == 60
    assert page.total_pages == 2
    assert page.has_more is True
    page = board.list_posts(99, 0, "")
    assert page.page == 2
    assert len(page.posts) == 1
    assert page.has_more is False

    reply = board.post(
        "b", "audit", "*", first.id, "reply title", "prefix " * 80 + "RareNeedle evidence"
    )
    page = board.list_posts(1, 0, "rareneedle")
    assert page.total_posts == 1
    assert page.posts[0].id == first.id
    assert board.list_posts(1, 0, "REPLY TITLE").total_posts == 1

    result = call(board, "c", "forum_threads", {"query": "rareneedle"})
    assert len(result["posts"]) == 1
    post = result["posts"][0]
    assert post["excerpt_message_id"] == reply.id
    assert "RareNeedle" in post["excerpt"]
    assert post["excerpt_truncated"] is True
    assert post["excerpt_offset"] > 0

    empty = board.list_posts(99, 0, "absent")
    assert empty.page == 1
    assert empty.total_posts == 0
    assert empty.posts == []
