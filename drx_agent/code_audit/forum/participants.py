"""Retained-thread membership used by the notification digest."""

from __future__ import annotations


def join_thread(board, message) -> None:
    members = board._participants.get(message.thread_id)
    if members is None:
        members = {}
        board._participants[message.thread_id] = members
    if members.get(message.agent_id, 0) == 0:
        members[message.agent_id] = message.id


def restore_participants(board, saved: dict[int, dict[str, int]]) -> None:
    with board.cond:
        for thread, members in saved.items():
            current = board._participants.get(thread)
            if current is None:
                continue
            for agent_id, joined in members.items():
                if not agent_id or joined < thread or joined > board._next_id:
                    raise ValueError("invalid forum participant checkpoint")
                if current.get(agent_id, 0) == 0 or joined < current[agent_id]:
                    current[agent_id] = joined


def checkpoint(board):
    with board.cond:
        names = {
            agent_id: participant.name
            for agent_id, participant in board._agents.items()
            if participant.name
        }
        participants = {
            thread: dict(members) for thread, members in board._participants.items()
        }
        return board.messages(), names, participants
