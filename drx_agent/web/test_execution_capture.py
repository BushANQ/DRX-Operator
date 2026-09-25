"""Execution metadata remains observational across concurrent calls and restore."""

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import json
import tempfile
from types import SimpleNamespace
import unittest

from drx_agent.agent.execution_context import advance_turn, execution_context, execution_scope, run_context
from drx_agent.agent.knowledge_base import KnowledgeBase
from drx_agent.agent.master import MasterAgent
from drx_agent.agent.steering import MessageSignal
from drx_agent.agent.sub_agent import SubAgent
from drx_agent.event_bus import EventBus, EventType
from drx_agent.llm.base import AgentEvent, AgentEventType
from drx_agent.session.manager import SessionManager
from drx_agent.tui.transcript import TranscriptLog


class ScriptedProvider:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests = []

    async def chat(self, messages, **_kwargs):
        self.requests.append(deepcopy(messages))
        for call_id, name, arguments in next(self.turns, []):
            yield AgentEvent(AgentEventType.TOOL_CALL, tool_name=name, tool_input=arguments,
                             metadata={"tool_call_id": call_id})
        yield AgentEvent(AgentEventType.DONE)


def master_fixture():
    master = MasterAgent.__new__(MasterAgent)
    master.event_bus = EventBus()
    master._actor = ContextVar("test_actor", default="master")
    master._tool_handoffs = ContextVar("test_handoffs", default=None)
    master._interrupt_epoch = 0
    master._script_counter = 0
    master._mail_paused = master._closing = master._restoring = master._interrupt = False
    master.stage_machine = SimpleNamespace(stage=SimpleNamespace(value="recon"))
    master.todos = []
    master._current_intent_id = None
    master.messages = []

    async def execute(_name, arguments, _sequence, _output):
        await asyncio.sleep(0)
        return json.dumps(arguments)

    master._execute_tool_impl = execute
    return master


def prepare_chat_loop(master, provider):
    master.llm_provider = provider
    master._message_signal = MessageSignal()
    master.iteration_soft_threshold = 0
    master._pack_notifications = lambda _actor: ""

    async def compact():
        return None

    master._maybe_compact_context = compact
    master.frontier = SimpleNamespace(prune_expired=lambda: None, _intents={})
    master._pending_observer_msg = None
    master.swarm_mode = False
    master._maybe_tick_moderator = lambda: None
    master._active_tool_schemas = lambda: []
    master._build_system_prompt = lambda: "test system"
    master._append_runtime_context = lambda: None
    master.active_sub_agents = {}
    master._team_members = {}
    master.forum = SimpleNamespace(pending=lambda: [])
    master._pending_team_irc = lambda: False
    master._record_usage = lambda *_args, **_kwargs: None
    master.llm_call_timeout = 5


class ExecutionCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_chat_session_reuses_request_only_for_continuations(self):
        master = master_fixture()
        master._session_generation = 0
        master._chat_requests = set()
        master._get_chat_lock = lambda: lock
        master._queue_mail = lambda _actor: None
        lock = asyncio.Lock()
        async with master._chat_session(new_request=True) as active:
            self.assertTrue(active)
            first = execution_context()
        async with master._chat_session() as active:
            self.assertTrue(active)
            continuation = execution_context()
        async with master._chat_session(new_request=True):
            following = execution_context()
        self.assertEqual(first["request_id"], continuation["request_id"])
        self.assertNotEqual(first["run_id"], continuation["run_id"])
        self.assertNotEqual(first["request_id"], following["request_id"])
        self.assertEqual({}, execution_context())

    async def test_single_agent_model_turns_and_parallel_calls_keep_provider_identity(self):
        master = master_fixture()
        log = TranscriptLog(master.event_bus)
        provider = ScriptedProvider([
            [("provider-a", "read_a", {"path": "a"}), ("provider-b", "read_b", {"path": "b"})],
            [("provider-c", "read_c", {"path": "c"})], [],
        ])
        prepare_chat_loop(master, provider)
        with execution_scope(run_context("master", "run-1", "request-1")):
            await master._chat_loop()
        records = [record for record in log.export() if record["kind"] == "tool"]
        self.assertEqual(["provider-a", "provider-b", "provider-c"], [record["tool_call_id"] for record in records])
        first, sibling, following = [record["execution_context"] for record in records]
        self.assertEqual(first["turn_id"], sibling["turn_id"])
        self.assertNotEqual(first["invocation_id"], sibling["invocation_id"])
        self.assertEqual(first["turn_id"], following["previous_turn_id"])
        self.assertIsNone(first["previous_turn_id"])
        self.assertEqual({"run-1"}, {context["run_id"] for context in (first, sibling, following)})
        self.assertEqual({"request-1"}, {context["request_id"] for context in (first, sibling, following)})
        self.assertTrue(all(record["started_at"] <= record["completed_at"] for record in records))
        self.assertTrue(all(record["execution_context"]["stage"] == "recon" for record in records))
        for record in records:
            self.assertEqual(record["tool_call_id"], record["data"]["tool_call_id"])
        for request in provider.requests:
            self.assertTrue(all("execution_context" not in message and "turn_id" not in message for message in request))
        self.assertEqual(["provider-a", "provider-b", "provider-c"], [m["tool_call_id"] for m in master.messages if m["role"] == "tool"])
        self.assertEqual({}, execution_context())
        log.close()

    async def test_call_context_isolated_while_tools_overlap(self):
        master = master_fixture()
        log = TranscriptLog(master.event_bus)
        entered = asyncio.Event()
        observations = []

        async def execute(_name, arguments, _sequence, _output):
            observations.append(execution_context())
            if len(observations) == 2:
                entered.set()
            await asyncio.wait_for(entered.wait(), 2)
            observations.append(execution_context())
            return json.dumps(arguments)

        master._execute_tool_impl = execute
        with execution_scope(run_context("master", "run", "request")):
            advance_turn("master")
            await master._run_chat_tools([{"id": "a", "name": "a", "input": {}}, {"id": "b", "name": "b", "input": {}}])
        self.assertEqual({"a", "b"}, {item["tool_call_id"] for item in observations})
        self.assertEqual(2, len({item["invocation_id"] for item in observations}))
        self.assertEqual(2, log.record_count)
        log.close()

    async def test_task_snapshot_is_frozen_and_ambiguous_tasks_are_not_selected(self):
        master = master_fixture()
        log = TranscriptLog(master.event_bus)
        master.todos = [{"id": "first", "content": "First task", "status": "in_progress"}]

        async def execute(_name, _arguments, _sequence, _output):
            master.todos[0]["status"] = "completed"
            master.stage_machine.stage.value = "report"
            return "ok"

        master._execute_tool_impl = execute
        await master._execute_tool("read", {})
        captured = log.export()[0]["execution_context"]
        self.assertEqual("first", captured["active_task"]["id"])
        self.assertEqual("in_progress", captured["active_task"]["status"])
        self.assertEqual("recon", captured["stage"])
        self.assertIsNone(captured["task_id"])
        master.todos = [{"id": "a", "status": "in_progress"}, {"id": "b", "status": "in_progress"}]
        await master._execute_tool("read", {})
        ambiguous = log.export()[1]["execution_context"]
        self.assertIsNone(ambiguous["active_task"])
        self.assertEqual(["a", "b"], [task["id"] for task in ambiguous["active_tasks"]])
        log.close()

    async def test_real_worker_dispatch_keeps_parent_and_sibling_contexts_separate(self):
        master = master_fixture()
        log = TranscriptLog(master.event_bus)
        workers = []
        master.todos = [{"id": "master-plan", "status": "in_progress"}]

        async def execute(name, arguments, _sequence, _output):
            if name != "dispatch":
                await asyncio.sleep(0)
                return json.dumps(arguments)
            for index in range(2):
                sub = SubAgent("worker", "local", f"Task {index}", master.event_bus,
                               llm_provider=ScriptedProvider([[("same-provider-call", "read", {"worker": index})], []]))
                sub.execution_task_id = f"intent-{index}"

                async def executor(tool, args, worker=sub):
                    token = master._actor.set(worker.agent_id)
                    try:
                        return await master._execute_tool(tool, args)
                    finally:
                        master._actor.reset(token)

                sub.tool_executor = executor
                sub.queue()
                workers.append(sub)
            await asyncio.gather(*(worker.run() for worker in workers))
            return "ok"

        master._execute_tool_impl = execute
        with execution_scope(run_context("master", "run", "request"), tool_call_id="parent-call"):
            advance_turn("master")
            with execution_scope(tool_call_id="parent-call"):
                await master._execute_tool("dispatch", {})
        records = log.export()
        parent = next(record for record in records if record.get("tool") == "dispatch")
        calls = [record for record in records if record.get("tool") == "read"]
        dispatches = [record for record in records if record["kind"] == "worker"]
        self.assertEqual(2, len(calls))
        self.assertEqual(2, len(dispatches))
        self.assertEqual(2, len({record["execution_context"]["turn_id"] for record in calls}))
        for record in calls:
            context = record["execution_context"]
            self.assertEqual(record["actor"], context["actor"])
            self.assertEqual(parent["invocation_id"], context["parent_invocation_id"])
            self.assertEqual("parent-call", context["parent_tool_call_id"])
            self.assertIsNone(context["active_task"])
            self.assertIn(context["task_id"], {"intent-0", "intent-1"})
            self.assertTrue(any(item["data"]["dispatch_id"] == context["dispatch_id"] for item in dispatches))
        log.close()

    async def test_inherited_context_from_a_finished_tool_cannot_parent_new_dispatch(self):
        master = master_fixture()
        observed = []

        async def execute(_name, _arguments, _sequence, _output):
            observed.append(execution_context())
            return "ok"

        master._execute_tool_impl = execute
        with execution_scope(run_context("master", "run", "request"), tool_call_id="expired-call"):
            await master._execute_tool("dispatch", {})
        with execution_scope(observed[0]):
            sub = SubAgent("worker", "local", "Later activation", master.event_bus)
            sub.queue()
        self.assertIsNone(sub._execution_capture["parent_invocation_id"])
        self.assertIsNone(sub._execution_capture["parent_tool_call_id"])

    async def test_reactivation_does_not_reuse_an_expired_parent_tool(self):
        master = master_fixture()
        dispatched = []
        master.event_bus.subscribe(EventType.SUB_AGENT_DISPATCH, lambda event: dispatched.append(deepcopy(event.data)))
        holder = []

        async def execute(_name, _arguments, _sequence, _output):
            sub = SubAgent("worker", "local", "Recorded task", master.event_bus)
            holder.append(sub)
            await sub.run()
            return "ok"

        master._execute_tool_impl = execute
        with execution_scope(run_context("master", "run", "request"), tool_call_id="parent-call"):
            await master._execute_tool("dispatch", {})
        self.assertIsNotNone(dispatched[0]["execution_context"]["parent_invocation_id"])
        sub = holder[0]
        runtime = sub.snapshot_runtime()
        restored = SubAgent("worker", "local", "Recorded task", master.event_bus)
        restored.agent_id = sub.agent_id
        restored.messages = runtime["messages"]
        restored.activation = runtime["activation"]
        restored._execution_capture = runtime["execution_capture"]
        restored._execution_parent = dict(runtime["execution_capture"])
        await restored.run()
        final = dispatched[-1]["execution_context"]
        self.assertIsNone(final["parent_invocation_id"])
        self.assertIsNone(final["parent_tool_call_id"])
        self.assertNotEqual(dispatched[0]["dispatch_id"], dispatched[-1]["dispatch_id"])

    async def test_cancellation_keeps_real_terminal_time_and_clears_scope(self):
        master = master_fixture()
        log = TranscriptLog(master.event_bus)
        started = asyncio.Event()

        async def execute(_name, _arguments, _sequence, _output):
            started.set()
            await asyncio.Event().wait()

        master._execute_tool_impl = execute
        with execution_scope(run_context("master", "run", "request")):
            task = asyncio.create_task(master._run_chat_tools([{"id": "cancel-call", "name": "wait", "input": {}}]))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        record = log.export()[0]
        self.assertEqual("cancelled", record["status"])
        self.assertEqual("cancel-call", record["tool_call_id"])
        self.assertGreaterEqual(record["completed_at"], record["started_at"])
        self.assertEqual({}, execution_context())
        log.close()


