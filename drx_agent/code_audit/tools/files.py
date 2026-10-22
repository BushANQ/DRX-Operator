"""Workspace file listing, reading and content search tools."""

from __future__ import annotations

import fnmatch
import os
import re

from .paths import safe_path, normalize_inventory_path

MAX_SCAN_TOKEN = 1024 * 1024

INCLUDE_SUFFIXES = (
    ".go", ".java", ".xml", ".sql", ".js", ".ts", ".tsx", ".jsx", ".vue", ".py",
    ".php", ".cs", ".cpp", ".c", ".h", ".yaml", ".yml", ".properties",
)


def _result(ok: bool, **kwargs):
    from .registry import Result

    return Result(ok=ok, **kwargs)


def _NULL_DATA():
    from .registry import NULL_DATA

    return NULL_DATA


def _depth(path: str) -> int:
    clean = os.path.normpath(path)
    if clean in ("/", "."):
        return 0
    return clean.count(os.sep)


def _match_pattern_insensitive(pattern: str, value: str) -> bool:
    pattern = (pattern or "").strip().replace("\\", "/")
    if not pattern:
        return True
    if fnmatch.fnmatchcase(value, pattern):
        return True
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())


def list_files(registry, args: dict):
    if not isinstance(args, dict):
        return _result(False, error="invalid arguments")
    root_arg = args.get("root", "")
    pattern = args.get("pattern", "")
    max_depth = args.get("max_depth", 0)
    include_hidden = bool(args.get("include_hidden", False))
    limit = args.get("limit", 0)
    if not isinstance(root_arg, str) or not isinstance(pattern, str):
        return _result(False, error="invalid arguments")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        max_depth = 0
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = 0
    if limit <= 0:
        limit = 500
    try:
        root = safe_path(registry.workspace, root_arg)
    except ValueError as exc:
        return _result(False, error=str(exc))
    base_depth = _depth(root)
    out: list[dict] = []
    truncated = False

    def walk(directory: str) -> None:
        nonlocal truncated
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            path = entry.path
            name = entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not include_hidden and name.startswith(".") and path != root:
                if is_dir:
                    continue
                continue
            if max_depth > 0 and _depth(path) - base_depth > max_depth:
                if is_dir:
                    continue
                continue
            if path == root:
                continue
            if pattern and not _match_pattern_insensitive(pattern, name):
                if is_dir:
                    continue
                continue
            rel = os.path.relpath(path, registry.workspace).replace(os.sep, "/")
            size = 0
            try:
                size = entry.stat(follow_symlinks=False).st_size if not is_dir else 0
            except OSError:
                size = 0
            out.append({"path": rel, "is_dir": is_dir, "size": size})
            if len(out) >= limit:
                truncated = True
                return
            if is_dir:
                walk(path)

    walk(root)
    out.sort(key=lambda item: item["path"])
    return _result(True, data=out, truncated=truncated)


def resolve_readable_file(registry, input_path: str) -> tuple[str, str]:
    value = (input_path or "").strip().strip("\"'")
    if not value:
        raise ValueError("path is required")
    existing = registry.existing_file(value)
    if existing is not None:
        return existing
    for candidate in _path_recovery_candidates(registry, value):
        existing = registry.existing_file(candidate)
        if existing is not None:
            return existing
    matches = _inventory_base_matches(registry, value)
    if len(matches) == 1:
        existing = registry.existing_file(matches[0])
        if existing is not None:
            return existing
    if len(matches) > 1:
        limited = matches[:8]
        suffix = ""
        if len(matches) > 8:
            suffix = ", ...and %d more" % (len(matches) - 8)
        raise ValueError(
            "path not found and basename is ambiguous: %s; candidates: %s"
            % (value, ", ".join(limited) + suffix)
        )
    raise ValueError(
        "path not found in workspace: %s. Use an exact relative path from "
        "review_state/list_files/search_content, not a guessed absolute path" % value
    )


def _path_recovery_candidates(registry, input_path: str) -> list[str]:
    normalized = (input_path or "").strip().replace("\\", "/")
    workspace_base = os.path.basename(registry.workspace.rstrip(os.sep))
    candidates: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip().replace("\\", "/")
        if not candidate:
            return
        if any(existing.lower() == candidate.lower() for existing in candidates):
            return
        candidates.append(candidate)

    if workspace_base:
        needle = workspace_base.lower() + "/"
        index = normalized.lower().rfind(needle)
        if index >= 0:
            add(normalized[index + len(needle):])
    for marker in ("src/", "app/", "internal/", "pkg/", "cmd/", "lib/", "resources/", "config/", "configs/"):
        index = normalized.lower().find(marker)
        if index >= 0:
            add(normalized[index:])
    return candidates


def _inventory_base_matches(registry, input_path: str) -> list[str]:
    base = os.path.basename((input_path or "").strip().replace("\\", "/"))
    if not base or base in (".", "/"):
        return []
    return [
        entry["path"]
        for entry in registry.inventory
        if os.path.basename(entry["path"]).lower() == base.lower()
    ]


