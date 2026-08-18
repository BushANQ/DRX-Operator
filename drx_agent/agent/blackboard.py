"""Shared operational blackboard — the war-room board all agents read and write.

Stigmergic coordination surface (inspired by strix / swarm blackboards):
- Master and every sub-agent see the board; every agent can write entries.
- Sections carry different information types so readers can scan fast.
- Dead-end entries persist so no agent repeats a failed path.
- Board size is bounded; oldest entries drop off the render (and storage).
"""

from __future__ import annotations

import time
import uuid

SECTIONS = {
    "objective": "作战目标",
    "findings": "已确认发现",
    "hypotheses": "待验证假设",
    "dead_ends": "已尝试死路（禁止重复）",
    "credentials": "凭据",
    "next_steps": "下一步计划",
}

MAX_PER_SECTION = 30


def _norm_section(section: str) -> str:
    key = (section or "").strip().lower()
    if key in SECTIONS:
        return key
    for k, v in SECTIONS.items():
        if v == section or key == k:
            return k
    return ""


class Blackboard:
    def __init__(self):
        self._entries: dict[str, list[dict]] = {k: [] for k in SECTIONS}

    def add(self, section: str, text: str, author: str = "") -> bool:
        key = _norm_section(section)
        if not key or not text:
            return False
        entry = {
            "id": f"bb-{uuid.uuid4().hex[:6]}",
            "text": str(text).strip()[:500],
            "author": author[:60],
            "ts": time.time(),
        }
        bucket = self._entries[key]
        dup = any(e["text"] == entry["text"] for e in bucket)
        if dup:
            return False
        bucket.append(entry)
        if len(bucket) > MAX_PER_SECTION:
            del bucket[: len(bucket) - MAX_PER_SECTION]
        return True

    def entries(self, section: str) -> list[dict]:
        key = _norm_section(section)
        return list(self._entries.get(key, [])) if key else []

    def render(self, max_chars: int = 3000) -> str:
        lines: list[str] = ["【黑板报 — 全体 Agent 共享作战状态】"]
        empty = True
        for key, label in SECTIONS.items():
            bucket = self._entries.get(key, [])
            if not bucket:
                continue
            empty = False
            lines.append(f"◆ {label}:")
            for e in bucket[-10:]:
                who = f" ({e['author']})" if e.get("author") else ""
                lines.append(f"  - {e['text']}{who}")
        if empty:
            lines.append("  (空 — 用 blackboard_write 记录)")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n  ...(黑板已截断，用 blackboard_read 看全文)"
        return text

    def to_dict(self) -> dict:
        return {"entries": {k: v for k, v in self._entries.items() if v}}

    @classmethod
    def from_dict(cls, data: dict) -> "Blackboard":
        bb = cls()
        for key, bucket in (data.get("entries") or {}).items():
            if key in bb._entries and isinstance(bucket, list):
                bb._entries[key] = [
                    e for e in bucket if isinstance(e, dict) and e.get("text")
                ]
        return bb
