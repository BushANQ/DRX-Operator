"""Project-level free-text working note."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProjectNote:
    note: str = ""

    def to_dict(self) -> dict:
        data = {}
        if self.note:
            data["note"] = self.note
        return data


def project_note_update(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    note = args.get("note", "")
    if not isinstance(note, str) or not note:
        return Result(ok=False, error="note is required")
    registry.project_note = ProjectNote(note=note)
    return Result(
        ok=True, data=registry.project_note.to_dict(), message="project note updated"
    )


def format_project_note(note: ProjectNote) -> str:
    if not note.note.strip():
        return (
            "暂无项目笔记。规划阶段必须调用 project_note_update 维护详细自由文本笔记，"
            "像人工审计员一样记录项目架构、行为、登录认证、鉴权、攻击面、数据/状态流、"
            "关键文件角色、已知结论和待确认问题。\n"
        )
    return note.note + "\n"
