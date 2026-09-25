"""Evidence-based execution trees and causal relation regression checks."""

from copy import deepcopy
import json
import unittest

from drx_agent.web.graph import session_to_graph
from drx_agent.web.semantic_graph import build_semantic_graphs


def session(records=None, *, messages=None, extra=None, kb=None):
    details = {"transcript": records} if records is not None else {}
    details.update(extra or {})
    return {"id": "synthetic", "metadata": {"extra": details}, "messages": messages or [], "kb_data": kb or {}}


def message(identity, role, text, actor="master"):
    return {"id": identity, "kind": "message", "role": role, "text": text, "actor": actor}


def tool(identity, *, call_id=None, actor="master", name="http_fetch", data=None, output=None):
    detail = data or {}
    if call_id:
        detail = {**detail, "tool_call_id": call_id}
    return {"id": identity, "kind": "tool", "tool": name, "actor": actor, "input": {}, "output": output, "data": detail}


def project(raw):
    actions = session_to_graph(raw)["actions"]
    return build_semantic_graphs(raw, actions), actions


class SemanticGraphTests(unittest.TestCase):
    def assert_valid(self, graph):
        nodes = {node["id"] for node in graph["nodes"]}
        self.assertEqual(len(nodes), len(graph["nodes"]))
        for edge in graph["edges"]:
            self.assertIn(edge["source"], nodes)
            self.assertIn(edge["target"], nodes)
            self.assertNotEqual(edge["source"], edge["target"])
            self.assertTrue(edge["sourceInfo"])

    def execution(self, raw):
        graph, actions = project(raw)
        self.assert_valid(graph["execution"])
        return graph["execution"], actions

    def test_single_agent_graph_contains_only_main_task_plans_and_tools(self):
        context = "【宿主运行状态 — 完整快照，后出现的快照替代先前快照】\nRecorded state"
        raw = session([message("u", "user", "Actual request"), message("a", "assistant", "Plan"),
                       message("ctx", "user", context), tool("t"), message("reply", "assistant", "Result")],
                      messages=[{"role": "user", "content": context}], extra={"todos": [{"id": "a", "content": "Task A"}, {"id": "b", "content": "Task B"}]})
        graph, actions = self.execution(raw)
        self.assertEqual(5, len(actions))
        self.assertEqual({"root", "task", "tool"}, {node["kind"] for node in graph["nodes"]})
        root = next(node for node in graph["nodes"] if node["kind"] == "root")
        self.assertEqual("Actual request", root["label"])
        self.assertEqual(raw["messages"], root["source"]["record"]["messages"])
        self.assertEqual([action["id"] for action in actions], root["source"]["logActionIds"])

    def test_assistant_context_and_system_logs_do_not_invent_a_main_task(self):
        context = "【宿主运行状态 — 完整快照，后出现的快照替代先前快照】\nRecorded state"
        raw = session([message("assistant", "assistant", "Saved reply"), message("context", "user", context),
                       message("system", "system", "System log")])
        raw["name"] = "Saved session name"
        graph, actions = self.execution(raw)
        self.assertEqual(3, len(actions))
        self.assertEqual({"nodes": [], "edges": []}, graph)

    def test_context_only_legacy_history_remains_logs_without_execution_nodes(self):
        context = "【宿主运行状态 — 完整快照，后出现的快照替代先前快照】\nRecorded state"
        graph, actions = self.execution(session(messages=[{"role": "user", "content": context}]))
        self.assertEqual(1, len(actions))
        self.assertEqual(context, actions[0]["text"])
        self.assertEqual([], graph["nodes"])

    def test_real_user_request_without_tools_has_one_unknown_status_main_task(self):
        graph, actions = self.execution(session([message("request", "user", "Real request")]))
        self.assertEqual(1, len(actions))
        self.assertEqual(1, len(graph["nodes"]))
        self.assertEqual("root", graph["nodes"][0]["kind"])
        self.assertEqual("Real request", graph["nodes"][0]["label"])
        self.assertIsNone(graph["nodes"][0]["status"])
        self.assertEqual([], graph["edges"])

    def test_saved_plan_list_is_a_labeled_plan_order_backbone(self):
        graph, _ = self.execution(session([], extra={"todos": [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}, {"id": "c", "content": "C"}]}))
        self.assertEqual(2, sum(edge["relation"] == "plan_order" for edge in graph["edges"]))
        self.assertFalse(any(edge["relation"] == "dependency" for edge in graph["edges"]))

    def test_explicit_plan_dependency_retains_multiple_predecessors(self):
        graph, _ = self.execution(session([], extra={"todos": [{"id": "a", "content": "A"}, {"id": "b", "content": "B"},
                                                              {"id": "c", "content": "C", "depends_on": ["a", "b"]}]}))
        edges = [edge for edge in graph["edges"] if edge["relation"] == "dependency"]
        self.assertEqual(2, len(edges))
        self.assertEqual(1, len({edge["target"] for edge in edges}))
        self.assertFalse(any(edge["relation"] == "plan_order" for edge in graph["edges"]))

    def test_duplicate_plan_ids_remain_distinct_and_do_not_bind_tools(self):
        ctx = {"version": 1, "actor": "master", "task_id": "same"}
        graph, _ = self.execution(session([tool("one", data={"execution_context": ctx})], extra={"todos": [
            {"id": "same", "content": "First"}, {"id": "same", "content": "Second"},
        ]}))
        tasks = [node for node in graph["nodes"] if node["kind"] == "task"]
        self.assertEqual(2, len(tasks))
        self.assertEqual(2, len({node["id"] for node in tasks}))
        self.assertEqual({"First", "Second"}, {node["label"] for node in tasks})
        self.assertEqual("unrecorded", next(node for node in graph["nodes"] if node["kind"] == "tool")["source"]["taskAssociation"])

    def test_legacy_parent_id_can_reference_a_saved_task(self):
        graph, _ = self.execution(session([tool("one", data={"parent_id": "a"})], extra={"todos": [{"id": "a", "content": "A"}]}))
        task = next(node for node in graph["nodes"] if node["kind"] == "task")
        child = next(node for node in graph["nodes"] if node["kind"] == "tool")
        self.assertTrue(any(edge["source"] == task["id"] and edge["target"] == child["id"] and edge["sourceInfo"]["field"] == "parent_id" for edge in graph["edges"]))

    def test_unassociated_different_actors_are_not_linked_as_serial(self):
        graph, _ = self.execution(session([tool("one", actor="a"), tool("two", actor="b"), tool("three", actor="a")]))
        nodes = {node["id"]: node for node in graph["nodes"]}
        order = [edge for edge in graph["edges"] if edge["relation"] == "record_order"]
        self.assertEqual(1, len(order))
        self.assertEqual("a", nodes[order[0]["source"]]["source"]["actor"])
        self.assertEqual("a", nodes[order[0]["target"]]["source"]["actor"])
        self.assertFalse(any(node["kind"] == "worker_task" for node in graph["nodes"]))

    def test_explicit_previous_turn_overrides_nearest_saved_round(self):
        base = {"version": 1, "actor": "master", "run_id": "run", "request_id": "request"}
        graph, actions = self.execution(session([
            tool("one", data={"execution_context": {**base, "turn_id": "first"}}),
            tool("two", data={"execution_context": {**base, "turn_id": "middle"}}),
            tool("three", data={"execution_context": {**base, "turn_id": "last", "previous_turn_id": "first"}}),
        ]))
        by_action = {node["actionId"]: node["id"] for node in graph["nodes"] if node["kind"] == "tool"}
        edge = next(edge for edge in graph["edges"] if edge["target"] == by_action[actions[2]["id"]])
        self.assertEqual(by_action[actions[0]["id"]], edge["source"])
        self.assertEqual("previous_turn_id", edge["sourceInfo"]["field"])
        self.assertEqual("first", edge["sourceInfo"]["value"])

    def test_captured_same_round_tools_share_a_parent_without_sibling_edges(self):
        ctx = {"version": 1, "actor": "master", "turn_id": "round-1", "task_id": "a"}
        graph, _ = self.execution(session([tool("one", data={"execution_context": ctx}), tool("two", data={"execution_context": ctx})],
                                         extra={"todos": [{"id": "a", "content": "A"}]}))
        tools = [node for node in graph["nodes"] if node["kind"] == "tool"]
        incoming = [{edge["source"] for edge in graph["edges"] if edge["target"] == node["id"]} for node in tools]
        self.assertEqual(incoming[0], incoming[1])
        self.assertEqual(1, len(incoming[0]))
        self.assertFalse(any(edge["source"] in {node["id"] for node in tools} for edge in graph["edges"]))

    def test_captured_active_plan_is_distinct_from_explicit_task_binding(self):
        active = {"id": "a", "content": "A", "status": "in_progress"}
        ctx = {"version": 1, "actor": "master", "turn_id": "r", "active_task": active, "active_tasks": [active]}
        graph, _ = self.execution(session([tool("one", data={"execution_context": ctx})], extra={"todos": [active]}))
        node = next(node for node in graph["nodes"] if node["kind"] == "tool")
        self.assertEqual("active_plan", node["source"]["association"]["relation"])
        self.assertTrue(any(edge["relation"] == "active_plan" for edge in graph["edges"]))

    def test_ambiguous_active_plans_are_not_guessed(self):
        active = [{"id": "a", "content": "A", "status": "in_progress"}, {"id": "b", "content": "B", "status": "in_progress"}]
        ctx = {"version": 1, "actor": "master", "turn_id": "r", "active_task": active[0], "active_tasks": active}
        graph, _ = self.execution(session([tool("one", data={"execution_context": ctx})], extra={"todos": active}))
        node = next(node for node in graph["nodes"] if node["kind"] == "tool")
        self.assertEqual("unrecorded", node["source"]["taskAssociation"])

    def test_worker_tools_do_not_borrow_the_master_active_plan(self):
        active = {"id": "a", "content": "A", "status": "in_progress"}
        ctx = {"version": 1, "actor": "worker-a", "active_task": active, "active_tasks": [active]}
        graph, _ = self.execution(session([tool("one", actor="worker-a", data={"execution_context": ctx})], extra={"todos": [active]}))
        node = next(node for node in graph["nodes"] if node["kind"] == "tool")
        self.assertEqual("unrecorded", node["source"]["taskAssociation"])
        self.assertEqual("unrecorded", node["source"]["workerAssociation"])
        self.assertFalse(any(node["kind"] == "worker_task" for node in graph["nodes"]))

    def test_unused_frontier_hypotheses_stay_out_of_execution(self):
        raw = session([tool("one")], extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Unexecuted hypothesis", "status": "open"}]}})
        graphs, _ = project(raw)
        self.assertFalse(any(node["kind"] == "task" for node in graphs["execution"]["nodes"]))
        self.assertTrue(any(node["kind"] == "hypothesis" for node in graphs["causal"]["nodes"]))

    def test_explicit_frontier_binding_creates_a_real_task(self):
        ctx = {"version": 1, "actor": "master", "task_id": "it-1"}
        graph, _ = self.execution(session([tool("one", data={"execution_context": ctx})],
                                         extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Bound intent", "status": "claimed"}]}}))
        task = next(node for node in graph["nodes"] if node["kind"] == "task")
        self.assertEqual("Bound intent", task["label"])
        self.assertEqual("claimed", task["status"])

    def test_members_roles_and_swarm_config_never_create_worker_branches(self):
        raw = session([tool("one")], extra={"swarm": {"enabled": True, "max_concurrent": 8},
                                           "team": {"members": [{"agent_id": "executor", "role": "executor", "task": "Saved member"}],
                                                    "residents": {"planner": {"task": "Saved role"}}}})
        graph, _ = self.execution(raw)
        self.assertEqual({"root", "tool"}, {node["kind"] for node in graph["nodes"]})

    def test_real_dispatch_worker_branch_owns_worker_tools(self):
        parent = {"version": 1, "actor": "worker-a", "dispatch_id": "d-1", "parent_invocation_id": "inv-parent"}
        records = [tool("dispatch", name="task", data={"invocation_id": "inv-parent"}),
                   {"id": "worker", "kind": "worker", "actor": "worker-a", "status": "running",
                    "data": {"agent_id": "worker-a", "task": "Actual worker task", "execution_context": parent}},
                   tool("child", actor="worker-a", data={"execution_context": {**parent, "turn_id": "wr-1"}})]
        graph, _ = self.execution(session(records))
        worker = next(node for node in graph["nodes"] if node["kind"] == "worker_task")
        child = next(node for node in graph["nodes"] if node["kind"] == "tool" and node["source"]["actor"] == "worker-a")
        self.assertTrue(any(edge["target"] == worker["id"] and edge["relation"] == "dispatch" for edge in graph["edges"]))
        self.assertTrue(any(edge["source"] == worker["id"] and edge["target"] == child["id"] for edge in graph["edges"]))

    def test_unresolved_explicit_dispatch_does_not_fall_back_to_older_actor_or_task(self):
        old_context = {"version": 1, "actor": "worker-a", "dispatch_id": "old"}
        records = [
            {"id": "old-worker", "kind": "worker", "actor": "worker-a", "data": {"agent_id": "worker-a", "execution_context": old_context}},
            tool("old-tool", actor="worker-a", data={"invocation_id": "old-invocation", "execution_context": old_context}),
            tool("new-tool", actor="worker-a", data={"execution_context": {
                "version": 1, "actor": "worker-a", "dispatch_id": "missing-new", "task_id": "old-plan",
                "parent_invocation_id": "old-invocation",
            }}),
        ]
        graph, actions = self.execution(session(records, extra={"todos": [{"id": "old-plan", "content": "Old plan"}]}))
        root = next(node for node in graph["nodes"] if node["kind"] == "root")
        new_tool = next(node for node in graph["nodes"] if node["actionId"] == actions[2]["id"])
        self.assertEqual({root["id"]}, {edge["source"] for edge in graph["edges"] if edge["target"] == new_tool["id"]})
        self.assertEqual("unassigned", new_tool["source"]["taskAssociation"])
        self.assertEqual("unresolved_dispatch", new_tool["source"]["workerAssociation"])
        self.assertEqual("missing-new", new_tool["source"]["unresolvedRelations"][0]["value"])

    def test_rejected_dispatch_does_not_invent_a_worker(self):
        graph, _ = self.execution(session([tool("dispatch", name="task", output={"agent_id": "worker-a", "status": "cancelled", "error": "not started"})]))
        self.assertFalse(any(node["kind"] == "worker_task" for node in graph["nodes"]))

    def test_statuses_do_not_propagate_to_main_task_or_other_nodes(self):
        records = [{**message("u", "user", "Request"), "status": "done"}, {**tool("one"), "status": "error"}]
        graph, _ = self.execution(session(records, extra={"todos": [{"id": "a", "content": "A", "status": "completed"}]}))
        self.assertIsNone(next(node for node in graph["nodes"] if node["kind"] == "root")["status"])
        self.assertEqual("error", next(node for node in graph["nodes"] if node["kind"] == "tool")["status"])
        self.assertEqual("completed", next(node for node in graph["nodes"] if node["kind"] == "task")["status"])

    def test_legacy_same_message_tools_are_parallel_without_message_nodes(self):
        raw = session(messages=[{"role": "user", "content": "Request"},
                                {"role": "assistant", "content": "", "tool_calls": [
                                    {"id": "a", "function": {"name": "first", "arguments": "{}"}},
                                    {"id": "b", "function": {"name": "second", "arguments": "{}"}}]},
                                {"role": "tool", "tool_call_id": "a", "content": "A"},
                                {"role": "tool", "tool_call_id": "b", "content": "B"}])
        graph, actions = self.execution(raw)
        self.assertEqual(3, len(actions))
        self.assertEqual({"root", "tool"}, {node["kind"] for node in graph["nodes"]})
        self.assertEqual(1, len({edge["source"] for edge in graph["edges"]}))
        self.assertTrue(all(node["source"]["executionRound"]["matching"] == "tool_call_id" for node in graph["nodes"] if node["kind"] == "tool"))

    def test_successful_todo_update_applies_only_to_following_completed_round(self):
        update = tool("update", call_id="u", name="todo_write", output={"ok": True})
        update["input"] = {"todos": [{"id": "a", "content": "A", "status": "in_progress"}]}
        raw = session([update, tool("sibling", call_id="s", output="S"), tool("later", call_id="l", output="L")],
                      extra={"todos": [{"id": "a", "content": "A", "status": "completed"}]}, messages=[
                          {"role": "assistant", "content": "", "tool_calls": [
                              {"id": "u", "function": {"name": "todo_write", "arguments": json.dumps(update["input"])}},
                              {"id": "s", "function": {"name": "http_fetch", "arguments": "{}"}}]},
                          {"role": "tool", "tool_call_id": "u", "content": '{"ok":true}'},
                          {"role": "tool", "tool_call_id": "s", "content": "S"},
                          {"role": "assistant", "content": "", "tool_calls": [{"id": "l", "function": {"name": "http_fetch", "arguments": "{}"}}]},
                          {"role": "tool", "tool_call_id": "l", "content": "L"},
                      ])
        graph, actions = self.execution(raw)
        nodes = {node["actionId"]: node for node in graph["nodes"] if node["kind"] == "tool"}
        self.assertEqual("unrecorded", nodes[actions[1]["id"]]["source"]["taskAssociation"])
        self.assertEqual("active_plan", nodes[actions[2]["id"]]["source"]["association"]["relation"])
        self.assertEqual("completed_previous_todo_write", nodes[actions[2]["id"]]["source"]["association"]["field"])

    def test_saved_active_plan_does_not_imply_historical_tool_membership(self):
        graph, _ = self.execution(session([tool("one")], extra={"todos": [{"id": "a", "content": "http_fetch target", "status": "in_progress"}]}))
        node = next(node for node in graph["nodes"] if node["kind"] == "tool")
        self.assertEqual("unrecorded", node["source"]["taskAssociation"])

    def test_all_long_tool_records_survive_and_original_logs_are_unchanged(self):
        raw = session([message("u", "user", "Request"), *[tool(f"t-{index}") for index in range(1200)], message("end", "assistant", "Finished")])
        before = deepcopy(raw)
        graph, actions = self.execution(raw)
        self.assertEqual(1202, len(actions))
        self.assertEqual(1200, sum(node["kind"] == "tool" for node in graph["nodes"]))
        self.assertEqual(before, raw)
        self.assertFalse(any(node["kind"] in {"turn", "context", "group", "request", "session"} for node in graph["nodes"]))

    def test_no_records_produces_two_empty_graphs(self):
        result = build_semantic_graphs({}, [])
        self.assertEqual({"execution": {"nodes": [], "edges": []}, "causal": {"nodes": [], "edges": []}}, result)

    def test_text_that_mentions_exploit_or_proof_never_creates_causality(self):
        raw = session([message("a", "assistant", "confirmed exploited evidence SQL injection")])
        self.assertEqual({"nodes": [], "edges": []}, project(raw)[0]["causal"])

    def test_finding_without_evidence_does_not_invent_evidence(self):
        raw = session([], kb={"findings": {"example.test": [{"claim": "Recorded suspicion", "status": "suspected", "evidence": []}]}})
        graph = project(raw)[0]["causal"]
        self.assertEqual(1, len(graph["nodes"]))
        self.assertEqual("finding", graph["nodes"][0]["kind"])
        self.assertEqual("suspected", graph["nodes"][0]["status"])
        self.assertEqual([], graph["edges"])

    def test_evidence_links_are_explicit_and_shared_without_default_status(self):
        evidence = {"evidence_id": "E-1", "type": "http", "value": "Recorded response"}
        raw = session([], kb={"findings": {"example.test": [{"claim": "First", "evidence": [evidence]}, {"claim": "Second", "evidence": ["E-1"]}]}})
        graph = project(raw)[0]["causal"]
        self.assertEqual(3, len(graph["nodes"]))
        self.assertEqual(2, len(graph["edges"]))
        self.assertTrue(all(edge["relation"] == "evidence_for" for edge in graph["edges"]))
        self.assertTrue(all(node["status"] is None for node in graph["nodes"]))
        self.assert_valid(graph)

    def test_later_saved_evidence_resolves_an_earlier_reference(self):
        raw = session([], kb={"findings": {"host": [{"claim": "First", "evidence": ["E-1"]}, {"claim": "Second", "evidence": [{"evidence_id": "E-1", "value": "Actual evidence"}]}]}})
        graph = project(raw)[0]["causal"]
        evidence = next(node for node in graph["nodes"] if node["kind"] == "evidence")
        self.assertEqual("Actual evidence", evidence["label"])
        self.assertFalse(evidence["source"]["unresolved"])

    def test_missing_evidence_is_labeled_unresolved_not_verified(self):
        raw = session([], kb={"findings": {"host": [{"claim": "Suspicion", "evidence": ["E-missing"]}]}})
        graph = project(raw)[0]["causal"]
        evidence = next(node for node in graph["nodes"] if node["kind"] == "evidence_reference")
        self.assertTrue(evidence["source"]["unresolved"])
        self.assertIsNone(evidence["status"])

    def test_saved_blackboard_fact_resolves_explicit_dependency(self):
        raw = session([], extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Hypothesis", "depends_on": ["bb-1"]}]}},
                      kb={"blackboard": {"entries": {"findings": [{"id": "bb-1", "text": "Observed fact"}]}}})
        graph = project(raw)[0]["causal"]
        fact = next(node for node in graph["nodes"] if node["kind"] == "fact")
        self.assertEqual("Observed fact", fact["label"])
        self.assertIsNone(fact["status"])

    def test_host_claim_dependency_reuses_the_existing_finding(self):
        raw = session([], extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Follow-up", "depends_on": ["example.test::测试发现"]}]}},
                      kb={"findings": {"example.test": [{"host": "example.test", "claim": "测试发现", "status": "suspected"}]}})
        graph = project(raw)[0]["causal"]
        self.assertEqual(2, len(graph["nodes"]))
        finding = next(node for node in graph["nodes"] if node["kind"] == "finding")
        self.assertEqual(1, len(graph["edges"]))
        self.assertEqual(finding["id"], graph["edges"][0]["source"])
        self.assertEqual("depends_on", graph["edges"][0]["relation"])
        self.assertFalse(any(node["source"].get("unresolved") for node in graph["nodes"]))

    def test_duplicate_host_claim_remains_an_ambiguous_reference(self):
        raw = session([], extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Follow-up", "depends_on": ["example.test::Same"]}]}},
                      kb={"findings": {"example.test": [{"claim": "Same", "status": "suspected"}, {"claim": "Same", "status": "confirmed"}]}})
        graph = project(raw)[0]["causal"]
        self.assertEqual(2, sum(node["kind"] == "finding" for node in graph["nodes"]))
        reference = next(node for node in graph["nodes"] if node["source"].get("unresolved"))
        self.assertEqual("ambiguous_finding_reference", reference["source"]["reason"])
        self.assertEqual(reference["id"], graph["edges"][0]["source"])

    def test_host_claim_collision_with_another_entity_does_not_pick_a_target(self):
        raw = session([], extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Follow-up", "depends_on": ["example.test::Same"]}]}},
                      kb={"findings": {"example.test": [{"claim": "Same"}]},
                          "blackboard": {"entries": {"findings": [{"id": "example.test::Same", "text": "Different entity"}]}}})
        graph = project(raw)[0]["causal"]
        reference = next(node for node in graph["nodes"] if node["source"].get("unresolved"))
        self.assertEqual("reference", reference["kind"])
        self.assertEqual(reference["id"], graph["edges"][0]["source"])
        self.assertEqual(1, sum(node["kind"] == "finding" for node in graph["nodes"]))
        self.assertEqual(1, sum(node["kind"] == "fact" for node in graph["nodes"]))

    def test_conflicting_bucket_host_and_record_host_remain_unresolved(self):
        raw = session([], extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Follow-up", "depends_on": ["example.test::Same"]}]}},
                      kb={"findings": {"example.test": [{"host": "different.test", "claim": "Same"}]}})
        graph = project(raw)[0]["causal"]
        reference = next(node for node in graph["nodes"] if node["source"].get("unresolved"))
        self.assertEqual("ambiguous_finding_reference", reference["source"]["reason"])
        self.assertEqual(reference["id"], graph["edges"][0]["source"])

    def test_unlinked_hypotheses_and_blackboard_facts_are_retained(self):
        raw = session([], extra={"frontier": {"intents": [{"id": "it-alone", "hypothesis": "Intent hypothesis", "status": "done"}]}},
                      kb={"blackboard": {"entries": {
                          "hypotheses": [{"id": "bb-h", "text": "Unverified hypothesis"}],
                          "findings": [{"id": "bb-f", "text": "Recorded information"}],
                      }}})
        graph = project(raw)[0]["causal"]
        self.assertEqual(3, len(graph["nodes"]))
        self.assertEqual([], graph["edges"])
        self.assertEqual({"Intent hypothesis", "Unverified hypothesis", "Recorded information"}, {node["label"] for node in graph["nodes"]})
        self.assertTrue(all(node["status"] is None for node in graph["nodes"]))
        self.assertFalse(any(node["kind"] == "finding" for node in graph["nodes"]))

    def test_evidence_reference_keeps_recorded_entity_fields(self):
        evidence = {"evidence_id": "E-entity", "result": "Recorded result", "source": "Recorded source"}
        raw = session([], kb={"findings": {"host": [{"claim": "Finding", "evidence": [evidence]}]}})
        graph = project(raw)[0]["causal"]
        node = next(node for node in graph["nodes"] if node["kind"] == "evidence")
        self.assertEqual(evidence, node["source"]["record"])
        self.assertFalse(node["source"]["unresolved"])
