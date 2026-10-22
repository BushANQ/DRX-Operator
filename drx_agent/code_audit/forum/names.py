"""Permanent public agent names and preset nickname allocation."""

from __future__ import annotations

import unicodedata

PRESET_NAMES = (
    "布丁", "麻薯", "饭团", "桃子", "柠檬", "西瓜", "芒果", "草莓",
    "葡萄", "樱桃", "荔枝", "椰子", "菠萝", "蓝莓", "橙子", "柚子",
    "汤圆", "豆包", "烧麦", "饺子", "年糕", "蛋挞", "泡芙", "饼干",
    "奶糖", "果冻", "爆米花", "小笼包", "糯米糍", "棉花糖", "南瓜饼", "土豆泥",
    "冰柚", "火鸡",
)

RESERVED_NAMES = ("user", "system", "coordinator", "moderator", "论坛管理员")


_FORBIDDEN_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")


def _has_forbidden_rune(name: str) -> bool:
    for ch in name:
        if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            return True
    return False


def valid_name(name: str) -> bool:
    if not name or len(name.encode("utf-8")) > 64:
        return False
    if _has_forbidden_rune(name):
        return False
    return True


def register_name_locked(board, agent_id: str, name: str) -> None:
    participant = board._agents.get(agent_id)
    if participant is None or participant.stage in ("user", "system"):
        raise ValueError("this identity cannot register an agent name")
    if participant.name:
        if participant.name == name:
            return
        raise ValueError(
            "name already registered as %r; renaming is not allowed" % participant.name
        )
    lowered = name.lower()
    for reserved in RESERVED_NAMES:
        if lowered == reserved.lower():
            raise ValueError("name is reserved; choose another name")
    for other_id, other in board._agents.items():
        if lowered == other_id.lower() or (other.name and lowered == other.name.lower()):
            raise ValueError("name is already taken or reserved; choose another name")
    participant.name = name
    board._agents[agent_id] = participant
    old_total = 0
    new_total = 0
    for message in board._msgs:
        if message.agent_id != agent_id:
            continue
        old_total += len(message.agent_name.encode("utf-8"))
        message.agent_name = name
        new_total += len(message.agent_name.encode("utf-8"))
    board._bytes += new_total - old_total
    board._trim_locked()
    board._signal_locked()


def register_name(board, agent_id: str, name: str) -> None:
    for ch in name:
        if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            raise ValueError("name must be single-line without control/format characters")
    name = name.strip()
    if not valid_name(name):
        raise ValueError(
            "name must be a nonempty single line, at most 64 UTF-8 bytes, "
            "with no control/format characters"
        )
    with board.cond:
        register_name_locked(board, agent_id, name)


def name(board, agent_id: str) -> str:
    with board.cond:
        participant = board._agents.get(agent_id)
        return participant.name if participant else ""


def restore_names(board, names: dict[str, str]) -> None:
    for agent_id in names:
        with board.cond:
            exists = agent_id in board._agents
        if not exists:
            stage = "moderator" if agent_id == "moderator" else "audit"
            board.register(agent_id, stage)
            board.set_status(agent_id, "completed")
    for agent_id, agent_name in names.items():
        try:
            register_name(board, agent_id, agent_name)
        except ValueError as exc:
            raise ValueError("restore name for %s: %s" % (agent_id, exc)) from exc


def ensure_preset_name(board, agent_id: str) -> str:
    with board.cond:
        participant = board._agents.get(agent_id)
        if participant is None or participant.stage in ("user", "system"):
            return ""
        if participant.name:
            return participant.name
        round_number = 0
        while True:
            for base in PRESET_NAMES:
                candidate = base
                if round_number > 0:
                    candidate = "%s%03d" % (base, round_number)
                try:
                    register_name_locked(board, agent_id, candidate)
                    return candidate
                except ValueError:
                    continue
            round_number += 1
