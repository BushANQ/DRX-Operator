"""File, variable and cross-file flow review state."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

CLOSED_FLOW_STATUSES = ("reviewed", "benign", "done", "closed")


@dataclass
class FileReview:
    path: str
    status: str
    note: str = ""

    def to_dict(self) -> dict:
        data = {"path": self.path, "status": self.status}
        if self.note:
            data["note"] = self.note
        return data

    def copy(self) -> "FileReview":
        return FileReview(path=self.path, status=self.status, note=self.note)


@dataclass
class VariableReview:
    name: str
    path: str = ""
    status: str = "tracking"
    note: str = ""

    def to_dict(self) -> dict:
        data = {"name": self.name}
        if self.path:
            data["path"] = self.path
        data["status"] = self.status
        if self.note:
            data["note"] = self.note
        return data

    def copy(self) -> "VariableReview":
        return VariableReview(
            name=self.name, path=self.path, status=self.status, note=self.note
        )


@dataclass
class FlowReview:
    name: str
    status: str = "tracking"
    entry: str = ""
    files: list = field(default_factory=list)
    variables: list = field(default_factory=list)
    evidence: str = ""
    next_step: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        data = {"name": self.name, "status": self.status}
        if self.entry:
            data["entry"] = self.entry
        if self.files:
            data["files"] = list(self.files)
        if self.variables:
            data["variables"] = list(self.variables)
        if self.evidence:
            data["evidence"] = self.evidence
        if self.next_step:
            data["next_step"] = self.next_step
        if self.note:
            data["note"] = self.note
        return data

    def copy(self) -> "FlowReview":
        return FlowReview(
            name=self.name,
            status=self.status,
            entry=self.entry,
            files=list(self.files),
            variables=list(self.variables),
            evidence=self.evidence,
            next_step=self.next_step,
            note=self.note,
        )


def normalize_selector_dir(directory: str) -> str:
    value = (directory or "").strip().replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def normalize_selector_suffix(suffix: str) -> str:
    value = (suffix or "").strip()
    if not value or value == "*":
        return value
    if not value.startswith(".") and not value.startswith("*"):
        value = "." + value
    return value.lstrip("*").lower()


def matches_file_selectors(path: str, dirs: list[str], suffixes: list[str], patterns: list[str]) -> bool:
    if not dirs and not suffixes and not patterns:
        return False
    return (
        _matches_any_dir(path, dirs)
        or _matches_any_suffix(path, suffixes)
        or _matches_any_pattern(path, patterns)
    )


def _matches_any_dir(path: str, dirs: list[str]) -> bool:
    for directory in dirs:
        if directory in ("", "."):
            return True
        if path == directory or path.startswith(directory + "/"):
            return True
    return False


def _matches_any_suffix(path: str, suffixes: list[str]) -> bool:
    lower = path.lower()
    for suffix in suffixes:
        if suffix == "*":
            return True
        if suffix and lower.endswith(suffix):
            return True
    return False


def _glob_match(pattern: str, path: str) -> bool:
    index = 0
    regex = ["^"]
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            regex.append("[^/]*")
            index += 1
        elif char == "?":
            regex.append("[^/]")
            index += 1
        else:
            regex.append(re.escape(char))
            index += 1
    regex.append("$")
    return bool(re.match("".join(regex), path))


def _matches_any_pattern(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        value = (pattern or "").strip().replace("\\", "/")
        if not value:
            continue
        if value == "*":
            return True
        if _glob_match(value, path):
            return True
    return False


def _has_selectors(args: dict) -> bool:
    return any(
        args.get(key)
        for key in ("dir", "dirs", "suffix", "suffixes", "pattern", "patterns")
    )


def _normalize_items(args: dict):
    items = args.get("items")
    if isinstance(items, list) and items:
        out = []
        for item in items:
            if not isinstance(item, dict):
                out.append({"path": "", "status": args.get("status", ""), "note": args.get("note", "")})
                continue
            out.append(
                {
                    "path": item.get("path", ""),
                    "status": item.get("status", "") or args.get("status", ""),
                    "note": item.get("note", "") or args.get("note", ""),
                }
            )
        return out
    paths = args.get("paths")
    if isinstance(paths, list) and paths:
        return [
            {"path": path, "status": args.get("status", ""), "note": args.get("note", "")}
            for path in paths
        ]
    path = args.get("path", "")
    if isinstance(path, str) and path:
        return [{"path": path, "status": args.get("status", ""), "note": args.get("note", "")}]
    return []


def _select_inventory(registry, args: dict) -> list[str]:
    dirs = list(args.get("dirs") or [])
    if args.get("dir"):
        dirs.append(args["dir"])
    suffixes = list(args.get("suffixes") or [])
    if args.get("suffix"):
        suffixes.append(args["suffix"])
    patterns = list(args.get("patterns") or [])
    if args.get("pattern"):
        patterns.append(args["pattern"])
    dirs = [normalize_selector_dir(directory) for directory in dirs]
    suffixes = [normalize_selector_suffix(suffix) for suffix in suffixes]
    seen = set()
    paths = []
    for entry in registry.inventory:
        if not matches_file_selectors(entry["path"], dirs, suffixes, patterns):
            continue
        if entry["path"] in seen:
            continue
        seen.add(entry["path"])
        paths.append(entry["path"])
    return paths


def file_review_update(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    items = _normalize_items(args)
    if not items and _has_selectors(args):
        items = [
            {"path": path, "status": args.get("status", ""), "note": args.get("note", "")}
            for path in _select_inventory(registry, args)
        ]
    if len(items) > 1:
        return _file_review_batch(registry, items)
    if len(items) == 0:
        if _has_selectors(args):
            return Result(
                ok=True,
                data={"total": 0, "added": 0, "updated": 0},
                message="file review selector matched no files",
            )
        return Result(ok=False, error="path is required")
    item = items[0]
    path = registry.normalize_inventory_path(item["path"])
    status = item["status"] or "reviewed"
    note = item["note"]
    for existing in registry.files:
        if existing.path == path:
            existing.status = status
            if note:
                existing.note = note
            return Result(ok=True, data=existing.to_dict(), message="file review updated")
    review = FileReview(path=path, status=status, note=note)
    registry.files.append(review)
    return Result(ok=True, data=review.to_dict(), message="file review added")


def _file_review_batch(registry, items: list[dict]):
    from .registry import Result

    total = len(items)
    added = 0
    updated = 0
    sample: list[dict] = []
    for item in items:
        path = item["path"]
        if not path:
            return Result(ok=False, error="path is required for every item")
        path = registry.normalize_inventory_path(path)
        status = item["status"] or "reviewed"
        note = item["note"]
        found = None
        for existing in registry.files:
            if existing.path == path:
                existing.status = status
                if note:
                    existing.note = note
                found = existing
                break
        if found is not None:
            updated += 1
            if len(sample) < 20:
                sample.append(found.to_dict())
            continue
        review = FileReview(path=path, status=status, note=note)
        registry.files.append(review)
        added += 1
        if len(sample) < 20:
            sample.append(review.to_dict())
    data = {"total": total, "added": added, "updated": updated}
    if sample:
        data["sample"] = sample
    return Result(ok=True, data=data, message="file review batch updated")


def variable_review_update(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    name = args.get("name", "")
    if not isinstance(name, str) or not name:
        return Result(ok=False, error="name is required")
    path = args.get("path", "")
    if not isinstance(path, str):
        path = ""
    status = args.get("status", "")
    if not isinstance(status, str):
        status = ""
    if not status:
        status = "tracking"
    note = args.get("note", "")
    if not isinstance(note, str):
        note = ""
    for existing in registry.variables:
        if existing.name == name and existing.path == path:
            existing.status = status
            if note:
                existing.note = note
            return Result(ok=True, data=existing.to_dict(), message="variable review updated")
    review = VariableReview(name=name, path=path, status=status, note=note)
    registry.variables.append(review)
    return Result(ok=True, data=review.to_dict(), message="variable review added")


def flow_review_update(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    name = args.get("name", "")
    if not isinstance(name, str) or not name:
        return Result(ok=False, error="name is required")
    status = args.get("status", "")
    if not isinstance(status, str):
        status = ""
    if not status:
        status = "tracking"
    entry = args.get("entry", "") if isinstance(args.get("entry", ""), str) else ""
    files = args.get("files") if isinstance(args.get("files"), list) else []
    variables = args.get("variables") if isinstance(args.get("variables"), list) else []
    evidence = args.get("evidence", "") if isinstance(args.get("evidence", ""), str) else ""
    next_step = args.get("next_step", "") if isinstance(args.get("next_step", ""), str) else ""
    note = args.get("note", "") if isinstance(args.get("note", ""), str) else ""

    for index, existing in enumerate(registry.flows):
        if existing.name != name:
            continue
        if status in CLOSED_FLOW_STATUSES:
            removed = registry.flows.pop(index)
            removed.status = status
            return Result(
                ok=True, data=removed.to_dict(), message="flow review closed and removed"
            )
        existing.status = status
        if entry:
            existing.entry = entry
        if files:
            existing.files = list(files)
        if variables:
            existing.variables = list(variables)
        if evidence:
            existing.evidence = evidence
        if next_step:
            existing.next_step = next_step
        if note:
            existing.note = note
        return Result(ok=True, data=existing.to_dict(), message="flow review updated")
    if status in CLOSED_FLOW_STATUSES:
        return Result(ok=True, message="flow review already closed; nothing added")
    review = FlowReview(
        name=name,
        status=status,
        entry=entry,
        files=list(files),
        variables=list(variables),
        evidence=evidence,
        next_step=next_step,
        note=note,
    )
    registry.flows.append(review)
    return Result(ok=True, data=review.to_dict(), message="flow review added")


def flow_review_delete(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    name = args.get("name", "")
    if not isinstance(name, str) or not name:
        return Result(ok=False, error="name is required")
    for index, existing in enumerate(registry.flows):
        if existing.name == name:
            removed = registry.flows.pop(index)
            return Result(ok=True, data=removed.to_dict(), message="flow review deleted")
    return Result(ok=False, error="flow not found")


def review_state(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    limit = args.get("limit", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        limit = 80
    snapshot = registry.snapshot()
    if len(snapshot.files) > limit:
        snapshot.files = snapshot.files[:limit]
    if len(snapshot.variables) > limit:
        snapshot.variables = snapshot.variables[:limit]
    if len(snapshot.flows) > limit:
        snapshot.flows = snapshot.flows[:limit]
    return Result(ok=True, data=snapshot.to_dict())
