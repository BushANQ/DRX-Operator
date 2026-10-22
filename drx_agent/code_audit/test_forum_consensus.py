"""Stage close requests and unanimous voting."""

from __future__ import annotations

import time

from drx_agent.code_audit.forum import Board
from drx_agent.code_audit.forum.consensus import CLOSE_CONSENSUS_TIMEOUT


def make_board() -> Board:
    board = Board(0.02)
    for agent_id in ("a", "b", "c"):
        board.register(agent_id, "audit")
        board.register_name(agent_id, "tester-" + agent_id)
    board.register("coordinator", "system")
    board.set_consensus_members("audit", ["a", "b", "c"])
    return board


def test_close_consensus_requires_unanimous_approval():
    board = make_board()
    invalid = board.request_close("a", "audit", "摘要", "后续", "")
    assert invalid.status == "invalid"
    first = board.request_close("a", "audit", "摘要", "后续", "approve")
    assert first.status == "pending"
    assert first.required == 3
    assert first.approved == 1
    second = board.request_close("b", "audit", "摘要", "后续", "approve")
    assert second.status == "pending"
    assert second.approved == 2
    rejected = board.request_close("c", "audit", "摘要", "后续", "reject")
    assert rejected.status == "rejected"
    assert rejected.approved == 2
    assert board.consensus_approved("a", "audit") is None

    fresh = board.request_close("a", "audit", "新摘要", "新后续", "approve")
    assert fresh.status == "pending"
    assert fresh.request_id != first.request_id
    assert fresh.approved == 1
    timed_out = board.request_close_at(
        "b", "audit", "新摘要", "新后续", "approve", time.time() + CLOSE_CONSENSUS_TIMEOUT + 1
    )
    assert timed_out.status == "timed_out"
    assert timed_out.approved == 1


def test_close_consensus_callback_fires_only_at_unanimity():
    board = make_board()
    callbacks = []
    board.set_on_consensus(callbacks.append)
    for agent_id in ("a", "b"):
        decision = board.request_close(agent_id, "audit", "摘要", "后续", "approve")
        assert decision.status == "pending"
    assert callbacks == []
    final = board.request_close("c", "audit", "摘要", "后续", "approve")
    assert final.status == "approved"
    assert final.approved == 3
    decision = board.consensus_approved("a", "audit")
    assert decision is not None
    assert decision.status == "approved"
    assert callbacks == ["audit"]
    board.reset_consensus("audit")
    following = board.request_close("a", "audit", "新摘要", "新后续", "approve")
    assert following.status == "pending"
    assert following.request_id != final.request_id
    assert following.approved == 1


def test_completed_recon_and_moderator_do_not_block_audit_consensus():
    board = make_board()
    board.register("recon-1", "recon")
    board.set_status("recon-1", "completed")
    board.register("moderator", "moderator")
    assert board.request_close("moderator", "moderator", "summary", "", "approve").status == "invalid"
    for agent_id, stage in (("recon-1", "recon"), ("moderator", "moderator")):
        assert board.request_close(agent_id, "audit", "summary", "", "approve").status == "invalid"
    expected = ("pending", "pending", "approved")
    for index, agent_id in enumerate(("a", "b", "c")):
        decision = board.request_close(agent_id, "audit", "summary", "", "approve")
        assert decision.status == expected[index]
        assert decision.required == 3
        assert decision.approved == index + 1
