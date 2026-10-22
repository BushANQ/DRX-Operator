"""Audit todo list tools."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Todo:
    id: int
    title: str
    status: str = "pending"
    priority: str = "medium"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
        }

    def copy(self) -> "Todo":
        return Todo(id=self.id, title=self.title, status=self.status, priority=self.priority)


def normalize_todo_id(value):
    if value is None:
        return 0, None
    if isinstance(value, bool):
        return 0, "invalid todo id type bool"
    if isinstance(value, (int, float)):
        return int(value), None
    if isinstance(value, str):
        try:
            return int(value.strip()), None
        except ValueError:
            return 0, 'invalid todo id: "%s"' % value
    return 0, "invalid todo id type %s" % type(value).__name__


def todo_create(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    title = args.get("title", "")
    if not isinstance(title, str) or not title:
        return Result(ok=False, error="title is required")
    priority = args.get("priority", "")
    if not isinstance(priority, str):
        priority = ""
    if not priority:
        priority = "medium"
    todo = Todo(id=registry.next_todo_id, title=title, status="pending", priority=priority)
    registry.next_todo_id += 1
    registry.todos.append(todo)
    return Result(
        ok=True,
        data=[item.to_dict() for item in registry.todos],
        message="todo created",
    )


def todo_update(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    todo_id, error = normalize_todo_id(args.get("id"))
    if error is not None:
        return Result(ok=False, error=error)
    title = args.get("title", "")
    status = args.get("status", "")
    priority = args.get("priority", "")
    if not isinstance(title, str) or not isinstance(status, str) or not isinstance(priority, str):
        return Result(ok=False, error="invalid arguments")
    if status == "done":
        status = "completed"
    index, error = _find_todo_index(registry, todo_id, title)
    if error is not None:
        return Result(ok=False, error=error)
    if index < 0:
        return Result(ok=False, error="todo not found")
    if title:
        registry.todos[index].title = title
    if status:
        registry.todos[index].status = status
    if priority:
        registry.todos[index].priority = priority
    return Result(
        ok=True,
        data=[item.to_dict() for item in registry.todos],
        message="todo updated",
    )


def _find_todo_index(registry, todo_id: int, title: str):
    if todo_id > 0:
        for index, todo in enumerate(registry.todos):
            if todo.id == todo_id:
                return index, None
        return -1, None
    stripped = (title or "").strip()
    if not stripped:
        return -1, "id is required when title is not provided"
    match = -1
    for index, todo in enumerate(registry.todos):
        if todo.title != stripped:
            continue
        if match >= 0:
            return -1, 'multiple todos match title "%s"; provide id' % stripped
        match = index
    return match, None
