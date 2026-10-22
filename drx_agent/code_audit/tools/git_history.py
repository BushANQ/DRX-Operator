"""Git argument validation, log ranges and history helpers."""

from __future__ import annotations

import os

from .git import run_git, validate_git_ref

ALLOWED = {
    "ref/commit": ("log", "show", "blame", "tree", "grep", "rev_parse"),
    "base/head": ("log", "diff", "changed_files", "merge_base"),
    "path": ("status", "changed_files", "diff", "log", "show", "blame", "tree", "grep"),
    "limit/skip": ("log",),
    "author/since/until/search": ("log",),
    "query": ("log", "grep"),
    "all": ("log", "branches"),
    "follow": ("log",),
    "patch": ("log", "show"),
    "context": ("diff", "log", "show"),
    "staged/unstaged": ("diff", "changed_files"),
    "line_start/line_end": ("blame",),
}


def _int(args: dict, key: str) -> int:
    value = args.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _bool(args: dict, key: str) -> bool:
    return bool(args.get(key, False))


def _text(args: dict, key: str) -> str:
    value = args.get(key, "")
    return value if isinstance(value, str) else ""


def normalize(args: dict) -> dict:
    action = _text(args, "action").strip().lower() or "status"
    normalized = {
        "action": action,
        "base": _text(args, "base"),
        "head": _text(args, "head"),
        "ref": _text(args, "ref"),
        "commit": _text(args, "commit"),
        "path": _text(args, "path"),
        "line_start": _int(args, "line_start"),
        "line_end": _int(args, "line_end"),
        "limit": _int(args, "limit"),
        "skip": _int(args, "skip"),
        "author": _text(args, "author"),
        "since": _text(args, "since"),
        "until": _text(args, "until"),
        "query": _text(args, "query"),
        "search": _text(args, "search"),
        "all": _bool(args, "all"),
        "follow": _bool(args, "follow"),
        "patch": _bool(args, "patch"),
        "context": _int(args, "context"),
        "staged": _bool(args, "staged"),
        "unstaged": _bool(args, "unstaged"),
    }
    if normalized["limit"] <= 0:
        normalized["limit"] = 50
    if normalized["limit"] > 200:
        normalized["limit"] = 200
    if normalized["context"] < 0:
        normalized["context"] = 0
    if not normalized["ref"]:
        normalized["ref"] = normalized["commit"]
    if not normalized["ref"]:
        normalized["ref"] = "HEAD"
    if not normalized["head"]:
        normalized["head"] = "HEAD"
    return normalized


def validate(args: dict) -> str | None:
    action = _text(args, "action").strip().lower() or "status"
    supplied = {
        "ref/commit": bool(_text(args, "ref") or _text(args, "commit")),
        "base/head": bool(_text(args, "base") or _text(args, "head")),
        "path": bool(_text(args, "path")),
        "limit/skip": _int(args, "limit") != 0 or _int(args, "skip") != 0,
        "author/since/until/search": bool(
            _text(args, "author")
            or _text(args, "since")
            or _text(args, "until")
            or _text(args, "search")
        ),
        "query": bool(_text(args, "query")),
        "all": _bool(args, "all"),
        "follow": _bool(args, "follow"),
        "patch": _bool(args, "patch"),
        "context": _int(args, "context") != 0,
        "staged/unstaged": _bool(args, "staged") or _bool(args, "unstaged"),
        "line_start/line_end": _int(args, "line_start") != 0 or _int(args, "line_end") != 0,
    }
    for field, is_supplied in supplied.items():
        if not is_supplied:
            continue
        if action not in ALLOWED[field]:
            return "%s is not supported for git action %s" % (field, action)
    if _int(args, "skip") < 0:
        return "skip must be nonnegative"
    if _int(args, "context") != 0 and action in ("log", "show") and not _bool(args, "patch"):
        return "context requires patch for %s" % action
    ref = _text(args, "ref")
    commit = _text(args, "commit")
    if ref and commit and ref != commit:
        return "ref and commit must identify the same revision when both are supplied"
    head = _text(args, "head")
    base = _text(args, "base")
    if action == "log" and head and not base:
        return "head requires base"
    if action == "log":
        if base and (ref or commit):
            return "log accepts ref/commit or base/head, not both"
        if _bool(args, "all") and (
            _bool(args, "follow") or ref or commit or base or head
        ):
            return "log all cannot be combined with follow or explicit revisions"
        if _bool(args, "follow") and (not _text(args, "path") or _text(args, "path") == "."):
            return "follow requires one file path"
    if action == "show" and _bool(args, "patch") and _text(args, "path"):
        return "show with path reads a historical file; patch requires no path"
    if action == "grep" and not _text(args, "query"):
        return "query is required for grep"
    if action == "merge_base" and not base:
        return "base is required for merge_base"
    line_start = _int(args, "line_start")
    line_end = _int(args, "line_end")
    if line_start < 0 or line_end < 0 or (line_end > 0 and line_start == 0):
        return "blame lines require line_start > 0 and nonnegative line_end"
    for key in ("author", "since", "until", "query", "search"):
        if "\x00" in _text(args, key):
            return "git query fields must not contain NUL"
    return None


