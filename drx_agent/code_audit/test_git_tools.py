"""Git inspection tool against a real temporary repository."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest

from drx_agent.code_audit.tools import Registry
from drx_agent.code_audit.tools.git import git_prompt

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git executable required")


def _remove_tree(path: str) -> None:
    """Remove a tree even when Git left read-only object files behind."""

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
        shutil.rmtree(path, onexc=handle)
    except TypeError:  # pragma: no cover - Python < 3.12
        shutil.rmtree(path, onerror=lambda func, target, info: handle(func, target, info[1]))
    for _ in range(10):
        if not os.path.exists(path):
            return
        shutil.rmtree(path, ignore_errors=True)
        time.sleep(0.05)


def _new_repo_root() -> str:
    """Unique per process and per test so parallel pytest runs cannot collide."""
    parent = os.path.join(tempfile.gettempdir(), "drx-audit-tests")
    os.makedirs(parent, exist_ok=True)
    root = os.path.join(parent, "%d-%s" % (os.getpid(), uuid.uuid4().hex))
    os.makedirs(root)
    return root


def _remove_repo_root(root: str) -> None:
    _remove_tree(root)
    try:
        os.rmdir(os.path.dirname(root))
    except OSError:
        pass


def git(root: str, *args: str) -> str:
    environment = os.environ.copy()
    environment.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    environment.setdefault("GIT_CONFIG_GLOBAL", os.path.join(root, ".empty-gitconfig"))
    environment.setdefault("GIT_CONFIG_COUNT", "0")
    environment.setdefault("GIT_CEILING_DIRECTORIES", os.path.dirname(root))
    environment.setdefault("GIT_AUTHOR_NAME", "Alice")
    environment.setdefault("GIT_AUTHOR_EMAIL", "alice@example.invalid")
    environment.setdefault("GIT_COMMITTER_NAME", environment["GIT_AUTHOR_NAME"])
    environment.setdefault("GIT_COMMITTER_EMAIL", environment["GIT_AUTHOR_EMAIL"])
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.path.join(root, ".no-hooks"),
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.autocrlf=false",
        ]
        + list(args),
        cwd=root,
        env=environment,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout.decode("utf-8", "replace").rstrip("\r\n")


def write(root: str, relative: str, content: str) -> None:
    path = os.path.join(root, relative.replace("/", os.sep))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def make_repo() -> tuple[str, list[str]]:
    root = _new_repo_root()
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(root, ".empty-gitconfig")
    os.environ["GIT_CONFIG_COUNT"] = "0"
    os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(root)
    git(root, "init", "--quiet")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    toplevel = git(root, "rev-parse", "--show-toplevel").replace("\\", "/").lower()
    assert toplevel == os.path.abspath(root).replace("\\", "/").lower()
    commits = []

    def commit(author: str, date: str, message: str) -> None:
        previous = os.environ.copy()
        os.environ.update(
            {
                "GIT_AUTHOR_NAME": author,
                "GIT_AUTHOR_EMAIL": author.lower() + "@example.invalid",
                "GIT_COMMITTER_NAME": author,
                "GIT_COMMITTER_EMAIL": author.lower() + "@example.invalid",
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            }
        )
        try:
            git(root, "add", "--all")
            git(root, "commit", "--quiet", "-m", message)
        finally:
            os.environ.clear()
            os.environ.update(previous)
        commits.append(git(root, "rev-parse", "HEAD"))

    write(root, "old.txt", "stable line\nneedle.initial\n")
    write(root, "deleted.txt", "deleted historical content\n")
    write(root, "[x].txt", "literal file needle.[x]\n")
    write(root, "x.txt", "decoy file needleZx\n")
    commit("Alice", "2024-01-01T12:00:00Z", "history-origin")
    git(root, "mv", "old.txt", "renamed.txt")
    git(root, "rm", "deleted.txt")
    write(root, "renamed.txt", "stable line\nneedle.initial\nintroduced-token\n")
    commit("Bob", "2024-01-02T12:00:00Z", "literal.[x] change")
    write(root, "renamed.txt", "latest line\nneedle.initial\n")
    commit("Alice", "2024-01-03T12:00:00Z", "literalZx change")
    write(root, "head.txt", "head-only content\n")
    commit("Bob", "2024-01-04T12:00:00Z", "history-head")
    git(root, "branch", "historical", commits[0])
    git(root, "tag", "release-history", commits[1])
    return root, commits


@pytest.fixture()
def repo():
    root, commits = make_repo()
    registry = Registry(root, 128000)
    try:
        yield root, commits, registry
    finally:
        registry.close()
        _remove_repo_root(root)


def inspect(registry, **args) -> dict:
    return json.loads(registry.call("git_inspect", args))


def output(registry, **args) -> str:
    result = inspect(registry, **args)
    assert result["ok"] is True, result
    return result["data"]["output"]


def test_git_prompt_and_status(repo):
    root, commits, registry = repo
    prompt = git_prompt(registry)
    assert "# 当前 Git 状态" in prompt
    assert "Git：可用" in prompt
    result = inspect(registry, action="status")
    assert result["ok"] is True
    assert result["data"]["command"][0] == "git"
    assert result["data"]["output"].startswith("## main")


def test_git_changed_files_and_diff_precedence(repo):
    root, commits, registry = repo
    write(root, "staged.txt", "STAGED_ONLY\n")
    git(root, "add", "--", "staged.txt")
    write(root, "head.txt", "UNSTAGED_ONLY\n")
    result = inspect(registry, action="changed_files", staged=True)
    assert result["data"]["files"] == ["staged.txt"]
    result = inspect(registry, action="changed_files", unstaged=True)
    assert result["data"]["files"] == ["head.txt"]
    staged = output(registry, action="diff", staged=True, unstaged=True, base=commits[0])
    assert "STAGED_ONLY" in staged
    assert "UNSTAGED_ONLY" not in staged
    historical = output(registry, action="diff", unstaged=True, base=commits[0])
    assert "deleted historical content" in historical


def test_git_log_filters_ranges_and_pagination(repo):
    root, commits, registry = repo
    assert "history-origin" in output(registry, action="log", ref="historical")
    assert "history-head" not in output(registry, action="log", ref="historical")
    ranged = output(registry, action="log", base=commits[0], head=commits[2])
    assert "literal.[x] change" in ranged
    assert "literalZx change" in ranged
    assert "history-origin" not in ranged
    literal = output(registry, action="log", query="literal.[x]")
    assert "literal.[x] change" in literal
    assert "literalZx change" not in literal
    alice = output(registry, action="log", author="Alice")
    assert "history-origin" in alice
    window = output(
        registry, action="log", since="2024-01-02T00:00:00Z", until="2024-01-02T23:59:59Z"
    )
    assert "literal.[x] change" in window
    assert "literalZx change" not in window
    changed = output(registry, action="log", search="introduced-token")
    assert "literal.[x] change" in changed
    assert "literalZx change" in changed
    first = output(registry, action="log", limit=1)
    assert "history-head" in first
    second = output(registry, action="log", limit=1, skip=1)
    assert "literalZx change" in second
    assert "history-head" not in second
    filtered = output(registry, action="log", author="Bob", limit=1, skip=1)
    assert "literal.[x] change" in filtered


def test_git_show_tree_grep_and_blame(repo):
    root, commits, registry = repo
    assert output(registry, action="show", ref=commits[0], path="old.txt").strip() == (
        "stable line\nneedle.initial"
    )
    assert output(registry, action="show", ref=commits[0], path="deleted.txt").strip() == (
        "deleted historical content"
    )
    tree = output(registry, action="tree", ref=commits[0])
    assert "deleted.txt" in tree
    assert "old.txt" in tree
    assert "head.txt" not in tree
    matches = output(registry, action="grep", ref=commits[0], query="deleted historical")
    assert "deleted.txt:1:deleted historical content" in matches
    assert output(registry, action="grep", query="needle.[x]").find("literal file") >= 0
    assert "decoy file" not in output(registry, action="grep", query="needle.[x]")
    blame = output(registry, action="blame", ref=commits[1], path="renamed.txt", line_start=1, line_end=1)
    assert "stable line" in blame
    assert "Alice" in blame
    assert "latest line" not in blame
    patch = output(registry, action="show", ref=commits[2], patch=True, context=1)
    assert "-stable line" in patch
    assert "+latest line" in patch
    summary = output(registry, action="show", ref=commits[2])
    assert "@@" not in summary
    follow = output(registry, action="log", ref=commits[1], path="renamed.txt", follow=True)
    assert "history-origin" in follow
    assert "literalZx change" not in follow


def test_git_refs_rev_parse_and_merge_base(repo):
    root, commits, registry = repo
    branches = output(registry, action="branches")
    assert "historical" in branches
    assert commits[0] in branches
    tags = output(registry, action="tags")
    assert "release-history" in tags
    assert commits[1] in tags
    parsed = output(registry, action="rev_parse", ref="release-history")
    assert parsed.strip() == commits[1]
    merged = output(registry, action="merge_base", base="release-history")
    assert merged.strip() == commits[1]


def test_git_rejects_unsafe_refs_paths_and_options(repo):
    root, commits, registry = repo
    marker = os.path.join(root, "injected-marker")
    for action in ("log", "show", "blame", "tree", "grep", "rev_parse"):
        for ref in ("--help", "--output=" + marker, "HEAD;touch " + marker):
            args = {"action": action, "ref": ref}
            if action == "blame":
                args["path"] = "renamed.txt"
            if action == "grep":
                args["query"] = "needle"
            result = inspect(registry, **args)
            assert result["ok"] is False, (action, ref)
    for path in ("../outside.txt", os.path.join(os.path.dirname(root), "outside.txt")):
        result = inspect(registry, action="show", path=path)
        assert result["ok"] is False
    assert not os.path.exists(marker)
    invalid_options = [
        {"action": "log", "skip": -1},
        {"action": "log", "follow": True},
        {"action": "log", "follow": True, "path": "renamed.txt", "all": True},
        {"action": "log", "ref": "HEAD", "base": commits[0]},
        {"action": "show", "path": "renamed.txt", "patch": True},
        {"action": "grep"},
        {"action": "merge_base"},
        {"action": "tree", "skip": 1},
    ]
    for args in invalid_options:
        assert inspect(registry, **args)["ok"] is False, args
    assert git(root, "status", "--porcelain") == ""


def test_git_literal_pathspecs_and_buffer_recovery(repo):
    root, commits, registry = repo
    result = inspect(registry, action="grep", query="file", path="[x].txt")
    assert result["ok"] is True
    assert "literal file" in result["data"]["output"]
    assert "decoy file" not in result["data"]["output"]
    content = "historical payload line\n" * 3000 + "unique-historical-tail\n"
    write(root, "large.txt", content)
    git(root, "add", "large.txt")
    git(root, "commit", "--quiet", "-m", "large historical blob")
    small = Registry(root, 1024)
    try:
        bounded, full = small.call_with_full_result(
            None, "git_inspect", {"action": "show", "path": "large.txt"}
        )
        page = json.loads(bounded)
        assert page["truncated"] is True
        assert page["buffer_id"]
        buffer_id = page["buffer_id"]
        recovered = page["preview"]
        while not page["eof"]:
            chunk = small.call(
                "read_tool_buffer",
                {"buffer_id": buffer_id, "offset": page["next_offset"], "limit": 700},
            )
            page = json.loads(chunk)
            assert page["ok"] is True
            recovered += page["content"]
        assert recovered == full
        parsed = json.loads(recovered)
        assert parsed["data"]["output"].rstrip("\r\n") == content.rstrip("\r\n")
    finally:
        small.close()


def test_git_follow_rejects_cross_workspace_rename():
    root = _new_repo_root()
    try:
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(root)
        git(root, "init", "--quiet")
        git(root, "symbolic-ref", "HEAD", "refs/heads/main")
        toplevel = git(root, "rev-parse", "--show-toplevel").replace("\\", "/").lower()
        assert toplevel == os.path.abspath(root).replace("\\", "/").lower()
        write(root, "outside.txt", "boundary-secret\n")
        write(root, "scope/keep.txt", "keep\n")
        git(root, "add", "--all")
        git(root, "commit", "--quiet", "-m", "history-origin")
        git(root, "mv", "outside.txt", "scope/inbound.txt")
        git(root, "commit", "--quiet", "-m", "history-cross-boundary")
        scoped = Registry(os.path.join(root, "scope"), 128000)
        try:
            result = json.loads(
                scoped.call(
                    "git_inspect",
                    {"action": "log", "path": "inbound.txt", "follow": True, "patch": True},
                )
            )
            assert result["ok"] is False
            assert "boundary-secret" not in json.dumps(result, ensure_ascii=False)
        finally:
            scoped.close()
        whole = Registry(root, 128000)
        try:
            result = json.loads(
                whole.call(
                    "git_inspect",
                    {"action": "log", "path": "scope/inbound.txt", "follow": True, "patch": True},
                )
            )
            assert result["ok"] is True
            assert "history-origin" in result["data"]["output"]
            assert "boundary-secret" in result["data"]["output"]
        finally:
            whole.close()
    finally:
        _remove_repo_root(root)