def _read_lines(path: str) -> list[str]:
    with open(path, "rb") as handle:
        data = handle.read()
    if not data:
        return []
    raw = data.split(b"\n")
    lines = []
    for item in raw:
        if item.endswith(b"\r"):
            item = item[:-1]
        lines.append(item.decode("utf-8", errors="surrogateescape"))
    if lines and lines[-1] == "" and data.endswith(b"\n"):
        lines.pop()
    return lines


def read_file(registry, args: dict):
    if not isinstance(args, dict):
        return _result(False, error="invalid arguments")
    path_arg = args.get("path", "")
    if not isinstance(path_arg, str):
        return _result(False, error="path is required")
    if not path_arg:
        return _result(False, error="path is required")
    offset = args.get("offset", 0)
    limit = args.get("limit", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset <= 0:
        offset = 1
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        limit = 120
    try:
        absolute, relative = resolve_readable_file(registry, path_arg)
    except ValueError as exc:
        return _result(False, error=str(exc))
    try:
        lines = _read_lines(absolute)
    except OSError as exc:
        return _result(False, error=str(exc))
    selected = []
    for index, text in enumerate(lines, start=1):
        if index < offset:
            continue
        if len(selected) >= limit:
            break
        selected.append({"line": index, "text": text})
    total = len(lines)
    truncated = total > offset - 1 + limit
    return _result(
        True,
        data={
            "path": relative,
            "offset": offset,
            "limit": limit,
            "total_lines": total,
            "lines": selected,
        },
        truncated=truncated,
    )


def _normalize_search_mode(mode: str) -> str:
    value = (mode or "").strip().lower()
    if value in ("", "literal", "text", "contains", "substring", "exact"):
        return "literal"
    if value in ("regex", "regexp", "regular_expression", "regular-expression"):
        return "regex"
    if value in ("fuzzy", "fuzz", "keyword", "keywords"):
        return "fuzzy"
    return "literal"


def _looks_like_include_pattern(pattern: str) -> bool:
    value = (pattern or "").strip().replace("\\", "/")
    if not value:
        return False
    if "/" in value:
        return True
    if "*" in value or "?" in value:
        return True
    return value.lower().endswith(INCLUDE_SUFFIXES)


def _split_pattern_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\n\t]", value or "") if part.strip()]


def _glob_regex(pattern: str):
    index = 0
    out = ["^"]
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                if index + 2 < len(pattern) and pattern[index + 2] == "/":
                    out.append("(?:.*/)?")
                    index += 3
                    continue
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append("$")
    return re.compile("".join(out))


def _match_glob(pattern: str, value: str) -> bool:
    try:
        return bool(_glob_regex(pattern).match(value))
    except re.error:
        return False


