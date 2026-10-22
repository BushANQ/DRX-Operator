"""Team orchestration: recon barrier, audit stage, moderator, IRC and budget."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..text import sanitize_text
from . import session as session_mod
from .broadcast import USER_BROADCAST_PRIORITY, broadcast_message, validate_user_broadcast
from .budget import BudgetBook
from .context import estimate_text_tokens, estimate_tokens
from .events import (
    BudgetStatus,
    Event,
    IRCMessage,
    WorkerStatus,
    bounded_excerpt,
    bounded_excerpt,
    clone_generation,
    maybe_await,
    phase_audit,
    phase_completed,
    phase_moderator,
    phase_recon,
    short_text,
    utc_now_iso,
)
from .finding_review import ReportQueue, review_finding_report
from .irc import (
    change_irc,
    deliver_worker_irc,
    fail_worker_irc,
    irc_tool,
    pending_irc,
    read_irc,
    reply_irc,
    send_irc,
    signal_irc,
    stage_pending_irc,
)


@dataclass
class TeamWorker:
    agent: Any
    saved: session_mod.WorkerSession
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    active: bool = False
    activity_task: asyncio.Task | None = None
    task: asyncio.Task | None = None


class Team:
    def __init__(self, cfg, prompts, client, compress_client, registry) -> None:
        self.cfg = cfg
        self.prompts = prompts
        self.client = client
        self.compress_client = compress_client or client
        self.registry = registry
        if not self.cfg.agent.recon_agents:
            self.cfg.agent.recon_agents = 4
        if not self.cfg.agent.audit_agents:
            self.cfg.agent.audit_agents = 4
        self._phase = phase_recon
        self.input = ""
        self.handoff = ""
        self.workers: list[TeamWorker] = []
        self.moderator: TeamWorker | None = None
        self.moderator_task: asyncio.Task | None = None
        self.moderator_task_activation: asyncio.Task | None = None
        self.moderator_wake: asyncio.Event | None = None
        self.moderator_reviews: asyncio.Queue | None = None
        self.moderator_ticks: asyncio.Queue | None = None
        self.revocations: list[dict[str, Any]] = []
        self.pending_assignments: dict[str, list[str]] = {}
        self.worker_cancels: dict[str, dict[str, Any]] = {}
        self.report_queue = ReportQueue()
        self.emitter: Callable[[Event], None] | None = None
        self.running = False
        self.cancelled = False
        self.stage_open = False
        self.irc_messages: list[IRCMessage] = []
        self.irc_next_id = 0
        self.irc_publishing = 0
        self.irc_changed = asyncio.Event()
        self.user_broadcasts: list[Any] = []
        self._snapshot: Any = None
        self.budget = BudgetBook()
        self.budget.bind_config(self.cfg)
        self._stage_cancel: asyncio.Event | None = None
        self._run_task: asyncio.Task | None = None
        self._board_request_close = getattr(registry, "request_close", None)
        self._local_consensus: dict[str, dict[str, Any]] = {}
        self._snapshot = self.aggregate()
        self.reset_board()

    # ------------------------------------------------------------------
    # Board and helpers
    # ------------------------------------------------------------------
    def reset_board(self) -> None:
        from ..forum import Board

        self.board = Board(float(self.cfg.agent.forum_wait_seconds or 30))
        self.board.register("user", "user")
        self.board.register("coordinator", "system")
        if self.cfg.agent.moderator_is_enabled():
            self.board.register(phase_moderator, phase_moderator)
            with contextlib.suppress(Exception):
                self.board.register_name(phase_moderator, "论坛管理员")
        for stage, count in (
            (phase_recon, int(self.cfg.agent.recon_agents)),
            (phase_audit, int(self.cfg.agent.audit_agents)),
        ):
            ids = []
            for index in range(1, count + 1):
                worker_id = "%s-%d" % (stage, index)
                self.board.register(worker_id, stage)
                self.board.set_status(worker_id, "pending")
                ids.append(worker_id)
            self.board.set_consensus_members(stage, ids)
        self.board.set_on_post(self._on_forum_post)
        self.board.set_on_consensus(self._on_consensus)

    def _on_forum_post(self, message: Any) -> None:
        self.publish(
            Event(
                kind="forum",
                agent_id=getattr(message, "agent_id", ""),
                phase=getattr(message, "stage", ""),
                forum=message,
            )
        )
        if getattr(message, "agent_id", "") != phase_moderator and self.moderator_wake is not None:
            self.moderator_wake.set()

    def _on_consensus(self, stage: str) -> None:
        if self.cfg.agent.infinite_mode:
            return
        for cancel in list(self.worker_cancels.get(stage, {}).values()):
            with contextlib.suppress(Exception):
                cancel()

    def phase(self) -> str:
        return self._phase

    def snapshot(self) -> Any:
        return self._snapshot

    @property
    def is_running(self) -> bool:
        task = self._run_task
        return bool(self.running and task is not None and not task.done())

    def cancel(self) -> None:
        """Request a pause: stop the current run at the next safe boundary."""

        self.cancelled = True
        self._cancel_run()

    def event(self, kind: str, **kwargs: Any) -> Event:
        kwargs.setdefault("phase", self._phase)
        return Event(kind=kind, **kwargs)

    def publish(self, event: Event) -> None:
        emitter = self.emitter
        if emitter is not None:
            emitter(event)

    def emit_state(self) -> None:
        self.publish(self.state_event())

    def state_event(self) -> Event:
        snapshot = self._snapshot
        return Event(
            kind="state",
            phase=self._phase,
            workers=self.status_dicts(),
            skills=self.loaded_skills(),
            todos=getattr(snapshot, "todos", None),
            findings=getattr(snapshot, "findings", None),
            project=getattr(snapshot, "project_note", None),
            files=getattr(snapshot, "files", None),
            variables=getattr(snapshot, "variables", None),
            flows=getattr(snapshot, "flows", None),
            audit=getattr(snapshot, "audit", None),
        )

    def status_dicts(self) -> list[WorkerStatus]:
        return [_clone_status(worker.saved.status) for worker in self.workers] + (
            [_clone_status(self.moderator.saved.status)] if self.moderator is not None else []
        )

    def statuses(self) -> list[WorkerStatus]:
        return self.status_dicts()

    def loaded_skills(self) -> list[str]:
        names: set[str] = set()
        for worker in self.workers:
            names.update(worker.saved.skills or [])
        return sorted(names)

    def forum_messages(self) -> list[Any]:
        return list(self.board.messages())

    def forum_posts(self, page: int, page_size: int, search: str = "") -> Any:
        return self.board.list_posts(page, page_size, search)

    async def post_message(self, content: str) -> None:
        content = sanitize_text(content)
        validate_user_broadcast(content)
        self.user_broadcasts.append(_UserBroadcast(len(self.user_broadcasts) + 1, content))
        await maybe_await(self.board.post("user", "user", "*", 0, "用户补充", content))

    async def reply_message(self, reply_to: int, content: str) -> None:
        await maybe_await(
            self.board.post(
                "user",
                "user",
                "*",
                int(reply_to),
                "用户回复",
                sanitize_text((content or "").strip()),
            )
        )

    def broadcasts_after(self, cursor: int) -> list[Any]:
        if cursor < 0 or cursor >= len(self.user_broadcasts):
            return []
        return list(self.user_broadcasts[cursor:])

    def utc_now(self) -> str:
        return utc_now_iso()

    def estimate_tokens(self, messages: Any) -> int:
        return estimate_tokens(messages)

    def estimate_text_tokens(self, text: str) -> int:
        return estimate_text_tokens(text)

    def _snapshot_dict(self, snapshot: Any) -> dict[str, Any]:
        return session_mod.snapshot_to_dict(snapshot)

    def finding_key(self, finding: Any) -> str:
        from ..tools import finding_key as tools_finding_key

        return tools_finding_key(finding)

    # ------------------------------------------------------------------
    # Worker construction
    # ------------------------------------------------------------------
    def make_worker(
        self,
        agent: Any,
        *,
        status_id: str,
        name: str,
        phase: str,
        status: str = "pending",
        activity: str = "",
    ) -> TeamWorker:
        saved = session_mod.WorkerSession(
            status=WorkerStatus(id=status_id, name=name, phase=phase, status=status, activity=activity),
            snapshot=agent.tools.snapshot(),
        )
        worker = TeamWorker(agent=agent, saved=saved)
        self.bind_worker(agent, worker)
        return worker

    def bind_worker(self, agent: Any, worker: TeamWorker) -> None:
        agent.checkpoint = lambda: self.capture(worker, agent)
        agent.user_broadcast_source = self.broadcasts_after
        agent.on_disconnect = self.model_disconnected
        agent.team = self
        agent.activity_register = lambda task: setattr(worker, "activity_task", task)
        agent.activity_clear = lambda: setattr(worker, "activity_task", None)
        self.bind_irc(worker)

    def bind_irc(self, worker: TeamWorker) -> None:
        agent = worker.agent
        agent.irc_tool = lambda name, args: irc_tool(self, worker, name, args)
        if agent.phase == phase_moderator:
            return
        agent.review_report = lambda raw: review_finding_report(self, worker, raw)
        agent.irc_pending = lambda: [
            message
            for message in self.irc_messages
            if message.agent_id == agent.id and message.state == "delivered"
        ]
        agent.deliver_irc = lambda: deliver_worker_irc(self, worker)

    def create_stage(self, stage: str) -> None:
        from .core import new_worker

        count = int(self.cfg.agent.recon_agents if stage == phase_recon else self.cfg.agent.audit_agents)
        assignment = stage_assignment(stage, count)
        for index in range(1, count + 1):
            worker_id = "%s-%d" % (stage, index)
            agent = new_worker(
                self.cfg,
                self.prompts.clone(),
                self.client,
                self.compress_client,
                self.registry.fork(),
                worker_id,
                stage,
                self.board,
            )
            agent.assignment = assignment
            if stage == phase_audit:
                agent.handoff = self.handoff
            worker = self.make_worker(
                agent,
                status_id=worker_id,
                name=self.board.name(worker_id),
                phase=stage,
                activity="",
            )
            self.workers.append(worker)
        self._snapshot = self.aggregate()

    def rebuild_worker(self, saved: session_mod.WorkerSession) -> TeamWorker:
        from .core import new_worker

        count = int(
            self.cfg.agent.recon_agents if saved.status.phase == phase_recon else self.cfg.agent.audit_agents
        )
        agent = new_worker(
            self.cfg,
            self.prompts.clone(),
            self.client,
            self.compress_client,
            self.registry.fork(),
            saved.status.id,
            saved.status.phase,
            self.board,
        )
        agent.assignment = stage_assignment(saved.status.phase, count)
        agent.turn = saved.status.turn
        agent.completed = saved.completed
        agent.plan = saved.plan
        agent.forum_cursor = saved.forum_cursor
        agent.forum_pending = saved.forum_pending
        agent.announced_name = saved.announced_name
        agent.trace_path = saved.trace_path
        agent.prompts.set_loaded_skills(saved.skills or [])
        agent.tools.restore_snapshot(saved.snapshot)
        agent.messages = list(saved.messages)
        agent.user_broadcast_cursor = saved.user_broadcast_cursor
        agent.user_broadcast_delivered = saved.user_broadcast_delivered
        for broadcast in self.user_broadcasts:
            agent.user_broadcast_messages.append(broadcast_message(broadcast))
            agent.user_broadcast_version = broadcast.id
        agent.user_broadcast_tokens = estimate_tokens(agent.user_broadcast_messages)
        agent.sanitize_messages()
        from ..llm import Message

        agent.messages.append(
            Message(
                role="user",
                content="会话已恢复。旧工具 buffer 已过期；不要沿用旧 buffer_id。需要时重新调用原工具、read_handoff 或 review_state。",
            )
        )
        if not saved.completed:
            saved.status.status = "pending"
            saved.status.activity = "已恢复，等待 go"
        worker = TeamWorker(agent=agent, saved=saved)
        self.bind_worker(agent, worker)
        if saved.status.phase == phase_moderator:
            agent.assignment = ""
            agent.completed = False
            agent.moderate_tool = lambda name, args: __import__(
                "drx_agent.code_audit.agent.moderator", fromlist=["moderator_tool"]
            ).moderator_tool(self, worker, name, args)
            saved.completed = False
            saved.status.status = "idle"
            saved.status.activity = "已恢复，等待 go"
        self.board.register(agent.id, agent.phase)
        self.board.set_status(agent.id, saved.status.status)
        return worker

    # ------------------------------------------------------------------
    # Capture / receive / aggregate
    # ------------------------------------------------------------------
    def capture(self, worker: TeamWorker, agent: Any) -> None:
        status = worker.saved.status
        saved = session_mod.WorkerSession(
            status=WorkerStatus(
                id=agent.id,
                name=status.name or self.board.name(agent.id),
                phase=agent.phase,
                status=status.status,
                activity=status.activity,
                turn=agent.turn,
                last_model_activity=status.last_model_activity,
                last_tool_activity=status.last_tool_activity,
                generation=clone_generation(status.generation),
                last_tool=status.last_tool,
                output_excerpt=status.output_excerpt,
                output_partial=status.output_partial,
            ),
            assignment=agent.assignment,
            messages=list(agent.messages),
            skills=agent.prompts.loaded_skill_names(),
            trace_path=agent.trace_path_value(),
            snapshot=agent.tools.snapshot(),
            plan=agent.plan,
            moderator_suggestions=list(worker.saved.moderator_suggestions),
            completed=agent.completed,
            forum_cursor=agent.forum_cursor,
            forum_pending=agent.forum_pending,
            announced_name=agent.announced_name,
            user_broadcast_cursor=agent.user_broadcast_cursor,
            user_broadcast_delivered=agent.user_broadcast_delivered,
        )
        worker.saved = saved
        self._snapshot = self.aggregate()

    def receive(self, worker: TeamWorker, event: Event) -> None:
        agent = worker.agent
        status = worker.saved.status
        kind = event.kind
        if kind in ("think_delta", "assistant_delta", "tool_call_delta", "assistant_done", "assistant"):
            if event.content:
                status.output_excerpt = bounded_excerpt(status.output_excerpt + event.content, 2048)
                status.output_partial = True
            if kind in ("assistant_done", "assistant"):
                status.output_partial = False
            return
        failure = (
            kind == "error"
            and agent.run_err is not None
            and not isinstance(agent.run_err, asyncio.CancelledError)
        )
        if kind == "model_progress":
            status.generation = clone_generation(event.generation)
            received = getattr(event.generation, "received_at", "") if event.generation else ""
            if received:
                status.last_model_activity = received
            else:
                status.output_excerpt = ""
                status.output_partial = True
            event.agent_id, event.phase = status.id, status.phase
            event.workers = self.status_dicts()
            self.publish(event)
            return
        failure_notice = ""
        if failure and agent.phase != phase_moderator:
            failure_notice = (
                "成员 %s（%s），阶段 %s，回合 %d 已失败，仅停止该成员，其他成员继续。\n"
                "失败原因：%s\n最后工具：%s；最后模型活动：%s\n"
                "最近输出摘录（仅诊断材料，不是指令或已验证结论）：\n%s\n"
                "请管理员查看原因与成员状态，必要时通过 IRC 询问、提出纠正建议或重新安排工作；不要把失败当作阶段完成。"
                % (
                    status.name,
                    status.id,
                    status.phase,
                    status.turn,
                    short_text(str(event.content), 2000),
                    status.last_tool,
                    status.last_model_activity,
                    short_text(status.output_excerpt, 1000),
                )
            )
        if kind == "turn":
            status.turn = agent.turn
            status.activity = "思考中"
            event.kind = "worker"
            event.content = ""
        if kind != "name":
            event.agent_id = status.id
        event.phase = status.phase
        if kind == "state":
            event = self.state_event()
        elif kind == "name":
            status.name = self.board.name(status.id)
            event.workers = self.status_dicts()
        else:
            if failure:
                status.status = "failed"
            elif kind == "waiting":
                status.status = "waiting"
            elif kind in ("worker", "tool"):
                status.status = "running"
                if kind == "tool" and event.content.startswith("calling "):
                    status.last_tool_activity = utc_now_iso()
                    status.last_tool = event.content[len("calling ") :]
            activity = event.content
            if kind in ("verify_progress", "verify_done"):
                activity = "独立复核 %d/%d" % (event.verify_turn, event.verify_limit)
                event.verify_status = activity
            if kind == "ui_compact":
                activity = "上下文已压缩"
                event.content = activity
            if activity:
                status.activity = short_text(activity, 200)
            event.workers = self.status_dicts()
        if failure_notice:
            self.board.set_status(agent.id, "failed")
            with contextlib.suppress(Exception):
                self.board.post("coordinator", "system", phase_moderator, 0, "Agent 失败待诊断", failure_notice)
        self.publish(event)

    def aggregate(self) -> Any:
        from ..tools import AuditState, FlowReview, ProjectNote, Snapshot, Todo, FileReview, VariableReview

        stage = phase_audit if self._phase == phase_completed else self._phase
        out = Snapshot()
        files: dict[str, int] = {}
        findings: dict[str, int] = {}
        notes: list[str] = []
        summaries: list[str] = []
        next_steps: list[str] = []
        all_done = True
        count = 0
        for worker in self.workers:
            saved = worker.saved
            if saved.status.phase != stage:
                continue
            count += 1
            if not saved.completed:
                all_done = False
            name = saved.status.name or "未命名 Agent"
            todo_list = getattr(saved.snapshot, "todos", []) or []
            for todo in todo_list:
                out.todos.append(
                    Todo(
                        id=len(out.todos) + 1,
                        title="[" + name + "] " + getattr(todo, "title", ""),
                        status=getattr(todo, "status", ""),
                        priority=getattr(todo, "priority", ""),
                    )
                )
            for finding in getattr(saved.snapshot, "findings", []) or []:
                key = self.finding_key(finding)
                if key in findings:
                    continue
                findings[key] = len(out.findings)
                copy_finding = copy.copy(finding)
                copy_finding.id = len(out.findings) + 1
                copy_finding.key = key
                copy_finding.evidence = "[" + name + "] " + (getattr(finding, "evidence", "") or "")
                out.findings.append(copy_finding)
            for file in getattr(saved.snapshot, "files", []) or []:
                note = "[" + name + "] " + (getattr(file, "note", "") or "")
                existing = files.get(getattr(file, "path", ""))
                if existing is not None:
                    out.files[existing].note = (out.files[existing].note or "") + "\n" + note
                    if _file_rank(getattr(file, "status", "")) < _file_rank(out.files[existing].status):
                        out.files[existing].status = getattr(file, "status", "")
                else:
                    files[getattr(file, "path", "")] = len(out.files)
                    out.files.append(
                        FileReview(
                            path=getattr(file, "path", ""),
                            status=getattr(file, "status", ""),
                            note=note,
                        )
                    )
            for variable in getattr(saved.snapshot, "variables", []) or []:
                out.variables.append(
                    VariableReview(
                        name=getattr(variable, "name", ""),
                        path=getattr(variable, "path", ""),
                        status=getattr(variable, "status", ""),
                        note="[" + name + "] " + (getattr(variable, "note", "") or ""),
                    )
                )
            for flow in getattr(saved.snapshot, "flows", []) or []:
                out.flows.append(
                    FlowReview(
                        name="[" + name + "] " + getattr(flow, "name", ""),
                        status=getattr(flow, "status", ""),
                        entry=getattr(flow, "entry", ""),
                        files=list(getattr(flow, "files", []) or []),
                        variables=list(getattr(flow, "variables", []) or []),
                        evidence=getattr(flow, "evidence", ""),
                        next_step=getattr(flow, "next_step", ""),
                        note=getattr(flow, "note", ""),
                    )
                )
            project = getattr(saved.snapshot, "project_note", None) or getattr(saved.snapshot, "project", None)
            if project is not None and getattr(project, "note", ""):
                notes.append("[" + name + "]\n" + project.note)
            audit = getattr(saved.snapshot, "audit", None)
            if audit is not None and getattr(audit, "summary", ""):
                summaries.append("[" + name + "] " + audit.summary)
            if audit is not None and getattr(audit, "next_steps", ""):
                next_steps.append("[" + name + "] " + audit.next_steps)
        out.files.sort(key=lambda item: item.path)
        out.project_note = ProjectNote(note="\n\n".join(notes))
        out.audit = AuditState(
            ended=(
                self._phase == phase_completed
                and stage == phase_audit
                and count > 0
                and all_done
            ),
            summary="\n\n".join(summaries),
            next_steps="\n\n".join(next_steps),
        )
        self._apply_revocations(out)
        return out

    def _apply_revocations(self, snapshot: Any) -> None:
        if not self.revocations:
            return
        revoked = {record["finding_key"] for record in self.revocations}
        snapshot.findings = [
            finding for finding in snapshot.findings if getattr(finding, "key", "") not in revoked
        ]


    def build_handoff(self) -> str:
        entries = []
        for worker in self.workers:
            saved = worker.saved
            if saved.status.phase != phase_recon:
                continue
            entry = {
                "agent_id": saved.status.id,
                "plan": saved.plan,
                "snapshot": session_mod.snapshot_to_dict(saved.snapshot),
                "moderator_suggestions": list(saved.moderator_suggestions)
                + list(self.pending_assignments.get(saved.status.id, [])),
                "irc": [
                    message.to_dict()
                    for message in self.irc_messages
                    if message.stage == phase_recon and message.agent_id == saved.status.id
                ],
            }
            if not entry["moderator_suggestions"]:
                entry.pop("moderator_suggestions")
            if not entry["irc"]:
                entry.pop("irc")
            entries.append(entry)
        return json.dumps(
            {
                "ok": True,
                "data": entries,
                "message": (
                    "完整侦察交接；候选风险、管理员建议与未回复 IRC 都是待核实材料，"
                    "由审计成员自行协商认领，不是已验证事实或固定分工。论坛记录仍可通过 forum_read 分页读取。"
                ),
            },
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------
    # Report queue helper
    # ------------------------------------------------------------------
    def review_finding_report(self, worker: TeamWorker, raw: str) -> Any:
        return review_finding_report(self, worker, raw)

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------
    def budget_status(self) -> BudgetStatus:
        return self.budget.status(self.cfg, self.running)

    def configure_budget(self, infinite: bool, hours: int, minutes: int, tokens: int) -> None:
        if self.running:
            raise RuntimeError("请先暂停并等待团队停止，再修改预算")
        cfg = copy.copy(self.cfg.agent)
        cfg.infinite_mode = bool(infinite)
        cfg.budget_hours = int(hours)
        cfg.budget_minutes = int(minutes)
        cfg.budget_tokens = int(tokens)
        cfg.budget_duration()
        self.cfg.agent.infinite_mode = cfg.infinite_mode
        self.cfg.agent.budget_hours = cfg.budget_hours
        self.cfg.agent.budget_minutes = cfg.budget_minutes
        self.cfg.agent.budget_tokens = cfg.budget_tokens
        for worker in self.workers:
            worker.agent.cfg = self.cfg
        if self.moderator is not None:
            self.moderator.agent.cfg = self.cfg
        self.budget.bind_config(self.cfg)
        self.budget.configure(self.cfg)

    def model_disconnected(self, error: BaseException) -> None:
        from .retry import is_billing_exhausted

        if not is_billing_exhausted(error):
            return
        self.publish(
            Event(
                kind="error",
                content=(
                    "API余额或额度不足：已立即终止全队运行（包括管理员），保留进度，不重试、不等待冷却；"
                    "充值后可手动 go 继续。" + str(error)
                ),
            )
        )
        self._cancel_run()

    def _cancel_run(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    def _on_exhausted(self) -> None:
        self._cancel_run()

    # ------------------------------------------------------------------
    # Stage machine
    # ------------------------------------------------------------------
    async def run(self, input_text: str, emit: Callable[[Event], None]) -> None:
        if self.running:
            if emit is not None:
                emit(Event(kind="error", content="审计团队已经在运行"))
            return
        self.running = True
        self.cancelled = False
        self.emitter = emit
        self._run_task = asyncio.current_task()
        self.budget.bind_config(self.cfg)
        self.budget.start(self.cfg, self._on_exhausted)
        if not self.input:
            self.input = input_text
        if self.cfg.agent.infinite_mode:
            self.board.reset_consensus(phase_audit)
            self._local_consensus.pop(phase_audit, None)
            for worker in self.workers:
                if worker.saved.status.phase == phase_audit and worker.saved.completed:
                    worker.saved.completed = False
                    worker.agent.completed = False
                    worker.saved.status.status = "pending"
                    worker.saved.snapshot.audit = _empty_audit()
                    worker.agent.tools.restore_snapshot(worker.saved.snapshot)
        if self._phase == phase_completed:
            self.board.reset_consensus(phase_audit)
            self._local_consensus.pop(phase_audit, None)
            self._phase = phase_audit
            for worker in self.workers:
                if worker.saved.status.phase == phase_audit:
                    worker.saved.completed = False
                    worker.agent.completed = False
                    worker.saved.status.status = "pending"
                    worker.saved.snapshot.audit = _empty_audit()
                    worker.agent.tools.restore_snapshot(worker.saved.snapshot)
        if not self.workers:
            self.create_stage(phase_recon)
        self.ensure_moderator()
        stop_moderator = await self.start_moderator()
        try:
            while True:
                if self.budget.reason:
                    break
                stage = self._phase
                stop_actors = self.start_stage_actors(stage, input_text)
                with contextlib.suppress(Exception):
                    await maybe_await(
                        self.board.post(
                            "coordinator",
                            "system",
                            "*",
                            0,
                            "阶段调度",
                            stage + " 阶段启动；完成成员在阶段屏障前仍可回答 IRC。",
                        )
                    )
                complete = False
                approved = False
                try:
                    while True:
                        if not await self.wait_stage_idle(stage):
                            break
                        complete = all(
                            worker.saved.completed
                            for worker in self.workers
                            if worker.saved.status.phase == stage
                        )
                        if complete and stage == phase_recon:
                            approved = True
                            self.stage_open = False
                            break
                        if complete:
                            approved = await self.review_stage(stage)
                        if not await self.wait_stage_idle(stage):
                            break
                        busy = self.stage_pending_irc(stage) or self.irc_publishing > 0
                        busy = busy or any(
                            worker.active
                            for worker in self.workers
                            if worker.saved.status.phase == stage
                        )
                        if busy:
                            continue
                        if any(self.pending_assignments.values()):
                            approved = False
                        self.stage_open = False
                        break
                finally:
                    self.stage_open = False
                    if stage == phase_recon and complete and stop_moderator is not None:
                        await stop_moderator()
                        stop_moderator = None
                    await stop_actors()
                if self.cancelled or not complete:
                    self.emit_state()
                    return
                if not approved:
                    reopened = self.apply_moderator_assignments(stage)
                    if reopened:
                        self.board.reset_consensus(stage)
                        self._local_consensus.pop(stage, None)
                        continue
                    self.emit_state()
                    return
                if stage == phase_recon:
                    for message in self.irc_messages:
                        if message.stage == phase_recon and message.state in ("queued", "delivered"):
                            message.state = "failed"
                            message.error = (
                                "侦察交接已完成；未回复 IRC 已转入审计交接，不再等待侦察成员"
                            )
                            change_irc(self, message)
                    for worker in self.workers:
                        if worker.saved.status.phase == phase_recon:
                            worker.saved.moderator_suggestions.extend(
                                self.pending_assignments.get(worker.saved.status.id, [])
                            )
                            self.pending_assignments.pop(worker.saved.status.id, None)
                    self.handoff = self.build_handoff()
                    self._phase = phase_audit
                    self.create_stage(phase_audit)
                    if stop_moderator is not None:
                        await stop_moderator()
                    stop_moderator = await self.start_moderator()
                    with contextlib.suppress(Exception):
                        await maybe_await(
                            self.board.post(
                                "coordinator",
                                "system",
                                "*",
                                0,
                                "阶段交接",
                                "全部侦察 Agent 已完成；新建审计 Agent，按需继承结构化地图、笔记、待办与论坛，不继承原始对话。",
                            )
                        )
                    continue
                self._phase = phase_completed
                self._snapshot = self.aggregate()
                self.emit_state()
                return
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            await self._cleanup(stop_moderator)

    async def _cleanup(self, stop_moderator) -> None:
        if stop_moderator is not None:
            with contextlib.suppress(BaseException):
                await stop_moderator()
        reason = self.budget.finish(self.cfg)
        self.stage_open = False
        for message in self.irc_messages:
            if message.state in ("queued", "delivered"):
                message.error = "cancelled: team paused; delivery/reply retained for resume"
                change_irc(self, message)
        if reason:
            for worker in self.workers:
                if not worker.saved.completed:
                    worker.saved.status.status = "cancelled"
                    worker.saved.status.activity = reason + "，调整预算后可继续"
            self.publish(
                Event(
                    kind="info",
                    content=reason + "；已停止整个团队，保留审计进度（不是审计完成）",
                )
            )
        self.running = False
        self.emitter = None

    # ------------------------------------------------------------------
    # Actors
    # ------------------------------------------------------------------
    def start_stage_actors(self, stage: str, input_text: str):
        self._stage_cancel = asyncio.Event()
        self.stage_open = True
        self.worker_cancels[stage] = {}
        original = self.input
        actors: list[TeamWorker] = []
        for worker in self.workers:
            if worker.saved.status.phase != stage:
                continue
            worker.wake = asyncio.Event()
            worker.active = not worker.saved.completed or pending_irc(self, worker.agent.id)
            actors.append(worker)
        tasks: list[asyncio.Task] = []
        for worker in actors:
            worker.task = asyncio.create_task(self._actor(worker, stage, input_text, original))
            tasks.append(worker.task)

        async def stop() -> None:
            if self._stage_cancel is not None:
                self._stage_cancel.set()
            for worker in actors:
                worker.wake.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.worker_cancels.pop(stage, None)

        return stop

    async def _actor(self, worker: TeamWorker, stage: str, input_text: str, original: str) -> None:
        agent = worker.agent
        stage_cancel = self._stage_cancel
        try:
            if not agent.completed:
                assignments: list[str] = []
                if stage == phase_audit:
                    assignments = self.pending_assignments.pop(agent.id, [])
                instruction = "原始审计目标：\n" + original
                for work in assignments:
                    instruction += "\n\n管理员建议（自行判断并反馈证据）：\n" + work
                if input_text != original:
                    instruction += "\n\n本次补充要求：\n" + input_text
                worker.active = True
                try:
                    await agent.run(instruction, lambda event: self.receive(worker, event))
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    agent.run_err = exc
                    self.receive(worker, Event(kind="error", content=str(exc)))
                if (
                    not self.cfg.agent.infinite_mode
                    and stage == phase_audit
                    and stage_cancel is not None
                    and not stage_cancel.is_set()
                ):
                    decision = self._consensus_approved(stage)
                    if decision is not None:
                        agent.completed = True
                        snapshot = agent.tools.snapshot()
                        snapshot.audit = _audit_ended(decision)
                        agent.tools.restore_snapshot(snapshot)
            while stage_cancel is None or not stage_cancel.is_set():
                if pending_irc(self, agent.id):
                    worker.active = True
                    try:
                        await agent.run_irc_only(lambda event: self.receive(worker, event))
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:  # noqa: BLE001
                        agent.run_err = exc
                        fail_worker_irc(self, worker, exc)
                self.capture(worker, agent)
                if pending_irc(self, agent.id) and not stage_cancel.is_set():
                    continue
                worker.active = False
                status = "completed"
                activity = "阶段完成"
                if not worker.saved.completed:
                    status, activity = "failed", "阶段未完成"
                    if agent.run_err is not None:
                        activity = str(agent.run_err)
                worker.saved.status.status = status
                worker.saved.status.activity = short_text(activity, 200)
                signal_irc(self)
                self.board.set_status(agent.id, status)
                self.emit_state()
                if stage_cancel.is_set():
                    return
                worker.wake = asyncio.Event()
                await worker.wake.wait()
        except asyncio.CancelledError:
            if worker.saved.completed:
                worker.saved.status.status = "completed"
                worker.saved.status.activity = "阶段完成"
            else:
                worker.saved.status.status = "cancelled"
                worker.saved.status.activity = "已暂停，等待 go"
            worker.active = False
            worker.activity_task = None
            self.capture(worker, agent)
            raise
        finally:
            worker.active = False
            worker.activity_task = None
            if worker.saved.completed:
                worker.saved.status.status = "completed"
                worker.saved.status.activity = "阶段完成"
            self.capture(worker, agent)

    async def wait_stage_idle(self, stage: str) -> bool:
        while True:
            if self.cancelled:
                return False
            busy = self.stage_pending_irc(stage) or self.irc_publishing > 0
            all_recon_complete = stage == phase_recon
            for worker in self.workers:
                if worker.saved.status.phase != stage:
                    continue
                if not worker.saved.completed:
                    all_recon_complete = False
                if worker.active:
                    busy = True
            if not busy or all_recon_complete:
                return True
            changed = self.irc_changed
            wakers = [worker.wake for worker in self.workers if worker.saved.status.phase == stage]
            waiters = [asyncio.create_task(changed.wait())] + [
                asyncio.create_task(event.wait()) for event in wakers
            ]
            try:
                for task in waiters:
                    task.add_done_callback(lambda _t: None)
                await asyncio.wait(
                    waiters,
                    timeout=0.05,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in waiters:
                    if not task.done():
                        task.cancel()
                for task in waiters:
                    with contextlib.suppress(BaseException):
                        await task

    # ------------------------------------------------------------------
    # Moderator glue
    # ------------------------------------------------------------------
    def ensure_moderator(self) -> None:
        from .moderator import ensure_moderator

        ensure_moderator(self)

    async def start_moderator(self):
        from .moderator import start_moderator

        return await start_moderator(self)

    async def review_stage(self, stage: str) -> bool:
        from .moderator import review_stage

        return await review_stage(self, stage)

    def apply_moderator_assignments(self, stage: str) -> bool:
        from .moderator import apply_moderator_assignments

        return apply_moderator_assignments(self, stage)

    # ------------------------------------------------------------------
    # Consensus fallback
    # ------------------------------------------------------------------
    def request_close(
        self, agent_id: str, stage: str, summary: str, next_steps: str, vote: str
    ) -> dict[str, Any]:
        request_close = getattr(self.board, "request_close", None)
        if callable(request_close):
            return request_close(agent_id, stage, summary, next_steps, vote)
        return self._local_request_close(agent_id, stage, summary, next_steps, vote)

    def _local_request_close(
        self, agent_id: str, stage: str, summary: str, next_steps: str, vote: str
    ) -> dict[str, Any]:
        vote = (vote or "").strip().lower()
        approve = vote in ("approve", "approved", "yes", "同意")
        reject = vote in ("reject", "rejected", "no", "不同意", "否决")
        if not approve and not reject:
            return {"status": "invalid", "reason": "vote must be approve or reject"}
        members = [
            worker.saved.status.id
            for worker in self.workers
            if worker.saved.status.phase == stage
        ]
        if agent_id not in members:
            return {"status": "invalid", "reason": "该身份不是本阶段的审计 Agent"}
        request = self._local_consensus.get(stage)
        if request is None or request["status"] != "pending":
            if reject:
                return {"status": "invalid", "reason": "当前没有可否决的关闭请求；先发起新的 end_audit 请求"}
            request = {"status": "pending", "votes": {agent_id}, "members": members, "summary": summary, "next_steps": next_steps}
            self._local_consensus[stage] = request
        elif reject:
            request["status"] = "rejected"
            return {"status": "rejected", "required": len(members), "approved": len(request["votes"]), "reason": "已否决关闭请求"}
        else:
            request["votes"].add(agent_id)
        approved = len(request["votes"] & set(members))
        if approved == len(members):
            request["status"] = "approved"
            return {"status": "approved", "required": len(members), "approved": approved, "summary": summary, "next_steps": next_steps}
        return {"status": "pending", "required": len(members), "approved": approved}

    def _consensus_approved(self, stage: str) -> dict[str, Any] | None:
        getter = getattr(self.board, "consensus_approved_for_stage", None)
        if callable(getter):
            result = getter(stage)
            if isinstance(result, tuple):
                decision, approved = result
                return decision if approved else None
            return result if result else None
        request = self._local_consensus.get(stage)
        if request is not None and request["status"] == "approved":
            return request
        return None

    def stage_pending_irc(self, stage: str) -> bool:
        return stage_pending_irc(self, stage)

    # ------------------------------------------------------------------
    # Session / workspace / close
    # ------------------------------------------------------------------
    def save_session(self, path: str) -> None:
        session_mod.save_session(self, path)

    def load_session(self, path: str) -> None:
        session_mod.load_session(self, path)

    def set_workspace(self, workspace: str) -> None:
        if self.running:
            raise RuntimeError("审计尚未停止，不能切换工作区")
        self.registry.set_workspace(workspace)
        for worker in self.workers:
            with contextlib.suppress(Exception):
                worker.agent.tools.close()
        if self.moderator is not None:
            with contextlib.suppress(Exception):
                self.moderator.agent.tools.close()
            self.moderator = None
        self.revocations = []
        self.pending_assignments = {}
        self.irc_messages = []
        self.irc_next_id = 0
        self.user_broadcasts = []
        self.workers = []
        self._phase, self.input, self.handoff = phase_recon, "", ""
        self._snapshot = self.aggregate()
        self.budget = BudgetBook()
        self.budget.bind_config(self.cfg)
        self.stage_open = False
        self.reset_board()

    async def close(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            with contextlib.suppress(BaseException):
                await self._run_task
        for worker in self.workers:
            with contextlib.suppress(Exception):
                worker.agent.tools.close()
        if self.moderator is not None:
            with contextlib.suppress(Exception):
                self.moderator.agent.tools.close()
        with contextlib.suppress(Exception):
            self.registry.close()


@dataclass
class _UserBroadcast:
    id: int
    content: str


def _clone_status(status: WorkerStatus) -> WorkerStatus:
    from .events import clone_worker_status

    return clone_worker_status(status)


def _file_rank(status: str) -> int:
    return {"reviewed": 3, "skipped": 2, "reviewing": 1}.get(status, 0)


def stage_assignment(stage: str, count: int) -> str:
    assignment = (
        "本阶段有 %d 个独立 Agent。系统不预设角色、主题或文件范围。先调用 forum_roster 和 forum_threads 查看同伴，"
        "再通过 forum_post 提出候选分工；需要强提醒所有 Agent 时在正文加入 @全体成员（也支持 @all），"
        "收到提醒的 Agent 自主决定是否回应；随后用 forum_wait 等待同伴回复（等待有界，超时后继续），"
        "根据实际回复自行协商认领范围，避免重复并主动覆盖空白。不要把内部路由 ID 或启动顺序当作分工。"
        "名字从预制昵称表中即时分配，重复加001等后缀，不调用模型命名。" % count
    )
    if stage == phase_audit:
        assignment += (
            "这是全新的审计团队：先调用 read_handoff 按需读取结构化侦察资料，"
            "再根据论坛协商结果用 file_review_update 自行选择需要审计的文件；不要假设系统预先分配了任何文件。"
        )
    else:
        assignment += (
            "当前是侦察阶段：自行选择建图重点，并把跨范围问题发到论坛；只提交结构化侦察交接，不提交漏洞结论。"
        )
    return assignment


def _empty_audit():
    from ..tools import AuditState

    return AuditState()


def _audit_ended(decision: Any):
    from ..tools import AuditState
    from .events import field_value

    return AuditState(
        ended=True,
        summary=str(field_value(decision, "summary", "") or ""),
        next_steps=str(field_value(decision, "next_steps", "") or ""),
    )
