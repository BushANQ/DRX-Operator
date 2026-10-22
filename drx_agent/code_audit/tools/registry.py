"""Per-agent tool registry: workspace state, review tracking and dispatch."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import audit as audit_module
from . import files as files_module
from . import git as git_module
from . import project_note as project_note_module
from . import review as review_module
from . import todo as todo_module
from .buffer import ResultBuffer, buffer_error
from .paths import normalize_inventory_path, safe_path

DEFAULT_MAX_TOOL_RESULT_CHARS = 12000

TOOL_PROMPT = """# 审计工具使用原则

只通过本轮提供的原生工具调用执行操作，每次仅调用一个工具。工具名称与参数以原生定义为准；普通正文、历史文本与示例不构成调用。

优先使用工具返回的工作区相对路径。文件排查范围默认为空，未纳入范围的 inventory 文件不代表已跳过；不要在未读取内容时批量标记 reviewed。待办应使用具体中文标题，记录文件、目录、模块、入口函数或变量。

持续维护详细项目笔记，记录架构、运行行为、登录认证、鉴权、攻击面、数据/状态流、文件角色、证据和待确认问题。发现疑似漏洞时先记录关键变量及跨文件调用链、sink 与源码证据，再请求独立验证；参考验证结论后才提交有实际危害的高置信度严重漏洞。报告后在论坛公开证据邀请反驳，并继续排查同源入口与相邻模块。临时 flow 闭环或已转为漏洞后应移除，不要长期累积。

