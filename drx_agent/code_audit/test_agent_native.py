"""Native tool protocol, role permissions and special tools."""

from __future__ import annotations

import asyncio
import json

import pytest

from drx_agent.code_audit import test_agent_support as s
from drx_agent.code_audit import tools
from drx_agent.code_audit.agent import native
from drx_agent.code_audit.agent.core import new_worker


def make_agent(phase="recon", verifying=False, workspace=None):
    ws = workspace or s.new_workspace()
    cfg = s.make_config(ws)
    registry = tools.Registry(ws, 1024)
    agent = new_worker(cfg, s.make_prompts(), None, None, registry, "%s-1" % phase, phase, None)
    agent.verifying = verifying
    return agent


def test_validate_native_response_rejects_multiple_calls():
    response = s.llm.ToolResponse(
        calls=[
            s.llm.FunctionCall(call_id="a", name="read_file", arguments="{}"),
            s.llm.FunctionCall(call_id="b", name="read_file", arguments="{}"),
        ]
    )
    with pytest.raises(ValueError) as error:
        native.validate_native_response(response, [])
    assert "只允许一个" in str(error.value)


def test_validate_native_response_allows_single_call_fallback():
    response = s.llm.ToolResponse(
        calls=[
            s.llm.FunctionCall(call_id="a", name="review_state", arguments="{}"),
            s.llm.FunctionCall(call_id="b", name="list_files", arguments="{}"),
        ],
        multi_call_fallback=True,
    )
    native.validate_native_response(response, [])


def test_multi_call_fallback_executes_first_call_and_warns():
    agent = make_agent()
    emitted = []

    def handler(messages, definitions, calls):
        return s.llm.ToolResponse(
            calls=[
                s.llm.FunctionCall(call_id="a", name="review_state", arguments="{}"),
                s.llm.FunctionCall(call_id="b", name="list_files", arguments="{}"),
            ],
            multi_call_fallback=True,
        )

    agent.client = s.ScriptedClient(handler)
    asyncio.run(agent._one_turn(emitted.append))
    recorded = [
        message.name
        for message in agent.messages
        if getattr(message, "type", "") == "function_call"
    ]
    assert recorded == ["review_state", "list_files"]
    outputs = [
        message.content
        for message in agent.messages
        if getattr(message, "type", "") == "function_call_output"
    ]
    assert len(outputs) == 2
    assert "未执行" in outputs[1]
    native.validate_native_history(agent.messages)
    notes = [
        message.content
        for message in agent.messages
        if getattr(message, "role", "") == "user"
        and "只执行了第一个" in (message.content or "")
    ]
    assert notes
    assert any(event.kind == "info" and "只执行了第一个" in (event.content or "") for event in emitted)
    assert agent.completed is False


@pytest.mark.parametrize(
    "call",
    [
        s.llm.FunctionCall(call_id="", name="read_file", arguments="{}"),
        s.llm.FunctionCall(call_id="a", name="", arguments="{}"),
        s.llm.FunctionCall(call_id="a", name="read_file", arguments="[]"),
        s.llm.FunctionCall(call_id="a", name="read_file", arguments=""),
    ],
)
def test_validate_native_response_rejects_incomplete_calls(call):
    with pytest.raises(ValueError):
        native.validate_native_response(s.llm.ToolResponse(calls=[call]), [])


def test_validate_native_response_rejects_duplicate_call_id_in_history():
    history = [
        s.llm.Message(type="function_call", call_id="dup", name="read_file", arguments="{}")
    ]
    with pytest.raises(ValueError) as error:
        native.validate_native_response(
            s.llm.ToolResponse(
                calls=[s.llm.FunctionCall(call_id="dup", name="read_file", arguments="{}")]
            ),
            history,
        )
    assert "重复 call_id" in str(error.value)


def test_validate_native_history_pairs():
    native.validate_native_history(
        [
            s.llm.Message(role="system", content="s"),
            s.llm.Message(type="function_call", call_id="c1", name="read_file", arguments="{}"),
            s.llm.Message(type="function_call_output", call_id="c1", content="{}"),
        ]
    )
    with pytest.raises(ValueError):
        native.validate_native_history(
            [s.llm.Message(type="function_call_output", call_id="c1", content="{}")]
        )
    with pytest.raises(ValueError):
        native.validate_native_history(
            [s.llm.Message(type="function_call", call_id="c1", name="read_file", arguments="{}")]
        )


def test_role_definition_boundaries():
    from drx_agent.code_audit.prompt import Skill

    recon = make_agent("recon")
    recon.prompts.skills = [Skill(name="web", content="use for http")]
    audit = make_agent("audit")
    verifier = make_agent("audit", verifying=True)
    names_recon = {item.name for item in native.tool_definitions(recon)}
    names_audit = {item.name for item in native.tool_definitions(audit)}
    names_verifier = {item.name for item in native.tool_definitions(verifier)}
    assert {"read_file", "read_handoff", "audit_plan_done", "load_skill"} <= names_recon
    assert "report_finding" not in names_recon
    assert "end_audit" not in names_recon
    assert {"report_finding", "end_audit", "verify_finding"} <= names_audit
    assert "audit_plan_done" not in names_audit
    assert "report_finding" not in names_verifier
    assert "read_file" in names_verifier
    assert native.tool_permission(recon, "moderator_idle")
    assert native.tool_permission(audit, "moderator_irc_send")
    assert native.tool_permission(recon, "end_audit")