def _match_include_pattern_exact(include: str, rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    if "/" in include:
        return _match_glob(include, rel)
    if _match_glob(include, base):
        return True
    return _match_glob(include, rel)


def _match_include_pattern(include: str, rel: str) -> bool:
    include = (include or "").strip().replace("\\", "/")
    if not include:
        return True
    if _match_include_pattern_exact(include, rel):
        return True
    return _match_include_pattern_exact(include.lower(), rel.lower())


def _include_patterns(args: dict) -> list[str]:
    patterns: list[str] = []
    for value in list(args.get("includes") or []) + [args.get("include", "")]:
        if not isinstance(value, str):
            continue
        patterns.extend(_split_pattern_list(value))
    return patterns


def _byte_index(text: str, needle: str, case_insensitive: bool) -> int:
    haystack = text.lower() if case_insensitive else text
    target = needle.lower() if case_insensitive else needle
    index = haystack.find(target)
    if index < 0:
        return -1
    return len(text[:index].encode("utf-8"))


def _fuzzy_tokens(query: str) -> list[str]:
    parts = re.split(r"[^a-z0-9_]+", (query or "").lower())
    seen = set()
    tokens = []
    for part in parts:
        if len(part) < 2 or part in seen:
            continue
        seen.add(part)
        tokens.append(part)
    return tokens


def _fuzzy_keyword_index(line: str, query: str) -> int:
    tokens = _fuzzy_tokens(query)
    if len(tokens) <= 1:
        return -1
    best = -1
    matched = 0
    for token in tokens:
        index = line.find(token)
        if index < 0:
            continue
        matched += 1
        if best < 0 or index < best:
            best = index
    if matched == 0:
        return -1
    return best


def _fuzzy_index(line: str, query: str) -> int:
    if not query:
        return -1
    keyword = _fuzzy_keyword_index(line, query)
    if keyword >= 0:
        return keyword
    start = -1
    j = 0
    offset = 0
    for char in line:
        if j < len(query) and (ord(char) & 0xFF) == ord(query[j]):
            if start < 0:
                start = offset
            j += 1
            if j == len(query):
                return start
        offset += len(char.encode("utf-8"))
    return -1


def _match_column(line: str, query: str, mode: str, regex, case_insensitive: bool) -> int:
    if mode == "regex":
        match = regex.search(line)
        if match is None:
            return 0
        return len(line[: match.start()].encode("utf-8")) + 1
    if mode == "fuzzy":
        return _fuzzy_index(line, query) + 1
    index = _byte_index(line, query, case_insensitive)
    return index + 1


def _line_column_at(content: str, char_offset: int) -> tuple[int, int]:
    prefix = content[:char_offset]
    line = prefix.count("\n") + 1
    line_start = prefix.rfind("\n") + 1
    byte_offset = len(content[:char_offset].encode("utf-8"))
    byte_line_start = len(content[:line_start].encode("utf-8"))
    return line, byte_offset - byte_line_start + 1


def _line_snippet_at(content: str, char_offset: int) -> str:
    start = content.rfind("\n", 0, char_offset) + 1
    end = content.find("\n", char_offset)
    if end < 0:
        end = len(content)
    return content[start:end]


def _search_file(registry, path: str, query: str, mode: str, regex, limit: int, case_insensitive: bool):
    if limit <= 0:
        return []
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return []
    content = data.decode("utf-8", errors="surrogateescape")
    relative = os.path.relpath(path, registry.workspace).replace(os.sep, "/")
    hits: list[dict] = []
    if mode == "regex":
        for match in regex.finditer(content):
            if match.start() == match.end():
                continue
            line, column = _line_column_at(content, match.start())
            hits.append(
                {
                    "path": relative,
                    "line": line,
                    "column": column,
                    "snippet": _line_snippet_at(content, match.start()).strip(),
                }
            )
            if len(hits) >= limit:
                break
        return hits
    for line_no, line in enumerate(content.split("\n"), start=1):
        column = _match_column(line, query, mode, regex, case_insensitive)
        if column == 0:
            continue
        hits.append(
            {
                "path": relative,
                "line": line_no,
                "column": column,
                "snippet": line.strip(),
            }
        )
        if len(hits) >= limit:
            break
    return hits


def search_content(registry, args: dict):
    if not isinstance(args, dict):
        return _result(False, error="invalid arguments")
    normalized = _normalize_search_args(args)
    query = normalized["query"]
    if not query:
        return _result(False, error="query is required")
    mode = normalized["mode"]
    case_insensitive = normalized["case_insensitive"]
    limit = normalized["limit"]
    try:
        root = safe_path(registry.workspace, normalized["root"])
    except ValueError as exc:
        return _result(False, error=str(exc))
    regex = None
    if mode == "regex":
        pattern = query
        if case_insensitive and not pattern.startswith("(?i)"):
            pattern = "(?i)" + pattern
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return _result(False, error=str(exc))
    include_patterns = normalized["include_patterns"]
    hits: list[dict] = []
    truncated = False

    def walk(directory: str) -> None:
        nonlocal truncated
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            path = entry.path
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                if entry.name.startswith(".") and path != root:
                    continue
                walk(path)
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if include_patterns and not any(
                _match_include_pattern(pattern, relative) for pattern in include_patterns
            ):
                continue
            file_hits = _search_file(
                registry, path, query, mode, regex, limit - len(hits), case_insensitive
            )
            hits.extend(file_hits)
            if len(hits) >= limit:
                truncated = True
                return

    walk(root)
    data = hits if hits else _NULL_DATA()
    return _result(True, data=data, truncated=truncated)


def _normalize_search_args(args: dict) -> dict:
    def first_non_empty(*values) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value
        return ""

    query = first_non_empty(
        args.get("query", ""),
        args.get("keyword", ""),
        args.get("text", ""),
        args.get("content", ""),
        args.get("needle", ""),
        args.get("term", ""),
    )
    pattern = args.get("pattern", "") if isinstance(args.get("pattern", ""), str) else ""
    include = args.get("include", "") if isinstance(args.get("include", ""), str) else ""
    file_pattern = args.get("file_pattern", "") if isinstance(args.get("file_pattern", ""), str) else ""
    mode = args.get("mode", "") if isinstance(args.get("mode", ""), str) else ""
    if not query and pattern.strip():
        if _looks_like_include_pattern(pattern):
            include = include or pattern
        else:
            query = pattern.strip()
            if not mode:
                mode = "regex"
    elif not include and not file_pattern and _looks_like_include_pattern(pattern):
        include = pattern
    root = first_non_empty(
        args.get("root", ""),
        args.get("path", ""),
        args.get("dir", ""),
        args.get("directory", ""),
    )
    if not include:
        include = file_pattern
    includes = args.get("includes") if isinstance(args.get("includes"), list) else []
    file_patterns = args.get("file_patterns") if isinstance(args.get("file_patterns"), list) else []
    if not includes:
        includes = file_patterns
    limit = args.get("limit", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        limit = 100
    case_sensitive = bool(args.get("case_sensitive", False))
    case_insensitive_arg = args.get("case_insensitive")
    if case_sensitive:
        case_insensitive = False
    elif case_insensitive_arg is None:
        case_insensitive = True
    else:
        case_insensitive = bool(case_insensitive_arg)
    combined = {"include": include, "includes": includes}
    return {
        "query": query,
        "mode": _normalize_search_mode(mode),
        "root": root,
        "limit": limit,
        "case_insensitive": case_insensitive,
        "include_patterns": _include_patterns(combined),
    }
