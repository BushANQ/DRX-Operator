"""Forum notification digest: participants, bursts and mentions."""

from __future__ import annotations

import json

from drx_agent.code_audit.forum import Board
from drx_agent.code_audit.forum.notifications import NOTIFICATION_MARKER


def make_board() -> Board:
    board = Board(0.02)
    for agent_id in ("a", "b", "c"):
        board.register(agent_id, "audit")
        board.register_name(agent_id, "tester-" + agent_id)
    return board


def notice(board, agent_id, after, budget=1024):
    text, cursor = board.notification(agent_id, after, budget)
    page = {}
    if text:
        assert text.startswith(NOTIFICATION_MARKER)
        assert len(text.encode("utf-8")) <= budget
        page = json.loads(text[len(NOTIFICATION_MARKER):])
        assert page["next_id"] == cursor
    return page, cursor, text


def test_notification_thread_participants_and_new_roots():
    board = make_board()
    board.register("d", "audit")
    root = board.post("a", "audit", "b", 0, "title", "new root")
    joined = board.post("b", "audit", "c", root.id, "", "B joins")
    for agent_id in ("a", "b", "c", "d"):
        page, _, _ = notice(board, agent_id, 0)
        for entry in page.get("entries", []):
            if entry["latest_message_id"] == joined.id and agent_id != "a":
                raise AssertionError("nonparticipant received reply")
        if agent_id == "c":
            assert len(page["entries"]) == 1
            assert page["entries"][0]["latest_message_id"] == root.id
    last = board.post("d", "audit", "c", joined.id, "", "D replies to the thread")
    for agent_id in ("a", "b"):
        page, cursor, _ = notice(board, agent_id, joined.id)
        assert len(page["entries"]) == 1
        assert page["entries"][0]["thread_id"] == root.id
        assert page["entries"][0]["latest_message_id"] == last.id
        assert page["entries"][0]["topic"] == "title"
        assert notice(board, agent_id, cursor)[2] == ""
    for agent_id in ("c", "d"):
        page, cursor, text = notice(board, agent_id, joined.id)
        assert text == ""
        assert cursor == last.id
    board.call(None, "c", "audit", "forum_read", {"all": True})
    page, _, _ = notice(board, "d", 0, 2048)
    assert len(page["entries"]) == 1
    assert page["entries"][0]["latest_message_id"] == root.id
    board.register("user", "user")
    user = board.post("user", "user", "*", 0, "user title", "user broadcast")
    page, _, _ = notice(board, "c", last.id)
    assert len(page["entries"]) == 1
    assert page["entries"][0]["latest_message_id"] == user.id


def test_notification_burst_byte_bounds_and_retention_restore():
    board = make_board()
    want = []
    for _ in range(25):
        message = board.post("b", "audit", "*", 0, "<题>" * 60, "<原文>\n" * 1000)
        want.append(message.id)
    cursor = 0
    got = []
    pages = 0
    while True:
        page, next_cursor, text = notice(board, "a", cursor, 1024)
        if not text:
            break
        pages += 1
        assert next_cursor > cursor
        for entry in page["entries"]:
            assert entry["truncated"] is True
            assert len(entry["excerpt"].encode("utf-8")) <= 256
            got.append(entry["latest_message_id"])
        cursor = next_cursor
        if not page["has_more"]:
            break
    assert pages >= 2
    assert got == want

    board = make_board()
    root = board.post("a", "audit", "*", 0, "retained thread", "root")
    joined = board.post("b", "audit", "*", root.id, "", "join")
    from drx_agent.code_audit.forum.forum import MAX_MESSAGES

    for _ in range(MAX_MESSAGES - 2):
        board.post("c", "audit", "*", 0, "", "filler")
    late = board.post("b", "audit", "*", joined.id, "", "retained reply")
    board.post("c", "audit", "*", 0, "", "evict early join")
    messages, names, members = board.checkpoint()
    assert messages[0].id > joined.id
    restored = make_board()
    restored.restore(messages)
    restored.restore_names(names)
    restored.restore_participants(members)
    last = restored.post("c", "audit", "*", late.id, "", "after restore")
    page, _, _ = notice(restored, "a", last.id - 1, 2048)
    assert len(page["entries"]) == 1
    assert page["entries"][0]["latest_message_id"] == last.id
    page, _, _ = notice(restored, "a", 0, 1024)
    assert page["gap"] is True
    assert page["oldest_id"] == messages[1].id


def test_all_mention_reaches_nonparticipants_without_self_notification():
    for mention in ("@all", "@全体成员", "@全体"):
        board = make_board()
        root = board.post("a", "audit", "*", 0, "review", "original")
        first = board.post("b", "audit", "a", root.id, "", "ordinary reply")
        _, cursor, text = notice(board, "c", root.id)
        assert text == ""
        assert cursor == first.id
        broadcast = board.post("b", "audit", "a", first.id, "", mention + " " + "新证据<>" * 300)
        for agent_id in ("a", "c"):
            page, cursor, _ = notice(board, agent_id, first.id, 1024)
            assert len(page["entries"]) == 1
            assert page["entries"][0]["latest_message_id"] == broadcast.id
            assert page["entries"][0]["thread_id"] == root.id
            assert cursor == broadcast.id
            assert notice(board, agent_id, cursor)[2] == ""
        _, cursor, text = notice(board, "b", first.id)
        assert text == ""
        assert cursor == broadcast.id
        plain = board.post("a", "audit", "*", first.id, "", "ordinary follow-up")
        _, cursor, text = notice(board, "c", broadcast.id)
        assert text == ""
        assert cursor == plain.id
