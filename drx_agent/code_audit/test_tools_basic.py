"""Tool registry basics: files, search, inventory and prompts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid

import pytest

from drx_agent.code_audit.tools import Registry, definitions, is_known_tool, is_terminal_tool


def _new_base() -> str:
    """Unique per process and per test so parallel pytest runs cannot collide."""
    root = os.path.join(tempfile.gettempdir(), "drx-audit-tests")
    os.makedirs(root, exist_ok=True)
    base = os.path.join(root, "%d-%s" % (os.getpid(), uuid.uuid4().hex))
    os.makedirs(base)
    return base


def _remove_base(base: str) -> None:
    def handle(func, target, error):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(base, onexc=handle)
    except TypeError:  # pragma: no cover - Python < 3.12
        shutil.rmtree(base, onerror=lambda func, target, info: handle(func, target, info[1]))
    try:
        os.rmdir(os.path.dirname(base))
    except OSError:
        pass


@pytest.fixture()
def workspace():
    path = _new_base()
    try:
        yield path
    finally:
        _remove_base(path)


def write(root: str, relative: str, content: str) -> None:
    path = os.path.join(root, relative.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def result(registry, name, args):
    return json.loads(registry.call(name, args))


def test_definitions_and_known_tools():
    names = [definition["name"] for definition in definitions()]
    assert names == [
        "list_files", "read_file", "read_tool_buffer", "search_content", "search_context",
        "git_inspect", "todo_create", "todo_update", "file_review_update",
        "variable_review_update", "flow_review_update", "flow_review_delete",
        "review_state", "project_note_update", "report_finding",
    ]
    for definition in definitions():
        assert definition["type"] == "function"
        assert definition["description"]
        assert isinstance(definition["parameters"], dict)
    assert is_known_tool("read_file") is True
    assert is_known_tool("load_skill") is True
    assert is_known_tool("forum_post") is False
    assert is_terminal_tool("end_audit") is True
    assert is_terminal_tool("read_file") is False


def test_list_files_filters_and_limit(workspace):
    write(workspace, "src/app.py", "print('x')\n")
    write(workspace, "src/util.py", "print('y')\n")
    write(workspace, "docs/readme.md", "docs\n")
    write(workspace, ".hidden/secret.py", "hidden\n")
    registry = Registry(workspace, 12000)
    data = result(registry, "list_files", {})["data"]
    paths = [entry["path"] for entry in data]
    assert "src/app.py" in paths
    assert ".hidden/secret.py" not in paths
    hidden = result(registry, "list_files", {"include_hidden": True})["data"]
    assert ".hidden/secret.py" in [entry["path"] for entry in hidden]
    matched = result(registry, "list_files", {"pattern": "*.py", "root": "src"})["data"]
    assert [entry["path"] for entry in matched] == ["src/app.py", "src/util.py"]
    limited = result(registry, "list_files", {"limit": 1})
    assert len(limited["data"]) == 1
    assert limited["truncated"] is True
    depth = result(registry, "list_files", {"max_depth": 1})["data"]
    assert "src/app.py" not in [entry["path"] for entry in depth]
    assert "src" in [entry["path"] for entry in depth]


def test_read_file_ranges_and_errors(workspace):
    write(workspace, "a.txt", "one\ntwo\nthree\n")
    write(workspace, "one/same.py", "a\n")
    write(workspace, "two/same.py", "b\n")
    registry = Registry(workspace, 12000)
    data = result(registry, "read_file", {"path": "a.txt"})["data"]
    assert data["path"] == "a.txt"
    assert data["total_lines"] == 3
    assert data["lines"] == [
        {"line": 1, "text": "one"},
        {"line": 2, "text": "two"},
        {"line": 3, "text": "three"},
    ]
    page = result(registry, "read_file", {"path": "a.txt", "offset": 2, "limit": 1})
    assert page["data"]["lines"] == [{"line": 2, "text": "two"}]
    assert page["truncated"] is True
    assert result(registry, "read_file", {})["ok"] is False
    assert result(registry, "read_file", {"path": "missing.txt"})["ok"] is False
    ambiguous = result(registry, "read_file", {"path": "same.py"})
    assert ambiguous["ok"] is False
    assert "ambiguous" in ambiguous["error"]


def test_read_file_recovers_prefixed_path(workspace):
    write(workspace, "pkg/module.py", "value = 1\n")
    registry = Registry(workspace, 12000)
    base = os.path.basename(workspace)
    data = result(registry, "read_file", {"path": base + "/pkg/module.py"})["data"]
    assert data["path"] == "pkg/module.py"
    data = result(registry, "read_file", {"path": "src/pkg/module.py"})["data"]
    assert data["path"] == "pkg/module.py"


def test_search_literal_regex_fuzzy_and_include(workspace):
    write(workspace, "a.py", "def login(user):\n    return user\n")
    write(workspace, "b.txt", "Login page\n")
    write(workspace, "sub/c.py", "no match here\n")
    registry = Registry(workspace, 12000)
    hits = result(registry, "search_content", {"query": "login"})["data"]
    assert {hit["path"] for hit in hits} == {"a.py", "b.txt"}
    assert hits[0]["line"] == 1
    hits = result(registry, "search_content", {"query": "Login", "case_sensitive": True})["data"]
    assert [hit["path"] for hit in hits] == ["b.txt"]
    regex = result(registry, "search_content", {"query": r"def\s+\w+\(", "mode": "regex"})["data"]
    assert len(regex) == 1
    assert regex[0]["path"] == "a.py"
    fuzzy = result(registry, "search_content", {"query": "defusr", "mode": "fuzzy"}).get("data")
    assert fuzzy and fuzzy[0]["path"] == "a.py"
    include = result(registry, "search_content", {"query": "login", "include": "*.py"})["data"]
    assert [hit["path"] for hit in include] == ["a.py"]
    missing = result(registry, "search_content", {"query": "absent"})
    assert missing["ok"] is True
    assert missing["data"] is None
    assert result(registry, "search_content", {})["ok"] is False


def test_path_sandbox_and_unknown_tool(workspace):
    write(workspace, "a.txt", "x\n")
    registry = Registry(workspace, 12000)
    escaped = result(registry, "list_files", {"root": ".."})
    assert escaped["ok"] is False
    assert "escapes workspace" in escaped["error"]
    unknown = result(registry, "not_a_tool", {})
    assert unknown["ok"] is False
    assert unknown["error"] == "unknown tool: not_a_tool"


def test_review_prompt_and_inventory_summary(workspace):
    write(workspace, "src/controller/LoginController.py", "class Login:\n    pass\n")
    write(workspace, "node_modules/lib/index.js", "x\n")
    registry = Registry(workspace, 12000)
    prompt = registry.review_prompt(10)
    assert "# 当前排查状态" in prompt
    assert "## 项目笔记" in prompt
    assert "## Inventory 摘要" in prompt
    assert "总文件数：2" in prompt
    assert "## 已纳入审计范围" in prompt
    assert "暂无已纳入审计范围的文件" in prompt
    assert "Interesting Paths" in prompt
    summary = registry.inventory_summary(10)
    assert "扩展名分布：" in summary
    assert "低价值折叠目录：" in summary
    assert "node_modules" in summary


def test_apply_audit_scope_and_fork_independence(workspace):
    write(workspace, "a.py", "x\n")
    write(workspace, "b.py", "y\n")
    registry = Registry(workspace, 12000)
    assert registry.apply_audit_scope(["a.py", "missing.py"]) == ["a.py"]
    assert registry.files[0].status == "reviewing"
    assert registry.files[0].note == "规划阶段纳入 one-shot 审计范围"
    registry.call("todo_create", {"title": "审计 a.py"})
    fork = registry.fork()
    assert fork.workspace == registry.workspace
    assert fork.inventory == registry.inventory
    assert fork.todos == []
    fork.call("todo_create", {"title": "fork todo"})
    assert len(registry.todos) == 1
    assert len(fork.todos) == 1
    registry.close()
    assert json.loads(registry.call("read_tool_buffer", {"buffer_id": "x"}))["ok"] is False