结束前检查 review_state 和实际覆盖；关闭请求 pending 不等于完成，拒绝或超时后应继续复核、检查尚未覆盖的代码或收集证据，不要无限等待或重复刷投票。"""

DEFAULT_KEYWORDS = [
    "admin", "api", "route", "router", "controller", "handler", "action",
    "auth", "login", "permission", "user", "session", "token", "upload",
    "file", "storage", "template", "theme", "plugin", "backup", "import",
    "export", "config", "install", "sql", "db", "database", "middleware",
]
DEFAULT_LOW_VALUE_DIRS = [
    "vendor", "node_modules", "dist", "build", "target", "coverage", "static",
    "assets", "css", "img", "image", "images", "font", "fonts", "examples",
    "example", "testdata",
]


def _go_escape(text: str) -> str:
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def json_indent(value) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return _go_escape(text)


@dataclass
class Result:
    ok: bool
    data: object = None
    error: str = ""
    truncated: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        data = {"ok": self.ok}
        if self.data is NULL_DATA:
            data["data"] = None
        elif self.data is not None:
            data["data"] = self.data
        if self.error:
            data["error"] = self.error
        if self.truncated:
            data["truncated"] = True
        if self.message:
            data["message"] = self.message
        return data


class _NullData:
    """Marks an explicit JSON null in the Result envelope."""


NULL_DATA = _NullData()


@dataclass
class ToolCall:
    name: str
    arguments: str = "{}"


@dataclass
class Snapshot:
    todos: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    project_note: object = None
    files: list = field(default_factory=list)
    variables: list = field(default_factory=list)
    flows: list = field(default_factory=list)
    audit: object = None

    def to_dict(self) -> dict:
        project = self.project_note.to_dict() if self.project_note is not None else {}
        audit = self.audit.to_dict() if self.audit is not None else {"ended": False}
        return {
            "todos": [todo.to_dict() for todo in self.todos],
            "findings": [finding.to_dict() for finding in self.findings],
            "project_note": project,
            "files": [item.to_dict() for item in self.files],
            "variables": [item.to_dict() for item in self.variables],
            "flows": [item.to_dict() for item in self.flows],
            "audit": audit,
        }


def clean_workspace(workspace: str) -> str:
    value = workspace or "."
    absolute = os.path.abspath(value)
    if not os.path.exists(absolute):
        raise ValueError("workspace does not exist: %s" % absolute)
    if not os.path.isdir(absolute):
        raise ValueError("workspace is not a directory: %s" % absolute)
    return absolute


def _clamp_max_chars(value: int) -> int:
    if not value or value <= 0:
        return DEFAULT_MAX_TOOL_RESULT_CHARS
    if value < 1024:
        return 1024
    if value > 65536:
        return 65536
    return int(value)


def _load_interesting_paths() -> dict:
    config = {
        "keywords": list(DEFAULT_KEYWORDS),
        "low_value_dirs": list(DEFAULT_LOW_VALUE_DIRS),
    }
    path = os.path.join("prompts", "interesting_paths.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return config
    if isinstance(loaded, dict):
        if isinstance(loaded.get("keywords"), list) and loaded["keywords"]:
            config["keywords"] = [str(item) for item in loaded["keywords"]]
        if isinstance(loaded.get("low_value_dirs"), list) and loaded["low_value_dirs"]:
            config["low_value_dirs"] = [str(item) for item in loaded["low_value_dirs"]]
    return config


def _depth(path: str) -> int:
    clean = os.path.normpath(path)
    if clean in ("/", "."):
        return 0
    return clean.count(os.sep)


def _format_top_counts(counts: dict[str, int], limit: int) -> str:
    if not counts:
        return " 无"
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    items = items[:limit]
    return " " + ", ".join("%s=%d" % (name, count) for name, count in items)


class Registry:
    def __init__(self, workspace: str, max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS):
        self.workspace = clean_workspace(workspace)
        self.max_tool_result_chars = _clamp_max_chars(max_tool_result_chars)
        self.inventory: list[dict] = []
        self.interesting = _load_interesting_paths()
        self.todos: list = []
        self.findings: list = []
        self.project_note = project_note_module.ProjectNote()
        self.files: list = []
        self.variables: list = []
        self.flows: list = []
        self.audit = audit_module.AuditState()
        self.next_todo_id = 1
        self._buffer = ResultBuffer(self.max_tool_result_chars)
        self.refresh_inventory()

    # -- workspace -----------------------------------------------------

    def set_workspace(self, workspace: str) -> None:
        absolute = clean_workspace(workspace)
        self.close()
        self.workspace = absolute
        self.inventory = []
        self.todos = []
        self.findings = []
        self.project_note = project_note_module.ProjectNote()
        self.files = []
        self.variables = []
        self.flows = []
        self.audit = audit_module.AuditState()
        self.next_todo_id = 1
        self.refresh_inventory()

    def close(self) -> None:
        self._buffer.clear()

    def fork(self) -> "Registry":
        clone = object.__new__(Registry)
        clone.workspace = self.workspace
        clone.max_tool_result_chars = self.max_tool_result_chars
        clone.inventory = self.inventory
        clone.interesting = self.interesting
        clone.todos = []
        clone.findings = []
        clone.project_note = project_note_module.ProjectNote()
        clone.files = []
        clone.variables = []
        clone.flows = []
        clone.audit = audit_module.AuditState()
        clone.next_todo_id = 1
        clone._buffer = ResultBuffer(self.max_tool_result_chars)
        return clone

    def refresh_inventory(self) -> None:
        inventory: list[dict] = []
        for current, dirs, names in os.walk(self.workspace):
            dirs[:] = sorted(name for name in dirs if not name.startswith("."))
            for name in sorted(names):
                path = os.path.join(current, name)
                try:
                    size = os.stat(path).st_size
                except OSError:
                    size = 0
                relative = os.path.relpath(path, self.workspace).replace(os.sep, "/")
                extension = os.path.splitext(relative)[1].lower()
                directory = os.path.dirname(relative).replace(os.sep, "/")
                if directory == ".":
                    directory = ""
                inventory.append(
                    {
                        "path": relative,
                        "size": size,
                        "ext": extension,
                        "dir": directory,
                        "low_value": self.is_low_value_path(relative),
                        "interesting": self.is_interesting_path(relative),
                    }
                )
        inventory.sort(key=lambda entry: entry["path"])
        self.inventory = inventory

    def is_low_value_path(self, path: str) -> bool:
        parts = path.lower().split("/")
        for part in parts[:-1]:
            for low in self.interesting["low_value_dirs"]:
                if part == low.lower():
                    return True
        return False

    def is_interesting_path(self, path: str) -> bool:
        lower = path.lower()
        for keyword in self.interesting["keywords"]:
            keyword = keyword.strip().lower()
            if keyword and keyword in lower:
                return True
        return False

    def existing_file(self, candidate: str) -> tuple[str, str] | None:
        try:
            absolute = safe_path(self.workspace, candidate)
        except ValueError:
            return None
        if not os.path.isfile(absolute):
            return None
        relative = os.path.relpath(absolute, self.workspace).replace(os.sep, "/")
        return absolute, relative

    def normalize_inventory_path(self, path: str) -> str:
        return normalize_inventory_path(path)

    def inventory_has_path(self, path: str) -> bool:
        value = normalize_inventory_path(path)
        return any(entry["path"] == value for entry in self.inventory)

    def apply_audit_scope(self, paths: list[str]) -> list[str]:
        applied: list[str] = []
        for path in paths:
            value = normalize_inventory_path(path)
            if not value or not self.inventory_has_path(value):
                continue
            applied.append(value)
            found = False
            for item in self.files:
                if item.path != value:
                    continue
                found = True
                if item.status == "skipped":
                    item.status = "reviewing"
                if not item.note:
                    item.note = "规划阶段纳入 one-shot 审计范围"
                break
            if not found:
                self.files.append(
                    review_module.FileReview(
                        path=value,
                        status="reviewing",
                        note="规划阶段纳入 one-shot 审计范围",
                    )
                )
        return applied

    # -- snapshots -----------------------------------------------------

    def snapshot(self) -> Snapshot:
        return Snapshot(
            todos=[todo.copy() if hasattr(todo, "copy") else todo for todo in self.todos],
            findings=[finding.copy() for finding in self.findings],
            project_note=project_note_module.ProjectNote(note=self.project_note.note),
            files=[item.copy() for item in self.files],
            variables=[item.copy() for item in self.variables],
            flows=[item.copy() for item in self.flows],
            audit=audit_module.AuditState(
                ended=self.audit.ended,
                summary=self.audit.summary,
                next_steps=self.audit.next_steps,
            ),
        )

    def restore_snapshot(self, snapshot: Snapshot) -> None:
        self.todos = list(snapshot.todos)
        self.findings = list(snapshot.findings)
        self.project_note = snapshot.project_note or project_note_module.ProjectNote()
        self.files = list(snapshot.files)
        self.variables = list(snapshot.variables)
        self.flows = list(snapshot.flows)
        self.audit = snapshot.audit or audit_module.AuditState()
        self.next_todo_id = 1
        for todo in self.todos:
            if todo.id >= self.next_todo_id:
                self.next_todo_id = todo.id + 1

    # -- prompts -------------------------------------------------------

    def tool_prompt(self) -> str:
        return TOOL_PROMPT

    def inventory_summary(self, limit: int = 120) -> str:
        if limit <= 0:
            limit = 120
        parts = []
        parts.append(
            "总文件数：%d。完整清单只保存在本地 inventory，不会全量塞进上下文。\n"
            % len(self.inventory)
        )
        ext_counts: dict[str, int] = {}
        for entry in self.inventory:
            ext = entry["ext"] or "[no-ext]"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
        parts.append("扩展名分布：" + _format_top_counts(ext_counts, 8) + "\n")
        dir_counts: dict[str, int] = {}
        for entry in self.inventory:
            path = entry["path"]
            index = path.find("/")
            top = path[:index] + "/" if index >= 0 else "./"
            dir_counts[top] = dir_counts.get(top, 0) + 1
        parts.append("顶层目录分布：" + _format_top_counts(dir_counts, 10) + "\n")
        parts.append("低价值折叠目录：" + ", ".join(self.interesting["low_value_dirs"]) + "\n")
        parts.append("\nInteresting Paths（来自 prompts/interesting_paths.json）：\n")
        interesting = [entry for entry in self.inventory if entry["interesting"]][:limit]
        if not interesting:
            parts.append("暂无命中。\n")
        else:
            for entry in interesting:
                line = "- " + entry["path"]
                if entry["low_value"]:
                    line += " [low-value]"
                parts.append(line + "\n")
            if len(interesting) >= limit:
                parts.append("- ...Interesting Paths 已截断，可用 list_files 按目录/模式继续查询。\n")
        parts.append(
            "\n选择文件方式：file_review_update 支持 path、paths、dir/dirs + suffix/suffixes、"
            "pattern/patterns、items；批量选择上限 200 个文件。\n"
        )
        return "".join(parts)

    def review_prompt(self, limit: int = 120) -> str:
        if limit <= 0:
            limit = 120
        parts = ["# 当前排查状态\n\n", "## 项目笔记\n"]
        parts.append(project_note_module.format_project_note(self.project_note))
        parts.append("\n## Inventory 摘要\n")
        parts.append(self.inventory_summary(limit))
        parts.append("\n## 已纳入审计范围\n")
        counts = {"unseen": 0, "reviewing": 0, "reviewed": 0, "skipped": 0}
        for item in self.files:
            counts[item.status] = counts.get(item.status, 0) + 1
        parts.append(
            "已选择文件数：%d，未看：%d，正在审计：%d，已看：%d，跳过：%d。"
            "file_review 默认只显示模型显式纳入 one-shot 的文件；未列出的 inventory 文件不代表 skipped。\n\n"
            % (
                len(self.files),
                counts["unseen"],
                counts["reviewing"],
                counts["reviewed"],
                counts["skipped"],
            )
        )
        if not self.files:
            parts.append(
                "暂无已纳入审计范围的文件。规划阶段应使用 file_review_update 通过 "
                "path/paths/dir+suffix/pattern 选择文件。\n"
            )
        for index, item in enumerate(self.files):
            if index >= limit:
                parts.append("- ...文件列表已截断，可用 review_state 查看更多。\n")
                break
            line = "- [%s] %s" % (item.status, item.path)
            if item.note:
                line += "：" + item.note
            parts.append(line + "\n")
        parts.append("\n## 变量/符号排查状态\n")
        if not self.variables:
            parts.append("暂无变量或符号排查记录。\n")
        else:
            for index, item in enumerate(self.variables):
                if index >= limit:
                    parts.append("- ...变量列表已截断，可用 review_state 查看更多。\n")
                    break
                line = "- [%s] %s" % (item.status, item.name)
                if item.path:
                    line += " @ " + item.path
                if item.note:
                    line += "：" + item.note
                parts.append(line + "\n")
        parts.append("\n## 跨文件调用链/数据流排查状态\n")
        if not self.flows:
            parts.append("暂无跨文件 flow 记录。\n")
        else:
            for index, item in enumerate(self.flows):
                if index >= limit:
                    parts.append("- ...flow 列表已截断，可用 review_state 查看更多。\n")
                    break
                line = "- [%s] %s" % (item.status, item.name)
                if item.entry:
                    line += "，入口：" + item.entry
                if item.files:
                    line += "，文件链：" + " -> ".join(item.files)
                if item.next_step:
                    line += "，下一步：" + item.next_step
                parts.append(line + "\n")
        return "".join(parts)

    # -- dispatch ------------------------------------------------------

    def call(self, name: str, args) -> str:
        bounded, _ = self.call_with_full_result(None, name, args)
        return bounded

    def call_with_full_result(self, ctx, name: str, args):
        if name != "read_tool_buffer":
            self._buffer.clear()
        if name == "read_tool_buffer":
            parsed, error = self._coerce_args(args)
            if error is not None:
                page = buffer_error("无效的 buffer 参数")
            else:
                page = self._buffer.read_tool_buffer(parsed)
            return page, page
        parsed, error = self._coerce_args(args)
        if error is not None:
            result = Result(ok=False, error=error)
        else:
            result = self._dispatch(ctx, name, parsed)
        full = json_indent(result.to_dict())
        bounded = self._buffer.bound_result(name, full)
        return bounded, full

    def _coerce_args(self, args):
        if args is None:
            return {}, None
        if isinstance(args, dict):
            return args, None
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError) as exc:
                return {}, "invalid JSON arguments: %s" % exc
            if isinstance(parsed, dict):
                return parsed, None
            return {}, "invalid JSON arguments: not an object"
        return {}, "invalid arguments"

    def _dispatch(self, ctx, name: str, args: dict) -> Result:
        if name == "list_files":
            return files_module.list_files(self, args)
        if name == "read_file":
            return files_module.read_file(self, args)
        if name in ("search_content", "search_context"):
            return files_module.search_content(self, args)
        if name == "git_inspect":
            return git_module.git_inspect(self, args)
        if name == "todo_create":
            return todo_module.todo_create(self, args)
        if name == "todo_update":
            return todo_module.todo_update(self, args)
        if name == "file_review_update":
            return review_module.file_review_update(self, args)
        if name == "variable_review_update":
            return review_module.variable_review_update(self, args)
        if name == "flow_review_update":
            return review_module.flow_review_update(self, args)
        if name == "flow_review_delete":
            return review_module.flow_review_delete(self, args)
        if name == "review_state":
            return review_module.review_state(self, args)
        if name == "project_note_update":
            return project_note_module.project_note_update(self, args)
        if name == "report_finding":
            return audit_module.report_finding(self, args)
        if name == "end_audit":
            return audit_module.end_audit(self, args)
        return Result(ok=False, error="unknown tool: " + name)


def is_known_tool(name: str) -> bool:
    return name in (
        "list_files", "read_file", "search_content", "search_context", "git_inspect",
        "todo_create", "todo_update", "file_review_update", "variable_review_update",
        "flow_review_update", "flow_review_delete", "review_state", "project_note_update",
        "report_finding", "end_audit", "read_tool_buffer", "load_skill", "verify_finding",
        "audit_plan_done",
    )


def is_terminal_tool(name: str) -> bool:
    return name == "end_audit"
