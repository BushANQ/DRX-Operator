"""Read-only Git inspection tool."""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from .paths import safe_path

GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/@{}^~+-]+$")
GIT_TIMEOUT = 10.0

GIT_PREFIX = [
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c", "core.quotepath=false",
    "-c", "core.fsmonitor=false",
    "-c", "color.ui=false",
    "-c", "diff.submodule=short",
    "-c", "log.showSignature=false",
    "-c", "log.follow=false",
    "--no-pager",
]

GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_GLOB_PATHSPECS": "0",
    "GIT_NOGLOB_PATHSPECS": "0",
    "GIT_ICASE_PATHSPECS": "0",
}

ACTIONS = (
    "status", "changed_files", "diff", "log", "show", "blame", "branches",
    "tags", "tree", "grep", "rev_parse", "merge_base",
)


def _result(ok: bool, **kwargs):
    from .registry import Result

    return Result(ok=ok, **kwargs)


def _trim_git_output(output: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(output) <= limit:
        return output, False
    return output[:limit] + "\n...truncated...", True


def git_prompt(registry) -> str:
    parts = ["# 当前 Git 状态\n\n", "- 工作区：", registry.workspace.replace(os.sep, "/"), "\n"]
    if shutil.which("git") is None:
        parts.append("- Git：不可用（未找到 git 可执行文件）。不要调用 `git_inspect`。")
        return "".join(parts)
    inside, error = run_git_raw(registry, "rev-parse", "--is-inside-work-tree")
    if error is not None or inside.strip() != "true":
        parts.append("- Git：不可用（当前工作区不是 Git 仓库）。不要调用 `git_inspect`。")
        return "".join(parts)
    parts.append("- Git：可用。可以按需调用只读 `git_inspect`。\n")
    parts.append(
        "- 历史：log 支持 ref 或 base/head、limit/skip 分页、author/since/until/query（字面消息）、"
        "search（-S）、all/follow；show 可读历史 path，log/show 可用 patch。"
        "branches/tags/tree/grep/rev_parse/merge_base 用于只读导航。"
        "大结果请先 read_tool_buffer 读取完整内容，或缩小 query/path；下一次非 buffer 工具调用会清除旧 buffer。\n"
    )
    root, error = run_git_raw(registry, "rev-parse", "--show-toplevel")
    if error is None and root.strip():
        parts.append("- 仓库根目录：")
        parts.append(root.strip().replace(os.sep, "/"))
        parts.append("\n")
    status, error = run_git_raw(registry, "status", "--short", "--branch")
    if error is not None or not status.strip():
        parts.append("- 当前状态：clean")
        return "".join(parts)
    status, _ = _trim_git_output(status, 4000)
    parts.append("- 当前状态：\n```text\n")
    parts.append(status)
    parts.append("\n```")
    return "".join(parts)


def require_git_repo(registry) -> str | None:
    if shutil.which("git") is None:
        return "git unavailable: executable not found"
    inside, error = run_git_raw(registry, "rev-parse", "--is-inside-work-tree")
    if error is not None:
        return "git unavailable: workspace is not a git repository"
    if inside.strip() != "true":
        return "git unavailable: workspace is not inside a git work tree"
    return None


def run_git(registry, *args: str) -> tuple[str, str | None]:
    error = require_git_repo(registry)
    if error is not None:
        return "", error
    return run_git_raw(registry, *args)


def run_git_raw(registry, *args: str) -> tuple[str, str | None]:
    if shutil.which("git") is None:
        return "", "git unavailable: executable not found"
    environment = os.environ.copy()
    environment.update(GIT_ENV)
    argv = ["git"] + GIT_PREFIX + list(args)
    try:
        completed = subprocess.run(
            argv,
            cwd=registry.workspace,
            env=environment,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "", "git command timed out"
    except OSError as exc:
        return "", str(exc)
    output = completed.stdout + completed.stderr
    text = output.decode("utf-8", errors="surrogateescape")
    if args and args[0] == "cat-file":
        pass
    else:
        text = text.rstrip("\r\n")
    if completed.returncode != 0:
        if (
            args
            and args[0] == "grep"
            and completed.returncode == 1
            and text == ""
        ):
            return "", None
        if not text.strip():
            return text, "git command failed (exit %d)" % completed.returncode
        return text, text
    return text, None


def validate_git_ref(ref: str) -> str | None:
    value = (ref or "").strip()
    if not value:
        return "git ref is required"
    if value.startswith("-") or any(ch in value for ch in " \t\r\n") or not GIT_REF_RE.match(value):
        return 'invalid git ref: "%s"' % value
    return None


def git_workspace_path(path: str) -> str:
    return path or "."


def git_path_arg(registry, input_path: str) -> str:
    if not input_path:
        return ""
    if "\x00" in input_path:
        raise ValueError("git path contains NUL")
    absolute = safe_path(registry.workspace, input_path)
    return os.path.relpath(absolute, registry.workspace).replace(os.sep, "/")


def git_repo_path(registry, path: str) -> str:
    prefix, error = run_git(registry, "rev-parse", "--show-prefix")
    if error is not None:
        raise ValueError(error)
    return os.path.normpath(prefix.rstrip("\r\n") + path).replace(os.sep, "/")


def git_inspect(registry, args: dict):
    if not isinstance(args, dict):
        return _result(False, error="invalid arguments")
    error = require_git_repo(registry)
    if error is not None:
        return _result(False, error=error)
    from . import git_history

    validation = git_history.validate(args)
    if validation is not None:
        return _result(False, error=validation)
    normalized = git_history.normalize(args)
    try:
        command_args = command_args_for(registry, normalized)
    except ValueError as exc:
        return _result(False, error=str(exc))
    output, error = run_git(registry, *command_args)
    if error is not None:
        return _result(False, error=error)
    data = {"action": normalized["action"], "command": ["git"] + command_args}
    if normalized["action"] == "changed_files":
        data["files"] = git_history.parse_changed_files(
            output, "--name-only" in command_args
        )
    else:
        data["output"] = output
    return _result(True, data=data)


def command_args_for(registry, args: dict) -> list[str]:
    from . import git_history

    path = git_path_arg(registry, args["path"])
    action = args["action"]
    if action == "status":
        return ["status", "--short", "--branch", "--", git_workspace_path(path)]
    if action == "changed_files":
        return changed_files_args(args, git_workspace_path(path))
    if action == "diff":
        return diff_args(args, git_workspace_path(path))
    if action == "log":
        return git_history.log_args(registry, args, path)
    if action == "show":
        return show_args(registry, args, path)
    if action == "blame":
        return blame_args(registry, args, path)
    if action == "branches":
        command = [
            "for-each-ref",
            "--format=%(refname:short)\t%(objectname)\t%(refname)",
            "refs/heads/",
        ]
        if args["all"]:
            command.append("refs/remotes/")
        return command
    if action == "tags":
        return [
            "for-each-ref",
            "--format=%(refname:short)\t%(objectname)\t%(refname)",
            "refs/tags/",
        ]
    if action in ("tree", "grep"):
        ref, error = git_history.commit_ref(registry, args["ref"])
        if error is not None:
            raise ValueError(error)
        if action == "tree":
            return ["ls-tree", "-r", "--full-name", ref, "--", git_workspace_path(path)]
        return [
            "grep", "--no-textconv", "-n", "-F", "-e", args["query"], ref, "--",
            git_workspace_path(path),
        ]
    if action == "rev_parse":
        error = validate_git_ref(args["ref"])
        if error is not None:
            raise ValueError(error)
        return ["rev-parse", "--verify", "--end-of-options", args["ref"] + "^{commit}"]
    if action == "merge_base":
        base, error = git_history.commit_ref(registry, args["base"])
        if error is not None:
            raise ValueError(error)
        head, error = git_history.commit_ref(registry, args["head"])
        if error is not None:
            raise ValueError(error)
        return ["merge-base", base, head]
    raise ValueError("unsupported git action: " + action)


def changed_files_args(args: dict, path: str) -> list[str]:
    if args["staged"]:
        command = ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "--cached"]
        if path:
            command.extend(["--", path])
        return command
    if args["unstaged"]:
        command = ["diff", "--no-ext-diff", "--no-textconv", "--name-only"]
        if path:
            command.extend(["--", path])
        return command
    if args["base"]:
        error = validate_git_ref(args["base"])
        if error is not None:
            raise ValueError(error)
        error = validate_git_ref(args["head"])
        if error is not None:
            raise ValueError(error)
        command = [
            "diff", "--no-ext-diff", "--no-textconv", "--name-only",
            args["base"] + ".." + args["head"],
        ]
        if path:
            command.extend(["--", path])
        return command
    command = ["status", "--porcelain", "-uall"]
    if path:
        command.extend(["--", path])
    return command


def diff_args(args: dict, path: str) -> list[str]:
    command = [
        "diff", "--no-ext-diff", "--no-textconv", "--unified=%d" % args["context"]
    ]
    if args["staged"]:
        command.append("--cached")
    elif args["base"]:
        error = validate_git_ref(args["base"])
        if error is not None:
            raise ValueError(error)
        error = validate_git_ref(args["head"])
        if error is not None:
            raise ValueError(error)
        command.append(args["base"] + ".." + args["head"])
    elif not args["unstaged"]:
        command.append("HEAD")
    if path:
        command.extend(["--", path])
    return command


def show_args(registry, args: dict, path: str) -> list[str]:
    from . import git_history

    ref, error = git_history.commit_ref(registry, args["ref"])
    if error is not None:
        raise ValueError(error)
    if not path:
        command = ["show", "--no-ext-diff", "--no-textconv", "--stat", "--oneline", "--decorate"]
        if args["patch"]:
            command.extend(["--patch", "--unified=%d" % args["context"]])
        return command + [ref, "--", "."]
    repo_path = git_repo_path(registry, path)
    return ["cat-file", "blob", ref + ":" + repo_path]


def blame_args(registry, args: dict, path: str) -> list[str]:
    from . import git_history

    if not path:
        raise ValueError("path is required for blame")
    ref, error = git_history.commit_ref(registry, args["ref"])
    if error is not None:
        raise ValueError(error)
    command = ["blame", "--no-textconv", "--date=short"]
    if args["line_start"] > 0:
        line_end = args["line_end"]
        if line_end < args["line_start"]:
            line_end = args["line_start"]
        command.extend(["-L", "%d,%d" % (args["line_start"], line_end)])
    command.extend([ref, "--", path])
    return command
