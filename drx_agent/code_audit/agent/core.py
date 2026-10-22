"""One independent audit agent: model loop, tool turn, verification."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from typing import Any, Callable

from .. import tools as tools_pkg
from ..forum import is_tool as forum_is_tool
from ..text import sanitize_text
from . import context as ctxmod
from .broadcast import broadcast_message, irc_error, irc_result, validate_user_broadcast
from .context import (
    add_message,
    audit_completion_policy,
    bounded_text,
    commit_response_continuation,
    estimate_text_tokens,
    estimate_tokens,
    invalidate_response_continuation,
    prepare_request,
    remove_think_blocks,
    request_kwargs,
    sanitize_messages_for_compression,
    state_prompt,
    system_prompt,
)
from .events import (
    Emit,
    Event,
    phase_audit,
    phase_moderator,
    phase_recon,
)
from .native import (
    audit_plan_accepted,
    execute_native_tool,
    reject_tool,
    tool_definition_tokens,
    tool_definitions,
    record_response,
    validate_native_response,
)
from .retry import ModelRequestRunner, is_context_length_error
from .trace import TraceLog


class Agent:
    """A single conversation with its own history, tools and trace."""

    def __init__(
        self,
        cfg: Any,
        prompts: Any,
        client: Any,
        compress_client: Any,
        registry: Any,
        agent_id: str,
        phase: str,
        board: Any = None,
    ) -> None:
        self.cfg = cfg
        self.prompts = prompts
        self.client = client
        self.compress_client = compress_client or client
        self.tools = registry
        self.id = agent_id
        self.phase = phase
        self.board = board

        self.announced_name = ""
        self.trace: TraceLog | None = None
        self.messages: list[Any] = []
        self.response_id = ""
        self.response_message_index = 0
        self.pending_end_audit = False
        self.trace_path = ""
        self.trace_err: BaseException | None = None
        self.trace_bootstrapped = False
        self.assignment = ""
        self.plan: Any = None
        self.completed = False
        self.run_err: BaseException | None = None
        self.turn = 0
        self.forum_cursor = 0
        self.forum_pending = ""
        self.user_broadcast_source: Callable[[int], list[Any]] | None = None
        self.user_broadcast_cursor = 0
        self.user_broadcast_delivered = 0
        self.user_broadcast_version = 0
        self.user_broadcast_messages: list[Any] = []
        self.user_broadcast_tokens = 0
        self.verifying = False
        self.tools_disabled = False
        self.protocol_failures = 0
        self.checkpoint: Callable[[], None] | None = None
        self.moderate_tool = None
        self.moderator_control = None
        self.review_report = None
        self.on_disconnect = None
        self.activity_cancel = None
        self.deliver_irc = None
        self.irc_tool = None
        self.irc_only = False
        self.irc_pending = None
        self.emit: Emit | None = None
        self.activity_register = None
        self.activity_clear = None
        self.team = None
        self.handoff = ""
        self.wait_retry = None
        self._activity_interrupted = False
        self._force_trace_bootstrap = True

    # ------------------------------------------------------------------
    # Trace
    # ------------------------------------------------------------------
    def ensure_trace(self) -> TraceLog | None:
        if not self.cfg.agent.log_session:
            return None
        if self.trace is not None:
            return self.trace
        try:
            if self.trace_path:
                self.trace = TraceLog.resume(self.trace_path)
            else:
                self.trace = TraceLog.create(self.cfg.agent.log_session_dir)
        except OSError as exc:
            self.trace_err = exc
            return None
        self.trace_path = self.trace.path
        self.trace_err = None
        return self.trace

    def bootstrap_trace(self) -> None:
        log = self.ensure_trace()
        if log is None or self.trace_bootstrapped:
            return
        try:
            if log.new_file:
                for message in self.messages:
                    log.append_message(message)
        except OSError as exc:
            self.trace_err = exc
            return
        self.trace_bootstrapped = True

    def append_trace_message(self, message: Any) -> None:
        if not self.cfg.agent.log_session:
            return
        if not self.trace_bootstrapped:
            self.bootstrap_trace()
        log = self.ensure_trace()
        if log is None:
            return
        try:
            log.append_message(message)
        except OSError as exc:
            self.trace_err = exc

    def trace_path_value(self) -> str:
        if self.trace is not None:
            return self.trace.path
        return self.trace_path

    # ------------------------------------------------------------------
    # History and prompts
    # ------------------------------------------------------------------
    def sanitize_messages(self) -> None:
        invalidate_response_continuation(self)
        if not self.messages:
            from ..llm import Message

            self.messages = [Message(role="system", content=self.system_prompt())]
            return
        cleaned = []
        seen_system = False
        for message in self.messages:
            if getattr(message, "role", "") == "system":
                if seen_system:
                    continue
                seen_system = True
            cleaned.append(message)
        from ..llm import Message

        if not cleaned or getattr(cleaned[0], "role", "") != "system":
            cleaned.insert(0, Message(role="system", content=self.system_prompt()))
        else:
            cleaned[0] = Message(role="system", content=self.system_prompt())
        from .session import repair_native_history

        self.messages = repair_native_history(cleaned)

    def system_prompt(self) -> str:
        return system_prompt(self)

    def add_message(self, message: Any) -> None:
        add_message(self, message)

    def emit_state(self, emit: Emit) -> None:
        if self.checkpoint is not None:
            self.checkpoint()
        snapshot = self.tools.snapshot()
        emit(
            Event(
                kind="state",
                phase=self.phase,
                skills=self.prompts.loaded_skill_names(),
                todos=snapshot.todos,
                findings=snapshot.findings,
                project=getattr(snapshot, "project_note", None),
                files=snapshot.files,
                variables=snapshot.variables,
                flows=snapshot.flows,
                audit=snapshot.audit,
            )
        )

    # ------------------------------------------------------------------
    # Broadcast channel
    # ------------------------------------------------------------------
    async def refresh_user_broadcasts(self) -> bool:
        if self.user_broadcast_source is None:
            return False
        pending = self.user_broadcast_source(self.user_broadcast_version) or []
        for broadcast in pending:
            self.user_broadcast_messages.append(broadcast_message(broadcast))
            self.user_broadcast_version = int(getattr(broadcast, "id", 0))
        if pending:
            self.user_broadcast_tokens = estimate_tokens(self.user_broadcast_messages)
        return bool(pending)

    def pending_user_broadcast_tokens(self) -> int:
        return estimate_tokens(self.user_broadcast_messages[self.user_broadcast_cursor :])

    def append_user_broadcasts(self) -> None:
        if self.user_broadcast_cursor == self.user_broadcast_version:
            return
        for message in self.user_broadcast_messages[self.user_broadcast_cursor :]:
            self.add_message(message)
        self.user_broadcast_cursor = self.user_broadcast_version
        if self.checkpoint is not None:
            self.checkpoint()

    def admit_user_broadcasts(self) -> None:
        if not self.user_broadcast_messages:
            return
        limit = self.cfg.compression_threshold()
        from ..llm import Message

        minimum = [Message(role="system", content=self.system_prompt())]
        if estimate_tokens(minimum) + self.user_broadcast_tokens + tool_definition_tokens(self) >= limit:
            raise RuntimeError(
                "系统提示、完整用户广播与当前工具定义超过上下文预算；用户指令未截断或丢弃，请提高模型上下文预算"
            )

    def user_broadcasts_delivered(self) -> None:
        if self.user_broadcast_delivered == self.user_broadcast_cursor:
            return
        self.user_broadcast_delivered = self.user_broadcast_cursor
        if self.checkpoint is not None:
            self.checkpoint()

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------
    def end_audit_needs_confirmation(self) -> str:
        files = [
            file
            for file in self.tools.snapshot().files
            if getattr(file, "status", "") in ("unseen", "reviewing")
        ]
        if not files:
            self.pending_end_audit = False
            return ""
        if self.pending_end_audit:
            self.pending_end_audit = False
            return ""
        self.pending_end_audit = True
        lines = ["- [%s] %s" % (getattr(f, "status", ""), getattr(f, "path", "")) for f in files]
        if len(lines) > 80:
            lines = lines[:80] + ["- ...还有 %d 个文件未列出" % (len(lines) - 80)]
        return ctxmod.render(self, "end_audit_confirmation", {"files": "\n".join(lines)})

    async def call_tool(self, emit: Emit, call: Any) -> tuple[str, str]:
        name = getattr(call, "name", "")
        raw = getattr(call, "arguments", "") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if name != "read_tool_buffer":
            from .native import clear_registry_buffer

            clear_registry_buffer(self)

        if name in ("moderator_irc_send", "moderator_irc_read", "worker_irc_reply"):
            result = (
                json.dumps({"ok": False, "error": "IRC unavailable"}, ensure_ascii=False)
                if self.irc_tool is None
                else await self.irc_tool(name, args)
            )
        elif name in ("moderator_review_state", "moderator_revoke_finding"):
            result = (
                json.dumps({"ok": False, "error": "moderator permission required"})
                if self.moderate_tool is None
                else await self.moderate_tool(name, args)
            )
        elif name in ("moderator_idle", "moderator_decide", "moderator_assign"):
            result = (
                json.dumps({"ok": False, "error": "moderator permission required"})
                if self.moderator_control is None
                else await self.moderator_control(name, args)
            )
        elif forum_is_tool(name):
            if self.board is None:
                result = json.dumps({"ok": False, "error": "forum unavailable"})
            else:
                if name == "forum_wait":
                    self.board.set_status(self.id, "waiting")
                    emit(Event(kind="waiting", content="等待论坛回复"))
                result = await self._forum_call(name, args)
                if name == "forum_wait":
                    self.board.set_status(self.id, "running")
                    emit(Event(kind="worker", content="论坛等待结束"))
        elif name == "read_handoff":
            result = self.handoff if hasattr(self, "handoff") else ""
            if not result:
                result = json.dumps({"ok": False, "error": "no prior stage handoff"})
        elif name == "load_skill":
            result = self.load_skill(args)
        elif name == "audit_plan_done":
            result = self.audit_plan_done(args)
        elif name == "verify_finding":
            result = await self.verify_finding(emit, args)
        elif name == "report_finding" and self.review_report is not None:
            result = await self.review_report(raw if isinstance(raw, str) else json.dumps(args))
        elif name == "end_audit":
            result = await self.request_end_audit(args)
        else:
            bounded, full = await _call_registry(self.tools, name, args)
            return bounded, full
        bounded = self._bound_result(name, result)
        return bounded, result

    async def _forum_call(self, name: str, args: dict[str, Any]) -> str:
        import inspect

        from .events import maybe_await

        call = self.board.call
        if name in ("forum_read", "forum_wait") and not inspect.iscoroutinefunction(call):
            # These tools wait on a threading condition; running them in the
            # event loop would freeze rendering and every other agent.
            return await _run_blocking(call, None, self.id, self.phase, name, args)
        return await maybe_await(call(None, self.id, self.phase, name, args))

    def _bound_result(self, name: str, result: str) -> str:
        from .native import bound_registry_result

        return bound_registry_result(self, name, result)

    def load_skill(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return json.dumps({"ok": False, "error": "name is required"}, ensure_ascii=False, indent=2)
        if not self.prompts.has_skill(name):
            return json.dumps(
                {"ok": False, "error": "unknown skill: " + name}, ensure_ascii=False, indent=2
            )
        loaded = self.prompts.load_skill(name)
        data: dict[str, Any] = {
            "name": name,
            "loaded": loaded,
            "skills": self.prompts.loaded_skill_names(),
        }
        result: dict[str, Any] = {"ok": True, "data": data}
        if loaded:
            for skill in self.prompts.loaded_skills():
                if skill.name == name:
                    data["content"] = skill.content
                    result["message"] = "skill loaded"
                    break
        else:
            result["message"] = "skill already loaded"
        return json.dumps(result, ensure_ascii=False, indent=2)

    def audit_plan_done(self, args: dict[str, Any]) -> str:
        try:
            plan = _decode_plan_args(args)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)
        applied = self.tools.apply_audit_scope(plan["audit_files"])
        if not applied:
            return json.dumps(
                {
                    "ok": False,
                    "error": "audit_files did not match any files in the current workspace; use exact relative paths from review_state/list_files/search_content",
                },
                ensure_ascii=False,
                indent=2,
            )
        plan["audit_files"] = list(applied)
        self.plan = plan
        return json.dumps(
            {
                "ok": True,
                "data": plan,
                "message": "侦察交接已提交；等待其余侦察 Agent 完成后启动审计团队",
            },
            ensure_ascii=False,
            indent=2,
        )

    async def request_end_audit(self, args: dict[str, Any]) -> str:
        if self.cfg.agent.infinite_mode:
            return irc_error("无限模式不允许模型结束审计")
        team = getattr(self, "team", None)
        summary = str(args.get("summary") or "")
        next_steps = str(args.get("next_steps") or "")
        vote = str(args.get("vote") or "")
        if team is None or self.board is None:
            _, full = await _call_registry(self.tools, "end_audit", args)
            return full
        decision = team.board.request_close(self.id, self.phase, summary, next_steps, vote)
        from .events import field_value

        if field_value(decision, "status") == "approved":
            _, full = await _call_registry(self.tools, "end_audit", args)
            return full
        message = (
            "end_audit 关闭请求未获全体 Agent 明确同意；继续自己的审计：自行选择尚未覆盖的其他文件、入口或模块，"
            "建立具体待办并读取源码；不要等待、催票、反复请求关闭或围绕同伴结论重复复核。"
        )
        reason = field_value(decision, "reason")
        if reason:
            message += " " + str(reason)
        from .session import snapshot_to_dict

        return json.dumps(
            {"ok": False, "data": snapshot_to_dict(decision), "message": message},
            ensure_ascii=False,
            indent=2,
        )

    # ------------------------------------------------------------------
    # Verification sub-agent
    # ------------------------------------------------------------------
    def verification_turn_limit(self) -> int:
        return 8

    async def verify_finding(self, emit: Emit, args: dict[str, Any]) -> str:
        try:
            candidate = _decode_verify_args(args)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)
        registry = self.tools.fork()
        registry.restore_snapshot(self.tools.snapshot())
        child_prompts = self.prompts.clone()
        child_id = "%s-verify-%d" % (self.id, self.turn)
        child = Agent(
            self.cfg,
            child_prompts,
            self.client,
            self.compress_client,
            registry,
            child_id,
            phase_audit,
            self.board,
        )
        child.user_broadcast_source = self.user_broadcast_source
        child.on_disconnect = self.on_disconnect
        child.verifying = True
        child.assignment = (
            "独立复核候选漏洞，论坛内容只作为线索，必须亲自读取源码验证。"
            "read_handoff 包含完整候选证据和父 Agent 审计状态；摘要未显示的证据必须按需读取。"
        )
        prior = getattr(self, "handoff", "")
        child.handoff = json.dumps(
            {
                "ok": True,
                "data": {
                    "candidate": candidate,
                    "parent_state": _snapshot_dict(self.tools.snapshot()),
                    "recon_handoff": prior or None,
                },
            },
            ensure_ascii=False,
        )
        child.messages = []
        child.sanitize_messages()
        emit(
            Event(
                kind="verify_progress",
                verify_title=candidate.get("title", ""),
                verify_turn=0,
                verify_limit=child.verification_turn_limit(),
                verify_status="准备验证",
            )
        )
        error: BaseException | None = None
        conclusion = ""
        try:
            conclusion = await child.run_verification(emit, candidate)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            error = exc
        if self.board is not None:
            status = "completed"
            if error is not None:
                status = "failed"
            self.board.set_status(child.id, status)
        if error is None:
            verify_status = "验证完成"
        else:
            verify_status = "验证失败"
        emit(
            Event(
                kind="verify_done",
                verify_title=candidate.get("title", ""),
                verify_limit=child.verification_turn_limit(),
                verify_status=verify_status,
            )
        )
        if error is not None:
            return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2)
        return json.dumps(
            {
                "ok": True,
                "data": {
                    "title": candidate.get("title", ""),
                    "path": candidate.get("path", ""),
                    "line": candidate.get("line", 0),
                    "conclusion": conclusion,
                },
                "message": "verification completed",
            },
            ensure_ascii=False,
            indent=2,
        )

    async def run_verification(self, emit: Emit, candidate: dict[str, Any]) -> str:
        self.verifying = True
        verify_prompt = ctxmod.render(
            self,
            "verify_finding",
            {
                "title": str(candidate.get("title") or ""),
                "severity": str(candidate.get("severity") or ""),
                "path": str(candidate.get("path") or ""),
                "line": str(candidate.get("line") or 0),
                "evidence": str(candidate.get("evidence") or ""),
                "impact": str(candidate.get("impact") or ""),
                "recommendation": str(candidate.get("recommendation") or ""),
                "cwe": str(candidate.get("cwe") or ""),
                "review_state": bounded_text(self, "review_state", self.tools.review_prompt(80)),
            },
        )
        from ..llm import Message

        self.add_message(
            Message(role="user", content=bounded_text(self, "verification_input", verify_prompt))
        )
        max_turns = self.verification_turn_limit()
        for turn in range(1, max_turns + 1):
            emit(
                Event(
                    kind="verify_progress",
                    verify_title=str(candidate.get("title") or ""),
                    verify_turn=turn,
                    verify_limit=max_turns,
                    verify_status="等待子 agent 响应",
                )
            )
            answer = await self.chat_stream(lambda _delta: None)
            call, ok = record_response(self, answer)
            if not ok:
                raise RuntimeError("验证 Agent 没有返回原生工具调用")
            emit(
                Event(
                    kind="verify_progress",
                    verify_title=str(candidate.get("title") or ""),
                    verify_turn=turn,
                    verify_limit=max_turns,
                    verify_status=_describe_verification_call(call),
                )
            )
            await execute_native_tool(self, emit, call)
            if len(getattr(answer, "calls", None) or []) > 1:
                from .native import record_skipped_calls

                record_skipped_calls(self, list(answer.calls))
        emit(
            Event(
                kind="verify_progress",
                verify_title=str(candidate.get("title") or ""),
                verify_turn=max_turns,
                verify_limit=max_turns,
                verify_status="达到上限，强制总结",
            )
        )
        self.add_message(
            Message(
                role="user",
                content=(
                    "已经达到验证轮数上限。现在禁止继续调用工具，请立刻基于已有证据输出最终中文验证结论，"
                    "按约定格式总结是否成立、是否建议提交、原因、利用链复核、关键证据和仍需补充。"
                ),
            )
        )
        self.tools_disabled = True
        emit(
            Event(
                kind="verify_progress",
                verify_title=str(candidate.get("title") or ""),
                verify_turn=max_turns,
                verify_limit=max_turns,
                verify_status="达到上限，正在强制总结",
            )
        )
        answer = await self.verification_conclusion()
        self.forum_pending = ""
        if not remove_think_blocks(answer).strip():
            raise RuntimeError("验证请求已完成但没有最终结论")
        self.add_message(Message(role="assistant", content=answer))
        return answer

    async def verification_conclusion(self) -> str:
        runner = ModelRequestRunner(lambda _e: None, self.on_disconnect, self.wait_retry)

        async def request() -> Any:
            await prepare_request(self, lambda _e: None)
            messages = sanitize_messages_for_compression(self.messages)
            answer = await self.client.chat(messages, emit=None)
            self.user_broadcasts_delivered()
            return answer

        result = await runner.run(request)
        return result if isinstance(result, str) else str(result)

    wait_retry = None

    # ------------------------------------------------------------------
    # Model request loop
    # ------------------------------------------------------------------
    async def chat_stream(self, emit: Emit) -> Any:
        from ..llm import Message

        if not self.messages:
            self.sanitize_messages()
        while True:
            context_retried = False

            async def request() -> Any:
                nonlocal context_retried
                try:
                    return await self._chat_stream_once(emit)
                except BaseException as exc:  # noqa: BLE001 - classified by the runner
                    if is_context_length_error(exc) and not context_retried:
                        context_retried = True
                        await self.compress_context(emit, "model context limit exceeded")
                        return await self._chat_stream_once(emit)
                    raise

            runner = ModelRequestRunner(emit, self.on_disconnect, self.wait_retry)
            try:
                response = await runner.run(request)
            except asyncio.CancelledError:
                if self._activity_interrupted:
                    self._activity_interrupted = False
                    continue
                raise
            try:
                validate_native_response(response, self.messages)
                calls = list(getattr(response, "calls", None) or [])
                if not calls:
                    raise ValueError(
                        "请求已完成但没有原生工具调用（回答 %d 字节，推理 %d 字节），不是网络停滞"
                        % (
                            len(sanitize_text(getattr(response, "content", "") or "").encode("utf-8")),
                            len(sanitize_text(getattr(response, "thinking", "") or "").encode("utf-8")),
                        )
                    )
                allowed = any(item.name == calls[0].name for item in tool_definitions(self))
                if not allowed:
                    call, _ = record_response(self, response)
                    if getattr(call, "name", "") != "read_tool_buffer":
                        from .native import clear_registry_buffer

                        clear_registry_buffer(self)
                    reject_tool(self, call, "当前角色未声明或不允许该工具：" + getattr(call, "name", ""))
                    raise ValueError("当前角色未声明或不允许该工具：" + getattr(call, "name", ""))
            except ValueError as exc:
                calls = list(getattr(response, "calls", None) or [])
                text = _join(getattr(response, "thinking", "") or "", getattr(response, "content", "") or "")
                if text:
                    self.add_message(Message(role="assistant", content=text))
                self.protocol_failures += 1
                feedback = (
                    "原生工具协议连续失败（第 %d 次）：%s。你刚才的工具调用无效，必须立即纠正后继续工作。"
                    "必须调用一个当前 API 声明的原生工具，类型 function_call；arguments 必须是完整 JSON 对象；"
                    "API 返回非空 call_id。不要输出 XML/DSML，也不要把 JSON 示例写在普通回答中代替调用。"
                    "具体示例：通过原生 function_call 调用 name=read_handoff，arguments={}"
                    "（call_id 由原生 API 调用记录提供，不要猜测旧 ID）。每回合只能一个工具；必须实际调用工具后继续。"
                    % (self.protocol_failures, exc)
                )
                self.add_message(Message(role="user", content=feedback))
                emit(Event(kind="info", content=feedback))
                continue
            self.protocol_failures = 0
            return response

    async def _chat_stream_once(self, emit: Emit) -> Any:
        await prepare_request(self, emit)
        content_buf: list[str] = []
        think_buf: list[str] = []
        state = {"content": False, "thinking": False}

        def flush() -> None:
            if think_buf:
                emit(Event(kind="think_delta", content="".join(think_buf)))
                think_buf.clear()
            if content_buf:
                emit(Event(kind="assistant_delta", content="".join(content_buf)))
                content_buf.clear()

        def on_delta(delta: Any) -> None:
            progress = getattr(delta, "progress", None)
            if progress is not None:
                emit(Event(kind="model_progress", generation=progress))
            content = getattr(delta, "content", "") or ""
            thinking = getattr(delta, "thinking", "") or ""
            if content:
                state["content"] = True
                content_buf.append(content)
            if thinking:
                state["thinking"] = True
                think_buf.append(thinking)
            if sum(len(item) for item in content_buf + think_buf) >= 512:
                flush()

        kwargs = request_kwargs(self)
        response = await self.client.chat_tools(
            self.messages, tool_definitions(self), emit=on_delta, **kwargs
        )
        flush()
        if not state["content"] and getattr(response, "content", ""):
            emit(Event(kind="assistant_delta", content=response.content))
        if not state["thinking"] and getattr(response, "thinking", ""):
            emit(Event(kind="think_delta", content=response.thinking))
        self.forum_pending = ""
        self.user_broadcasts_delivered()
        return response

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------
    async def compress_if_needed(self, emit: Emit) -> None:
        limit = self.cfg.compression_threshold()
        if estimate_tokens(self.messages) + tool_definition_tokens(self) + self.pending_user_broadcast_tokens() < limit:
            return
        await self.compress_context(emit, "estimated context budget reached")

    async def compress_context(self, emit: Emit, reason: str) -> None:
        emit(Event(kind="tool", content="compressing context: " + reason))
        messages = list(self.messages)
        if messages and getattr(messages[0], "role", "") == "system":
            messages = messages[1:]
        await self.compress_messages(emit, reason, messages)

    async def compress_messages(self, emit: Emit, reason: str, messages: list[Any]) -> None:
        from ..llm import Message

        limit = self.cfg.compression_threshold()
        budget = self.cfg.compression_input_budget()
        state = bounded_text(self, "review_state", state_prompt(self, 80))
        resume_template = "plan_resume_after_compress" if self.phase == phase_recon else "resume_after_compress"
        replacement = [
            Message(role="system", content=self.system_prompt()),
            Message(role="assistant", content="Compressed audit context:\n"),
            Message(role="user", content=ctxmod.render(self, "state_after_compress", {"state": state})),
            Message(role="user", content=ctxmod.render(self, resume_template)),
        ]
        if self.irc_pending is not None:
            pending = self.irc_pending()
            if pending:
                replacement.append(
                    Message(
                        role="user",
                        content="压缩后保留的已投递待答 IRC（不是新投递；先用 worker_irc_reply 明确回答再继续）："
                        + json.dumps([item.to_dict() for item in pending], ensure_ascii=False),
                    )
                )
        if self.phase == phase_moderator:
            replacement[3] = Message(
                role="user",
                content=(
                    "从压缩后的管理员上下文继续当前激活。你仍是独立论坛管理员，不是审计成员；"
                    "继续证据审查，仅在显式 review_stage 内给出 moderator_decide，完成后 moderator_idle。"
                ),
            )
        elif self.verifying:
            replacement[3] = Message(
                role="user",
                content="继续独立复核候选漏洞，只读取必要证据，最后输出验证结论；不得提交漏洞、修改审计状态或结束团队阶段。",
            )
        if self.forum_pending:
            replacement.append(Message(role="user", content=self.forum_pending))
        replacement.extend(self.user_broadcast_messages[: self.user_broadcast_cursor])
        tool_tokens = tool_definition_tokens(self)
        summary_budget = limit // 4
        available = limit - estimate_tokens(replacement) - tool_tokens - self.pending_user_broadcast_tokens() - 1
        if available < summary_budget:
            summary_budget = available
        if summary_budget <= 0:
            raise RuntimeError("系统提示与压缩后状态仍超过上下文预算，请调整模型上下文/输出配置")

        def request_for(history: list[Any]) -> list[Any]:
            pieces = [
                "Compression reason: %s\nKeep the summary within %d estimated tokens; prioritize remaining tasks and exact evidence.\n\n%s\nConversation to compress:\n"
                % (reason, summary_budget, state)
            ]
            for message in history:
                pieces.append("%s:\n%s\n\n" % (getattr(message, "role", ""), getattr(message, "content", "")))
            return [
                Message(role="system", content=self.prompts.compress),
                Message(
                    role="user",
                    content=ctxmod.render(
                        self, "compress_user", {"state_and_conversation": "".join(pieces)}
                    ),
                ),
            ]

        request = request_for([])
        base_cost = estimate_tokens(request)
        if base_cost > budget:
            raise RuntimeError(
                "压缩模型完整基础请求超过输入预算，请降低输出上限或提高 compress_openai.max_context_tokens"
            )
        history = ctxmod.compression_history(self, messages, budget - base_cost)
        request = request_for(history)
        while estimate_tokens(request) > budget and history:
            history = history[1:]
            request = request_for(history)
        if estimate_tokens(request) > budget:
            raise RuntimeError("压缩模型完整请求超过输入预算")
        compressed = ""
        while True:
            try:
                compressed = await self._chat_compress(request, emit)
                break
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - classified below
                if not is_context_length_error(exc):
                    raise
                if not history:
                    raise RuntimeError(
                        "压缩已移除全部历史，服务端仍拒绝最小请求；请检查压缩模型上下文及输出配置: %s" % exc
                    ) from exc
                drop = (len(history) + 3) // 4
                history = history[drop:]
                request = request_for(history)
                emit(
                    Event(
                        kind="info",
                        content="压缩请求超限，移除最早 %d 组会话/工具记录后重试，保留 %d 组"
                        % (drop, len(history)),
                    )
                )
        if not (compressed or "").strip():
            raise RuntimeError("压缩模型返回空摘要，原始上下文已保留")
        summary = bounded_text(self, "compressed_context", compressed)
        if estimate_text_tokens(summary) > summary_budget:
            raise RuntimeError("压缩摘要超过恢复预算，原始上下文已保留")
        replacement[1] = _replace_content(replacement[1], replacement[1].content + summary)
        if (
            estimate_tokens(replacement) + tool_tokens + self.pending_user_broadcast_tokens()
            >= limit
        ):
            raise RuntimeError("系统提示与压缩后状态仍超过上下文预算，请调整模型上下文/输出配置")
        self.messages = replacement
        invalidate_response_continuation(self)
        emit(Event(kind="ui_compact", content="## 上下文已压缩\n\n" + compressed))
        self.emit_state(emit)

    async def _chat_compress(self, request: list[Any], emit: Emit) -> str:
        runner = ModelRequestRunner(emit, self.on_disconnect, self.wait_retry)

        async def call() -> Any:
            return await self.compress_client.chat(request, emit=None)

        result = await runner.run(call)
        return result if isinstance(result, str) else str(result)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self, input_text: str, emit: Emit) -> None:
        self.run_err = None
        self.protocol_failures = 0
        self.completed = False
        self.emit = emit
        self.sanitize_messages()
        self.bootstrap_trace()
        initial_template = "initial_plan_instruction" if self.phase == phase_recon else "initial_audit_instruction"
        initial = ctxmod.render(
            self,
            initial_template,
            {
                "input": input_text,
                "review_state": (
                    "工作区已在本地索引。调用 review_state 获取当前文件地图和审计状态；"
                    "不要假设系统预先分配了角色或文件范围，先通过论坛协商后自行选择范围。"
                    "超长结果按需用 read_tool_buffer 读取。"
                ),
            },
        )
        handoff = getattr(self, "handoff", "")
        if handoff:
            initial += (
                "\n\n已有前一阶段结构化交接，请调用 read_handoff 按需读取完整地图、笔记、todo 与未决问题；"
                "不要假设已看过原始侦察对话。"
            )
        from ..llm import Message

        self.add_message(Message(role="user", content=initial))
        self.emit_state(emit)
        for turn in range(1, 1000000):
            if not self.cfg.agent.infinite_mode and self.cfg.agent.max_turns > 0:
                if turn > self.cfg.agent.max_turns:
                    self.run_err = RuntimeError("达到当前 worker 的 max_turns，阶段未完成；可用 go 继续")
                    emit(Event(kind="error", content=str(self.run_err)))
                    return
            self.turn += 1
            emit(Event(kind="turn"))
            try:
                outcome = await self._guarded_turn(lambda: self._one_turn(emit))
            except asyncio.CancelledError:
                self.run_err = asyncio.CancelledError()
                raise
            except BaseException as exc:  # noqa: BLE001 - surfaced to the team
                self.run_err = exc
                emit(Event(kind="error", content=str(exc)))
                return
            if outcome == "interrupted":
                continue
            if outcome == "completed":
                return

    async def _guarded_turn(self, factory):
        task = asyncio.create_task(factory())
        if self.activity_register is not None:
            self.activity_register(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            parent_cancelled = current is not None and current.cancelling()
            if parent_cancelled:
                # Let the inner turn finish its cancellation cleanup (a
                # cancelled tool result must be recorded) before the worker
                # checkpoint is captured.
                if not task.done():
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            if self._activity_interrupted:
                self._activity_interrupted = False
                return "interrupted"
            raise
        finally:
            if self.activity_clear is not None:
                self.activity_clear()

    async def _one_turn(self, emit: Emit):
        if self.deliver_irc is not None:
            await self.deliver_irc()
        try:
            answer = await self.chat_stream(emit)
        except asyncio.CancelledError:
            raise
        call, _ = record_response(self, answer)
        commit_response_continuation(self, answer)
        emit(Event(kind="assistant_done"))
        if call is None:
            return None
        try:
            full_result = await execute_native_tool(self, emit, call)
        except asyncio.CancelledError:
            self._record_cancelled_tool(call)
            raise
        calls = list(getattr(answer, "calls", None) or [])
        if len(calls) > 1 and getattr(answer, "multi_call_fallback", False):
            from ..llm import Message
            from .native import record_tool_output

            # Only the first call is executed.  Record explicit skipped
            # outputs for the rest so the native history (and any
            # previous_response_id continuation) stays complete.
            for skipped in calls[1:]:
                payload = json.dumps(
                    {
                        "ok": False,
                        "error": "未执行：每回合只允许一个工具调用，请下一回合重新发起。",
                    },
                    ensure_ascii=False,
                )
                record_tool_output(self, skipped, payload, payload)
            note = (
                "上一回合模型返回了 %d 个原生工具调用，但当前模型接口不能强制单工具调用；"
                "系统只执行了第一个，其余 %d 个未执行。每回合只能返回一个工具调用，"
                "请根据真实需要重新发起未执行的调用，不要一次并行多个工具。"
                % (len(calls), len(calls) - 1)
            )
            self.add_message(Message(role="user", content=note))
            emit(Event(kind="info", content=note))
        name = getattr(call, "name", "")
        if name == "audit_plan_done" and self.plan is not None and audit_plan_accepted(full_result):
            self.completed = True
        if name == "end_audit" and self.tools.snapshot().audit.ended and audit_plan_accepted(full_result):
            self.completed = True
        self.emit_state(emit)
        if self.completed:
            return "completed"
        return None

    def _record_cancelled_tool(self, call: Any) -> None:
        from ..llm import Message

        full = json.dumps({"ok": False, "cancelled": True, "error": "工具等待被 IRC 中断；结果未执行"}, ensure_ascii=False)
        bounded = self._bound_result(getattr(call, "name", ""), full)
        self.messages.append(
            Message(type="function_call_output", call_id=getattr(call, "call_id", ""), content=bounded)
        )
        self.append_trace_message(
            Message(type="function_call_output", call_id=getattr(call, "call_id", ""), content=full)
        )

    async def run_irc_only(self, emit: Emit) -> None:
        self.irc_only = True
        try:
            for _turn in range(16):
                if self.irc_pending is None or not self.irc_pending():
                    return
                self.turn += 1
                emit(Event(kind="turn"))
                try:
                    await self.deliver_irc()
                    answer = await self.chat_stream(emit)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    self.run_err = exc
                    emit(Event(kind="error", content=str(exc)))
                    return
                call, _ = record_response(self, answer)
                emit(Event(kind="assistant_done"))
                if call is not None:
                    await execute_native_tool(self, emit, call)
                    if len(getattr(answer, "calls", None) or []) > 1:
                        from .native import record_skipped_calls

                        record_skipped_calls(self, list(answer.calls))
                self.emit_state(emit)
            self.run_err = RuntimeError("IRC 未在16回合内明确回复")
        finally:
            self.irc_only = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_blocking(function: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking callable without occupying the event loop or its pool.

    A daemon thread is used instead of the default executor because forum
    waits can legitimately block for tens of seconds and must not starve model
    requests or delay process exit.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def deliver(result: Any, error: BaseException | None) -> None:
        if future.done() or loop.is_closed():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def worker() -> None:
        try:
            result = function(*args)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, None, exc)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, result, None)

    threading.Thread(
        target=worker, name="drx-audit-blocking", daemon=True
    ).start()
    return await future


_HEAVY_REGISTRY_TOOLS = {
    "read_file",
    "read_tool_buffer",
    "search_content",
    "search_context",
    "git_inspect",
    "list_files",
    "review_state",
}


async def _call_registry(registry: Any, name: str, args: Any) -> tuple[str, str]:
    from .events import maybe_await

    if isinstance(args, (dict, list)):
        args = json.dumps(args, ensure_ascii=False)
    # File, search and Git tools are synchronous and can take seconds; keep
    # them off the event loop so the terminal and sibling agents stay live.
    # State-mutating tools stay inline so checkpoints never race a worker.
    if name in _HEAVY_REGISTRY_TOOLS:
        result = await _run_blocking(
            registry.call_with_full_result, None, name, args
        )
    else:
        result = registry.call_with_full_result(None, name, args)
    value = await maybe_await(result)
    if isinstance(value, tuple):
        return value[0], value[1]
    return value, value


def _decode_plan_args(args: dict[str, Any]) -> dict[str, Any]:
    summary = str(args.get("summary") or "").strip()
    audit_map = str(args.get("audit_map") or "").strip()
    files = args.get("audit_files") or []
    if not isinstance(files, list):
        raise ValueError("audit_files must include concrete files selected for execute phase")
    if not summary:
        raise ValueError("summary is required")
    if not audit_map:
        raise ValueError("audit_map is required")
    if not files:
        raise ValueError("audit_files must include concrete files selected for execute phase")
    return {
        "summary": summary,
        "audit_map": audit_map,
        "audit_files": [str(item) for item in files],
        "execute_instructions": str(args.get("execute_instructions") or ""),
    }


def _decode_verify_args(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    path = str(args.get("path") or "").strip()
    evidence = str(args.get("evidence") or "").strip()
    if not title:
        raise ValueError("title is required")
    if not path:
        raise ValueError("path is required")
    if not evidence:
        raise ValueError("evidence is required")
    return {
        "severity": str(args.get("severity") or ""),
        "title": title,
        "path": path,
        "line": int(args.get("line") or 0),
        "evidence": evidence,
        "impact": str(args.get("impact") or ""),
        "recommendation": str(args.get("recommendation") or ""),
        "cwe": str(args.get("cwe") or ""),
    }


def _replace_content(message: Any, content: str) -> Any:
    try:
        from dataclasses import replace

        return replace(message, content=content)
    except Exception:  # pragma: no cover - mutable message fallback
        message.content = content
        return message


def _join(thinking: str, content: str) -> str:
    if not thinking:
        return content
    return "<think>" + thinking + "</think>" + content


def _describe_verification_call(call: Any) -> str:
    name = getattr(call, "name", "")
    try:
        args = json.loads(getattr(call, "arguments", "") or "{}")
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if name == "read_file":
        return "读取 " + _truncate(str(args.get("path") or ""), 24)
    if name == "search_content":
        return "搜索 " + _truncate(str(args.get("query") or ""), 24)
    if name == "review_state":
        return "查看当前排查状态"
    if name == "variable_review_update":
        return "记录变量 " + _truncate(str(args.get("name") or ""), 20)
    if name == "flow_review_update":
        return "记录链路 " + _truncate(str(args.get("name") or ""), 20)
    return name


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _snapshot_dict(snapshot: Any) -> dict[str, Any]:
    from .session import snapshot_to_dict

    return snapshot_to_dict(snapshot)


def new_worker(cfg, prompts, client, compress_client, registry, agent_id, phase, board=None) -> Agent:
    if compress_client is None:
        compress_client = client
    prompts.set_loaded_skills(prompts.loaded_skill_names())
    if board is not None:
        board.register(agent_id, phase)
        ensure = getattr(board, "ensure_preset_name", None)
        if callable(ensure):
            ensure(agent_id)
    return Agent(cfg, prompts, client, compress_client, registry, agent_id, phase, board)
