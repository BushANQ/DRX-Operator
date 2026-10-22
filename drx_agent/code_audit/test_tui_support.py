"""Shared fakes and builders for the audit terminal interface tests."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterator


@contextmanager
def temp_dir(prefix: str = "tui-test-") -> Iterator[str]:
    """Create and remove a scratch directory that this environment can write."""
    from drx_agent.code_audit._test_utils import writable_temp_dir

    path = writable_temp_dir(prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_finding(
    finding_id: int,
    severity: str = "high",
    title: str = "finding title",
    path: str = "src/app.py",
    line: int = 10,
    **extra: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=finding_id,
        severity=severity,
        title=title,
        path=path,
        line=line,
        evidence=extra.get("evidence", "evidence text"),
        impact=extra.get("impact", "impact text"),
        recommendation=extra.get("recommendation", "recommendation text"),
        cwe=extra.get("cwe", "CWE-79"),
        key="",
    )


def make_snapshot(
    *,
    findings: list[Any] | None = None,
    files: list[Any] | None = None,
    todos: list[Any] | None = None,
    variables: list[Any] | None = None,
    flows: list[Any] | None = None,
    summary: str = "",
    next_steps: str = "",
    note: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        todos=list(todos or []),
        findings=list(findings or []),
        project=SimpleNamespace(note=note),
        files=list(files or []),
        variables=list(variables or []),
        flows=list(flows or []),
        audit=SimpleNamespace(ended=False, summary=summary, next_steps=next_steps),
    )


def make_budget(
    infinite_mode: bool = False,
    hours: int = 8,
    minutes: int = 0,
    token_limit: int = 0,
    used_tokens: int = 0,
    elapsed: float = 0.0,
    stop_reason: str = "",
    estimated: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        infinite_mode=infinite_mode,
        hours=hours,
        minutes=minutes,
        token_limit=token_limit,
        used_tokens=used_tokens,
        elapsed=elapsed,
        running=False,
        stop_reason=stop_reason,
        estimated=estimated,
    )


def make_message(
    message_id: int,
    content: str,
    *,
    agent_id: str = "user",
    agent_name: str = "user",
    stage: str = "user",
    topic: str = "",
    reply_to: int = 0,
    thread_id: int = 0,
    created_at: str = "2026-09-13T10:00:00Z",
    pinned: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        thread_id=thread_id,
        agent_id=agent_id,
        agent_name=agent_name,
        stage=stage,
        to="*",
        reply_to=reply_to,
        topic=topic,
        content=content,
        created_at=created_at,
        closed=False,
        pinned=pinned,
        announcement=False,
    )


def make_worker(
    worker_id: str = "recon-1",
    name: str = "",
    *,
    status: str = "pending",
    phase: str = "recon",
    activity: str = "",
    turn: int = 0,
    last_model_activity: str = "",
    last_tool_activity: str = "",
    last_tool: str = "",
    generation: Any = None,
    output_excerpt: str = "",
    output_partial: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=worker_id,
        name=name,
        phase=phase,
        status=status,
        activity=activity,
        turn=turn,
        last_model_activity=last_model_activity,
        last_tool_activity=last_tool_activity,
        generation=generation,
        last_tool=last_tool,
        output_excerpt=output_excerpt,
        output_partial=output_partial,
    )


def make_post(
    post_id: int,
    topic: str = "",
    content: str = "",
    *,
    replies: list[Any] | None = None,
    updated_at: str = "2026-09-13T10:00:00Z",
    pinned: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=post_id,
        root=make_message(
            post_id,
            content,
            topic=topic,
            created_at=updated_at,
            pinned=pinned,
        ),
        replies=list(replies or []),
        updated_at=updated_at,
        last_id=post_id,
        root_missing=False,
    )


class FakeTeam:
    """Minimal stand-in for the audit engine used by the interface tests."""

    def __init__(
        self,
        *,
        block: bool = False,
        snapshot: Any | None = None,
        budget: Any | None = None,
        posts: list[Any] | None = None,
        messages: list[Any] | None = None,
        workers: list[Any] | None = None,
        phase: str = "recon",
        fail_run: str = "",
    ) -> None:
        self._phase = phase
        self._snapshot = snapshot or make_snapshot()
        self._budget = budget or make_budget()
        self._workers = list(workers or [])
        self._skills: list[str] = []
        self._posts = list(posts or [])
        self._messages = list(messages or [])
        self.workspace = ""
        self.running = False
        self.cancelled = False
        self.fail_run = fail_run
        self.block = block
        self.run_texts: list[str] = []
        self.workspaces: list[str] = []
        self.saved: list[str] = []
        self.loaded: list[str] = []
        self.posted: list[str] = []
        self.replied: list[tuple[int, str]] = []
        self.budget_calls: list[tuple[bool, int, int, int]] = []
        self.emitted: list[Any] = []
        self.run_started = asyncio.Event()
        self.run_finished = asyncio.Event()
        self._stop_event: asyncio.Event | None = None

    # -- state reads -----------------------------------------------------
    def phase(self) -> str:
        return self._phase

    def snapshot(self) -> Any:
        return self._snapshot

    def statuses(self) -> list[Any]:
        return list(self._workers)

    def budget_status(self) -> Any:
        return self._budget

    def loaded_skills(self) -> list[str]:
        return list(self._skills)

    def forum_posts(self, page: int, page_size: int, search: str = "") -> Any:
        posts = list(self._posts)
        if search:
            posts = [
                post
                for post in posts
                if search in (getattr(post.root, "content", "") or "")
                or search in (getattr(post.root, "topic", "") or "")
            ]
        return SimpleNamespace(
            posts=posts,
            page=max(1, page),
            page_size=page_size,
            total_posts=len(posts),
            total_pages=max(1, (len(posts) + page_size - 1) // page_size),
            has_more=False,
            gap=False,
        )

    def forum_messages(self) -> list[Any]:
        return list(self._messages)

    # -- controls --------------------------------------------------------
    def set_workspace(self, path: str) -> None:
        if self.fail_run == "workspace":
            raise OSError("workspace unavailable")
        self.workspace = path
        self.workspaces.append(path)

    async def run(self, text: str, emit: Any) -> None:
        self.run_texts.append(text)
        self.running = True
        self.emitted = []
        self._stop_event = asyncio.Event()
        self.run_started.set()
        try:
            if self.fail_run == "run":
                raise RuntimeError("engine offline")
            emit(SimpleNamespace(kind="state", phase="audit", workers=self._workers))
            if self.block:
                await self._stop_event.wait()
            else:
                await asyncio.sleep(0)
        finally:
            self.running = False
            self.run_finished.set()

    def cancel(self) -> None:
        self.cancelled = True
        if self._stop_event is not None:
            self._stop_event.set()

    async def post_message(self, text: str) -> None:
        if self.fail_run == "post":
            raise RuntimeError("post rejected")
        self.posted.append(text)
        self._messages.append(make_message(900 + len(self.posted), text))

    async def reply_message(self, reply_to: int, text: str) -> None:
        self.replied.append((reply_to, text))

    def save_session(self, path: str) -> None:
        self.saved.append(path)

    def load_session(self, path: str) -> None:
        if self.fail_run == "load":
            raise ValueError("broken session")
        self.loaded.append(path)

    def configure_budget(self, infinite: bool, hours: int, minutes: int, tokens: int) -> None:
        self.budget_calls.append((infinite, hours, minutes, tokens))
        self._budget = make_budget(
            infinite_mode=infinite,
            hours=hours,
            minutes=minutes,
            token_limit=tokens,
            used_tokens=self._budget.used_tokens,
            elapsed=self._budget.elapsed,
        )

    async def close(self) -> None:
        pass


class BrokenTeam(FakeTeam):
    """Engine whose startup reads raise, mirroring a bad configuration."""

    def phase(self) -> str:
        raise RuntimeError("missing api key")

    def snapshot(self) -> Any:
        raise RuntimeError("missing api key")

    def statuses(self) -> list[Any]:
        raise RuntimeError("missing api key")

    def budget_status(self) -> Any:
        raise RuntimeError("missing api key")

    def forum_posts(self, page: int, page_size: int, search: str = "") -> Any:
        raise RuntimeError("missing api key")


@dataclass
class FakeConfig:
    agent: Any = field(
        default_factory=lambda: SimpleNamespace(
            session_dir="",
            infinite_mode=False,
            budget_hours=8,
            budget_minutes=0,
            budget_tokens=0,
        )
    )


def make_config(tmp_path: Any = None, **agent_overrides: Any) -> FakeConfig:
    values = dict(
        session_dir=str(tmp_path) if tmp_path is not None else "",
        infinite_mode=False,
        budget_hours=8,
        budget_minutes=0,
        budget_tokens=0,
    )
    values.update(agent_overrides)
    return FakeConfig(agent=SimpleNamespace(**values))


def plain_lines(app: Any) -> list[str]:
    return app.render_snapshot()