class CapturePersistenceTests(unittest.TestCase):
    def test_old_message_import_does_not_invent_execution_times_or_context(self):
        log = TranscriptLog(EventBus())
        log.restore_messages([
            {"role": "assistant", "content": "", "tool_calls": [{"id": "old-call", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "old-call", "content": "old output"},
        ])
        record = log.export()[0]
        self.assertNotIn("started_at", record)
        self.assertNotIn("completed_at", record)
        self.assertNotIn("execution_context", record)
        log.close()

    def test_snapshot_restores_capture_todo_hierarchy_and_configuration(self):
        todos = [{"id": "child", "content": "Recorded child", "status": "in_progress", "parent_id": "parent", "depends_on": ["before"]}]
        transcript = [{"id": "tool-record", "kind": "tool", "actor": "master", "tool": "read", "status": "done", "started_at": 12.5,
                       "completed_at": 13.5, "execution_context": {"run_id": "run", "request_id": "request"}, "data": {}}]
        capture = {"version": 1, "request_id": "request"}
        swarm = {"enabled": True, "batch_size": 2, "max_concurrent": 4}
        with tempfile.TemporaryDirectory() as directory:
            manager = SessionManager(directory)
            identity = manager.save(KnowledgeBase(), [], [], todos=todos, transcript=transcript, execution_capture=capture, swarm=swarm)
            restored = manager.restore(identity)
            self.assertEqual(capture, restored["execution_capture"])
            self.assertEqual(swarm, restored["swarm"])
            self.assertEqual(todos, restored["todos"])
            log = TranscriptLog(EventBus())
            log.restore(restored["transcript"])
            self.assertEqual(transcript, log.export())
            log.close()
            legacy = manager.save(KnowledgeBase(), [], [])
            self.assertIsNone(manager.restore(legacy)["execution_capture"])
            self.assertIsNone(manager.restore(legacy)["swarm"])

    def test_todo_hierarchy_is_preserved_without_generating_relations(self):
        master = master_fixture()
        todos = [{"id": "first", "content": "First", "status": "pending"},
                 {"id": "child", "content": "Child", "status": "pending", "parent_id": "first", "depends_on": ["first"]}]
        result = json.loads(master._tool_todo_write(todos))
        self.assertTrue(result["ok"])
        self.assertEqual(todos, master.todos)
        original = deepcopy(master.todos)
        invalid = [{"id": "bad", "content": "Bad", "depends_on": "first"}]
        self.assertIn("error", json.loads(master._tool_todo_write(invalid)))
        self.assertEqual(original, master.todos)


if __name__ == "__main__":
    unittest.main()