def commit_ref(registry, ref: str) -> tuple[str, str | None]:
    error = validate_git_ref(ref)
    if error is not None:
        return "", error
    return run_git(registry, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")


def log_args(registry, args: dict, path: str) -> list[str]:
    command = [
        "log",
        "--oneline",
        "--decorate",
        "--no-ext-diff",
        "--no-textconv",
        "--fixed-strings",
        "-n",
        str(args["limit"]),
        "--skip=" + str(args["skip"]),
    ]
    if args["author"]:
        command.append("--author=" + args["author"])
    if args["since"]:
        command.append("--since=" + args["since"])
    if args["until"]:
        command.append("--until=" + args["until"])
    if args["query"]:
        command.append("--grep=" + args["query"])
    if args["search"]:
        command.append("-S" + args["search"])
    if args["patch"]:
        command.extend(["--patch", "--unified=%d" % args["context"]])
    revision = ""
    if args["all"]:
        command.append("--all")
    else:
        ref = args["head"] if args["base"] else args["ref"]
        head, error = commit_ref(registry, ref)
        if error is not None:
            raise ValueError(error)
        revision = head
        if args["base"]:
            base, error = commit_ref(registry, args["base"])
            if error is not None:
                raise ValueError(error)
            revision = base + ".." + head
    if args["follow"]:
        if not path or path == ".":
            raise ValueError("follow requires one file path")
        follow_boundary(registry, revision, path)
        command.extend(["--follow", "--find-renames"])
    if revision:
        command.append(revision)
    if not path:
        prefix, error = run_git(registry, "rev-parse", "--show-prefix")
        if error is not None:
            raise ValueError(error)
        if prefix:
            path = "."
    if path:
        command.extend(["--", path])
    else:
        command.append("--")
    return command


def follow_boundary(registry, revision: str, path: str) -> None:
    prefix, error = run_git(registry, "rev-parse", "--show-prefix")
    if error is not None:
        raise ValueError(error)
    command = [
        "log", "--follow", "--find-renames", "--no-ext-diff", "--no-textconv",
        "--no-relative", "--format=", "--name-status", "-z",
    ]
    if not prefix:
        command.extend(["-n", "1"])
    command.extend([revision, "--", path])
    output, error = run_git(registry, *command)
    if error is not None:
        raise ValueError(
            "cannot verify follow workspace boundary: %s; open the repository root for full history"
            % error
        )
    fields = output.split("\x00")
    first = True
    index = 0
    while index < len(fields):
        status = fields[index].lstrip("\r\n")
        index += 1
        if not status:
            continue
        count = 2 if status[0] in "RC" else 1
        if status[0] not in "ACDMRTUXB" or index + count > len(fields):
            raise ValueError(
                "cannot verify follow workspace boundary; open the repository root for full history"
            )
        if first and fields[index + count - 1] != prefix + path:
            raise ValueError("follow requires one file path, not a directory")
        first = False
        for name in fields[index:index + count]:
            if not name or (prefix and not name.startswith(prefix)):
                raise ValueError(
                    "follow crosses the workspace boundary; open the repository root for full history"
                )
        index += count


def parse_changed_files(output: str, name_only: bool) -> list[str]:
    files: list[str] = []
    for raw_line in output.rstrip("\r\n").split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if name_only:
            files.append(line.strip().replace(os.sep, "/"))
            continue
        if len(line) <= 3:
            continue
        path = line[3:].strip()
        marker = path.rfind(" -> ")
        if marker >= 0:
            path = path[marker + 4:]
        files.append(path.replace(os.sep, "/"))
    return files


def trim_git_output(output: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(output) <= limit:
        return output, False
    return output[:limit] + "\n...truncated...", True
