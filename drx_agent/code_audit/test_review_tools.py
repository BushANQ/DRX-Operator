"""Review state tools: todos, files, variables, flows, notes and findings."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid

import pytest

from drx_agent.code_audit.tools import Registry, finding_key


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
def registry():
    path = _new_base()
    for relative, content in (
        ("src/controller/LoginController.py", "class Login:\n    pass\n"),
        ("src/service/UserService.py", "def load():\n    pass\n"),
        ("static/app.css", "body {}\n"),
        ("README.md", "docs\n"),
    ):
        target = os.path.join(path, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
    instance = Registry(path, 12000)
    try:
        yield instance
    finally:
        instance.close()
        _remove_base(path)


def call(registry, name, args):
    return json.loads(registry.call(name, args))


def test_todo_lifecycle(registry):
    created = call(registry, "todo_create", {"title": "审计 src/controller/LoginController.py 鉴权"})
    assert created["ok"] is True
    assert created["message"] == "todo created"
    assert created["data"][0] == {
        "id": 1,
        "title": "审计 src/controller/LoginController.py 鉴权",
        "status": "pending",
        "priority": "medium",
    }
    call(registry, "todo_create", {"title": "审计 src/service/UserService.py 数据流", "priority": "high"})
    updated = call(registry, "todo_update", {"id": 1, "status": "done"})
    assert updated["data"][0]["status"] == "completed"
    updated = call(registry, "todo_update", {"id": "2", "title": "重命名任务"})
    assert updated["data"][1]["title"] == "重命名任务"
    by_title = call(registry, "todo_update", {"title": "重命名任务", "status": "in_progress"})
    assert by_title["data"][1]["status"] == "in_progress"
    assert call(registry, "todo_update", {"id": 99, "status": "completed"})["error"] == "todo not found"
    assert call(registry, "todo_update", {"status": "completed"})["ok"] is False
    assert call(registry, "todo_update", {"id": "abc"})["ok"] is False
    call(registry, "todo_create", {"title": "重复标题"})
    call(registry, "todo_create", {"title": "重复标题"})
    ambiguous = call(registry, "todo_update", {"title": "重复标题", "status": "completed"})
    assert ambiguous["ok"] is False
    assert "multiple todos match title" in ambiguous["error"]
    assert call(registry, "todo_create", {})["error"] == "title is required"


def test_file_review_single_and_batch(registry):
    added = call(
        registry,
        "file_review_update",
        {"path": "src/controller/LoginController.py", "status": "reviewing", "note": "入口控制器"},
    )
    assert added["message"] == "file review added"
    assert added["data"]["status"] == "reviewing"
    updated = call(
        registry, "file_review_update", {"path": "src/controller/LoginController.py", "status": "reviewed"}
    )
    assert updated["message"] == "file review updated"
    assert updated["data"]["note"] == "入口控制器"
    batch = call(
        registry,
        "file_review_update",
        {
            "items": [
                {"path": "src/service/UserService.py", "status": "reviewing", "note": "服务层"},
                {"path": "static/app.css", "status": "skipped"},
            ]
        },
    )
    assert batch["data"]["total"] == 2
    assert batch["data"]["added"] == 2
    selected = call(
        registry,
        "file_review_update",
        {"dir": "src", "suffix": "Controller.py", "status": "reviewing", "note": "范围"},
    )
    assert selected["ok"] is True
    pattern = call(registry, "file_review_update", {"pattern": "*.md", "status": "skipped"})
    assert pattern["ok"] is True
    assert call(registry, "file_review_update", {"path": "src/controller/LoginController.py"})[
        "data"
    ]["status"] == "reviewed"
    empty = call(registry, "file_review_update", {"dir": "no-such-dir"})
    assert empty["ok"] is True
    assert empty["message"] == "file review selector matched no files"
    assert call(registry, "file_review_update", {})["error"] == "path is required"
    bad_batch = call(
        registry,
        "file_review_update",
        {"items": [{"path": "a.py"}, {"path": ""}]},
    )
    assert bad_batch["ok"] is False
    assert bad_batch["error"] == "path is required for every item"


def test_variable_and_flow_reviews(registry):
    variable = call(registry, "variable_review_update", {"name": "userId", "path": "src/service/UserService.py"})
    assert variable["message"] == "variable review added"
    assert variable["data"]["status"] == "tracking"
    variable = call(
        registry,
        "variable_review_update",
        {"name": "userId", "path": "src/service/UserService.py", "status": "suspicious", "note": "未校验"},
    )
    assert variable["message"] == "variable review updated"
    assert variable["data"]["note"] == "未校验"
    assert call(registry, "variable_review_update", {})["error"] == "name is required"

    flow = call(
        registry,
        "flow_review_update",
        {
            "name": "login-to-dao",
            "entry": "LoginController.login",
            "files": ["src/controller/LoginController.py", "src/service/UserService.py"],
            "variables": ["userId"],
            "evidence": "source -> sink",
            "next_step": "读取 UserService.load",
        },
    )
    assert flow["message"] == "flow review added"
    assert flow["data"]["files"] == ["src/controller/LoginController.py", "src/service/UserService.py"]
    updated = call(registry, "flow_review_update", {"name": "login-to-dao", "status": "suspicious"})
    assert updated["data"]["entry"] == "LoginController.login"
    closed = call(registry, "flow_review_update", {"name": "login-to-dao", "status": "confirmed"})
    assert closed["data"]["status"] == "confirmed"
    removed = call(registry, "flow_review_update", {"name": "login-to-dao", "status": "reviewed"})
    assert removed["message"] == "flow review closed and removed"
    assert call(registry, "flow_review_update", {"name": "login-to-dao", "status": "closed"})[
        "message"
    ] == "flow review already closed; nothing added"
    call(registry, "flow_review_update", {"name": "temporary"})
    deleted = call(registry, "flow_review_delete", {"name": "temporary"})
    assert deleted["message"] == "flow review deleted"
    assert call(registry, "flow_review_delete", {"name": "temporary"})["error"] == "flow not found"
    assert call(registry, "flow_review_delete", {})["error"] == "name is required"


def test_review_state_and_project_note(registry):
    call(registry, "todo_create", {"title": "任务"})
    call(registry, "project_note_update", {"note": "项目架构：登录入口 LoginController"})
    call(registry, "file_review_update", {"path": "src/controller/LoginController.py", "status": "reviewing"})
    call(registry, "variable_review_update", {"name": "userId"})
    call(registry, "flow_review_update", {"name": "flow"})
    state = call(registry, "review_state", {"limit": 1})
    assert state["ok"] is True
    data = state["data"]
    assert set(data) == {"todos", "findings", "project_note", "files", "variables", "flows", "audit"}
    assert data["project_note"] == {"note": "项目架构：登录入口 LoginController"}
    assert len(data["files"]) == 1
    assert len(data["variables"]) == 1
    assert len(data["flows"]) == 1
    assert data["audit"] == {"ended": False}
    assert call(registry, "project_note_update", {})["error"] == "note is required"
    replaced = call(registry, "project_note_update", {"note": "新笔记"})
    assert replaced["data"] == {"note": "新笔记"}
    assert call(registry, "review_state", {})["data"]["project_note"] == {"note": "新笔记"}


def test_report_finding_and_end_audit(registry):
    assert call(registry, "report_finding", {})["error"] == "title is required"
    assert call(registry, "report_finding", {"title": "x"})["error"] == "path is required"
    assert call(registry, "report_finding", {"title": "x", "path": "a.py"})["error"] == "evidence is required"
    finding = call(
        registry,
        "report_finding",
        {
            "title": "登录接口未鉴权",
            "path": "src/controller/LoginController.py",
            "line": 12,
            "evidence": "evidence",
            "impact": "impact",
            "recommendation": "fix",
            "cwe": "CWE-306",
        },
    )
    assert finding["ok"] is True
    assert finding["message"] == "finding recorded"
    assert finding["data"]["severity"] == "medium"
    assert finding["data"]["id"] == 1
    assert "finding_key" not in finding["data"]
    stored = registry.findings[0]
    assert finding_key(stored) == finding_key(stored.copy())
    changed = stored.copy()
    changed.title = "另一个标题"
    assert finding_key(stored) != finding_key(changed)
    assert call(registry, "end_audit", {"summary": "done", "vote": "reject"})["error"] == (
        "end_audit requires an explicit team approve vote"
    )
    ended = call(registry, "end_audit", {"summary": "done", "vote": "approve"})
    assert ended["ok"] is True
    assert ended["message"] == "audit ended"
    assert ended["data"]["summary"] == "done"
    assert ended["data"]["findings"][0]["title"] == "登录接口未鉴权"
    assert registry.audit.ended is True
    state = call(registry, "review_state", {})["data"]
    assert state["audit"] == {"ended": True, "summary": "done"}