def test_infinite_mode_hides_completion_tools():
    agent = make_agent("audit")
    agent.cfg.agent.infinite_mode = True
    names = {item.name for item in native.tool_definitions(agent)}
    assert "end_audit" not in names
    assert "moderator_decide" not in names
    assert "无限模式" in native.tool_permission(agent, "end_audit")


def test_tools_disabled_hides_every_tool():
    agent = make_agent("recon")
    agent.tools_disabled = True
    assert native.tool_definitions(agent) == []
    assert "工具调用已禁用" in native.tool_permission(agent, "read_file")


def test_audit_plan_done_validation_and_scope():
    agent = make_agent("recon")
    rejected = agent.audit_plan_done({"summary": "", "audit_map": "m", "audit_files": ["entry1.go"]})
    assert json.loads(rejected)["ok"] is False
    assert "summary is required" in json.loads(rejected)["error"]
    rejected = agent.audit_plan_done({"summary": "s", "audit_map": "m", "audit_files": []})
    assert "audit_files must include" in json.loads(rejected)["error"]
    rejected = agent.audit_plan_done({"summary": "s", "audit_map": "m", "audit_files": ["nope.go"]})
    assert json.loads(rejected)["ok"] is False
    accepted = json.loads(
        agent.audit_plan_done(
            {"summary": "s", "audit_map": "m", "audit_files": ["entry1.go"]}
        )
    )
    assert accepted["ok"] is True
    assert agent.plan["audit_files"] == ["entry1.go"]
    files = {item.path: item for item in agent.tools.snapshot().files}
    assert files["entry1.go"].status == "reviewing"


def test_load_skill_errors_and_success():
    from drx_agent.code_audit.prompt import Skill

    agent = make_agent("recon")
    agent.prompts.skills = [Skill(name="web", content="# Web\nUse for HTTP.")]
    assert json.loads(agent.load_skill({"name": ""}))["error"] == "name is required"
    assert "unknown skill" in json.loads(agent.load_skill({"name": "nope"}))["error"]
    loaded = json.loads(agent.load_skill({"name": "web"}))
    assert loaded["ok"] is True and loaded["message"] == "skill loaded"
    assert loaded["data"]["content"].startswith("# Web")
    again = json.loads(agent.load_skill({"name": "web"}))
    assert again["message"] == "skill already loaded"


def test_end_audit_confirmation_once_then_allows_retry():
    agent = make_agent("audit")
    agent.tools.files = [s.tools.FileReview(path="entry1.go", status="reviewing")]
    first = agent.end_audit_needs_confirmation()
    assert "请完成剩余文件" in first
    assert agent.pending_end_audit is True
    assert agent.end_audit_needs_confirmation() == ""
    assert agent.pending_end_audit is False
    agent.tools.files = []
    assert agent.end_audit_needs_confirmation() == ""


def test_record_response_keeps_native_pairing():
    agent = make_agent("recon")
    answer = s.tool_response("read_file", {"path": "entry1.go"}, call_id="call-1")
    call, ok = native.record_response(agent, answer)
    assert ok is True and call.call_id == "call-1"
    native.record_tool_output(agent, call, '{"ok":true}', '{"ok":true,"full":true}')
    native.validate_native_history(agent.messages)
    assert agent.messages[-1].content == '{"ok":true}'


def test_chat_stream_protocol_retry_then_success():
    agent = make_agent("recon")

    def handler(messages, definitions, calls):
        if calls <= 2:
            return s.llm.ToolResponse(
                calls=[s.llm.FunctionCall(call_id="", name="read_file", arguments="{}")]
            )
        return s.tool_response("read_file", {"path": "entry1.go"}, call_id="ok-1")

    client = s.ScriptedClient(handler)
    agent.client = client
    response = asyncio.run(agent.chat_stream(lambda event: None))
    assert response.calls[0].call_id == "ok-1"
    assert agent.protocol_failures == 0
    feedback = [m.content for m in agent.messages if "立即纠正" in (m.content or "")]
    assert len(feedback) == 2
    native.validate_native_history(agent.messages)


def test_execute_native_tool_rejects_disallowed_and_records_output():
    agent = make_agent("recon")
    call = s.llm.FunctionCall(call_id="bad-1", name="moderator_idle", arguments="{}")
    asyncio.run(native.execute_native_tool(agent, lambda event: None, call))
    outputs = [m for m in agent.messages if m.type == "function_call_output"]
    assert outputs and '"ok": false' in outputs[0].content
    assert "仅允许论坛管理员" in outputs[0].content or "不可用" in outputs[0].content


def test_sanitize_messages_repairs_unpaired_call():
    agent = make_agent("recon")
    agent.messages.append(
        s.llm.Message(
            type="function_call",
            call_id="orphan-call",
            name="read_file",
            arguments="{}",
        )
    )
    agent.sanitize_messages()
    native.validate_native_history(agent.messages)
    repaired = agent.messages[-1]
    assert repaired.type == "function_call_output"
    assert repaired.call_id == "orphan-call"
    assert "上次中断未产生工具执行结果" in repaired.content
