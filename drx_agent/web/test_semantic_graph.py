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
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(len(nodes), len(graph["nodes"]))
        for node in nodes.values():
            self.assertTrue({"id", "label", "kind", "status", "actionId", "step", "source"} <= node.keys())
        for edge in graph["edges"]:
            self.assertIn(edge["source"], nodes)
            self.assertIn(edge["target"], nodes)
            self.assertNotEqual(edge["source"], edge["target"])
            self.assertTrue({"id", "source", "target", "relation", "label", "sourceInfo"} <= edge.keys())

    def test_no_records_produces_two_empty_graphs(self):
        result = build_semantic_graphs({}, [])
        self.assertEqual({"execution": {"nodes": [], "edges": []}, "causal": {"nodes": [], "edges": []}}, result)

    def test_nonempty_execution_has_one_real_session_root(self):
        raw = session([tool("one", actor="worker-a"), tool("two", actor="worker-b")])
        raw["name"] = "Recorded session name"
        graph = project(raw)[0]["execution"]
        child_ids = {edge["target"] for edge in graph["edges"]}
        roots = [node for node in graph["nodes"] if node["id"] not in child_ids]
        self.assertEqual(1, len(roots))
        self.assertEqual("session", roots[0]["kind"])
        self.assertEqual("Recorded session name", roots[0]["label"])
        self.assertIsNone(roots[0]["status"])
        self.assertEqual(2, len([edge for edge in graph["edges"] if edge["source"] == roots[0]["id"]]))

    def test_message_done_is_not_a_completed_task_in_request_or_turn(self):
        raw = session([{**message("u", "user", "Request"), "status": "done"},
                       {**message("a", "assistant", "Reply"), "status": "done"}])
        graph = project(raw)[0]["execution"]
        for node in graph["nodes"]:
            if node["kind"] in {"request", "turn"}:
                self.assertIsNone(node["status"])
                self.assertEqual("done", node["source"]["recordStatus"])

    def test_user_and_assistant_records_form_branches_not_sequence_chain(self):
        raw = session([message("u", "user", "Inspect this"), message("a", "assistant", "Plan"), tool("t1"), tool("t2")])
        result, actions = project(raw)
        graph = result["execution"]
        by_action = {node["actionId"]: node for node in graph["nodes"] if node["actionId"]}
        request, turn, first, second = [by_action[action["id"]] for action in actions]
        self.assertEqual("request", request["kind"])
        self.assertEqual("turn", turn["kind"])
        edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
        self.assertIn((request["id"], turn["id"]), edges)
        self.assertIn((turn["id"], first["id"]), edges)
        self.assertIn((turn["id"], second["id"]), edges)
        self.assertNotIn((first["id"], second["id"]), edges)
        self.assertTrue(all(edge["relation"] == "record_group" for edge in graph["edges"]))
        self.assertEqual([], result["causal"]["nodes"])
        self.assert_valid(graph)

    def test_legacy_messages_tool_call_ids_make_real_turn_branches(self):
        raw = session(messages=[
            {"role": "user", "content": "Inspect two resources"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "http_fetch", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "http_fetch", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "First"},
            {"role": "tool", "tool_call_id": "c2", "content": "Second"},
        ])
        result, actions = project(raw)
        graph = result["execution"]
        turns = [node for node in graph["nodes"] if node["source"].get("origin") == "assistant_tool_calls"]
        self.assertEqual(1, len(turns))
        children = [edge for edge in graph["edges"] if edge["source"] == turns[0]["id"]]
        self.assertEqual(2, len(children))
        self.assertEqual({"c1", "c2"}, {edge["sourceInfo"]["value"] for edge in children})
        self.assertEqual(len(actions), len([node for node in graph["nodes"] if node["actionId"]]))

    def test_modern_tool_calls_match_original_messages_by_id(self):
        raw = session([tool("event-a", call_id="call-a"), tool("event-b", call_id="call-b")], messages=[
            {"role": "user", "content": "Recorded request"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-a", "function": {"name": "http_fetch"}},
                {"id": "call-b", "function": {"name": "http_fetch"}},
            ]},
        ])
        graph = project(raw)[0]["execution"]
        turn = next(node for node in graph["nodes"] if node["source"].get("origin") == "assistant_tool_calls")
        self.assertEqual(2, len([edge for edge in graph["edges"] if edge["source"] == turn["id"]]))

    def test_empty_assistant_calls_are_named_and_located_by_matched_actions(self):
        raw = session([message("user", "user", "Request"), tool("one", call_id="c1"), tool("two", call_id="c2")], messages=[
            {"role": "user", "content": "Request"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "http_fetch", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "http_fetch", "arguments": "{}"}},
            ]},
        ])
        graph = project(raw)[0]["execution"]
        group = next(node for node in graph["nodes"] if node["source"].get("origin") == "assistant_tool_calls")
        self.assertEqual("tool_group", group["kind"])
        self.assertEqual("工具调用组 · 2项", group["label"])
        self.assertEqual(1, group["step"])
        self.assertIsNone(group["actionId"])
        self.assertEqual("matched_tool_calls", group["source"]["stepSource"]["kind"])
        self.assertNotIn("replayVisibility", group["source"])
        self.assertFalse(any("助手回合" in node["label"] for node in graph["nodes"]))

    def test_unmatched_tool_call_group_is_snapshot_only(self):
        raw = session([], messages=[{"role": "assistant", "content": "", "tool_calls": [
            {"id": "unmatched", "function": {"name": "http_fetch", "arguments": "{}"}}
        ]}])
        graph = project(raw)[0]["execution"]
        group = next(node for node in graph["nodes"] if node["source"].get("origin") == "assistant_tool_calls")
        self.assertIsNone(group["step"])
        self.assertEqual("snapshot_only", group["source"]["replayVisibility"])

    def test_null_and_whitespace_toolcall_content_are_groups_with_original_sources(self):
        for content in (None, "", " \n\t"):
            with self.subTest(content=content):
                saved_message = {"role": "assistant", "content": content, "tool_calls": [
                    {"id": "c1", "function": {"name": "http_fetch", "arguments": "{}"}}
                ]}
                raw = session([tool("one", call_id="c1")], messages=[saved_message])
                group = next(node for node in project(raw)[0]["execution"]["nodes"] if node["kind"] == "tool_group")
                self.assertEqual(saved_message, group["source"]["record"])
                self.assertEqual("工具调用组 · 1项", group["label"])

    def test_empty_auxiliary_messages_without_calls_do_not_create_nodes(self):
        for content in (None, "", " \n\t"):
            with self.subTest(content=content):
                raw = session([], messages=[{"role": "assistant", "content": content}])
                self.assertEqual([], project(raw)[0]["execution"]["nodes"])

    def test_unmatched_auxiliary_message_keeps_source_and_is_snapshot_only(self):
        saved_message = {"role": "assistant", "content": "Unmatched saved message"}
        raw = session([], messages=[saved_message])
        node = next(node for node in project(raw)[0]["execution"]["nodes"] if node["kind"] == "turn")
        self.assertEqual(saved_message, node["source"]["record"])
        self.assertIsNone(node["step"])
        self.assertEqual("snapshot_only", node["source"]["replayVisibility"])

    def test_matched_empty_assistant_action_keeps_tool_group_classification(self):
        raw = session([message("empty", "assistant", ""), tool("one", call_id="c1")], messages=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "http_fetch", "arguments": "{}"}}]}
        ])
        graph = project(raw)[0]["execution"]
        group = next(node for node in graph["nodes"] if node["source"].get("origin") == "assistant_tool_calls")
        self.assertEqual("tool_group", group["kind"])
        self.assertIsNotNone(group["actionId"])
        self.assertEqual(0, group["step"])

    def test_host_context_is_not_a_user_request_or_new_conversation_parent(self):
        context = "【宿主运行状态 — 完整快照，后出现的快照替代先前快照】\nSynthetic context"
        raw = session([message("user", "user", "Request"), tool("one", call_id="c1")], messages=[
            {"role": "user", "content": "Request"}, {"role": "user", "content": context},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "http_fetch", "arguments": "{}"}}]},
        ])
        graph = project(raw)[0]["execution"]
        self.assertEqual(1, sum(node["kind"] == "request" for node in graph["nodes"]))
        host = next(node for node in graph["nodes"] if node["kind"] == "context")
        request = next(node for node in graph["nodes"] if node["kind"] == "request")
        group = next(node for node in graph["nodes"] if node["source"].get("origin") == "assistant_tool_calls")
        self.assertEqual(context, host["source"]["record"]["content"])
        self.assertEqual("宿主状态快照", host["label"])
        self.assertEqual("snapshot_only", host["source"]["replayVisibility"])
        self.assertTrue(any(edge["source"] == request["id"] and edge["target"] == group["id"] for edge in graph["edges"]))
        self.assertFalse(any(edge["source"] == host["id"] for edge in graph["edges"]))

    def test_legacy_host_context_keeps_its_real_action_position(self):
        context = "【宿主运行状态 — 完整快照，后出现的快照替代先前快照】\nSynthetic context"
        raw = session(messages=[{"role": "user", "content": "Request"}, {"role": "user", "content": context},
                                {"role": "assistant", "content": "Reply"}])
        graph = project(raw)[0]["execution"]
        host = next(node for node in graph["nodes"] if node["kind"] == "context")
        self.assertEqual(1, host["step"])
        self.assertIsNotNone(host["actionId"])
        self.assertIsNone(host["status"])
        self.assertEqual(1, sum(node["kind"] == "request" for node in graph["nodes"]))

    def test_different_id_namespaces_use_only_unique_exact_payload_match(self):
        raw = session([tool("trace-one", call_id="internal-id", output="Actual result")], messages=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "provider-id", "function": {"name": "http_fetch", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "provider-id", "content": "Actual result"},
        ])
        graph = project(raw)[0]["execution"]
        matched = [edge for edge in graph["edges"] if edge["sourceInfo"].get("matching")]
        self.assertEqual(1, len(matched))
        self.assertEqual("tool_input_output_exact", matched[0]["sourceInfo"]["matching"])
        self.assertEqual("record_group", matched[0]["relation"])

    def test_ambiguous_payloads_are_not_assigned_to_a_message_call(self):
        raw = session([tool("one", output="same"), tool("two", output="same")], messages=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "provider-id", "function": {"name": "http_fetch", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "provider-id", "content": "same"},
        ])
        graph = project(raw)[0]["execution"]
        self.assertFalse(any(edge["sourceInfo"].get("matching") for edge in graph["edges"]))
        self.assertEqual(2, len([node for node in graph["nodes"] if node["actionId"]]))

    def test_explicit_task_parent_and_action_membership_override_groups(self):
        raw = session([tool("event", data={"task_id": "child"})], extra={"todos": [
            {"id": "root", "content": "Actual root", "status": "in_progress"},
            {"id": "child", "parent_id": "root", "content": "Actual child", "status": "pending"},
        ]})
        graph = project(raw)[0]["execution"]
        by_label = {node["label"]: node for node in graph["nodes"]}
        action = next(node for node in graph["nodes"] if node["actionId"])
        edges = {(edge["source"], edge["target"]): edge for edge in graph["edges"]}
        self.assertEqual("task_parent", edges[(by_label["Actual root"]["id"], by_label["Actual child"]["id"])]["relation"])
        self.assertEqual("task_id", edges[(by_label["Actual child"]["id"], action["id"])]["sourceInfo"]["field"])

    def test_explicit_plan_dependencies_keep_multiple_predecessors(self):
        raw = session([], extra={"todos": [
            {"id": "a", "content": "First prerequisite"},
            {"id": "b", "content": "Second prerequisite"},
            {"id": "c", "content": "Dependent task", "depends_on": ["a", "b"]},
        ]})
        graph = project(raw)[0]["execution"]
        dependencies = [edge for edge in graph["edges"] if edge["relation"] == "dependency"]
        self.assertEqual(2, len(dependencies))
        self.assertEqual(1, len({edge["target"] for edge in dependencies}))
        self.assertEqual({"a", "b"}, {edge["sourceInfo"]["value"] for edge in dependencies})
        self.assertTrue(all(edge["sourceInfo"]["field"] == "depends_on" for edge in dependencies))
        incoming = {edge["target"] for edge in graph["edges"]}
        self.assertEqual(1, sum(node["id"] not in incoming for node in graph["nodes"]))

    def test_dependency_cycle_is_reported_without_invented_edge(self):
        raw = session([], extra={"todos": [
            {"id": "a", "content": "A", "dependencies": ["b"]},
            {"id": "b", "content": "B", "dependencies": ["a"]},
        ]})
        graph = project(raw)[0]["execution"]
        self.assertEqual(1, sum(edge["relation"] == "dependency" for edge in graph["edges"]))
        self.assertTrue(any(relation.get("reason") == "cyclic_dependency" for node in graph["nodes"] for relation in node["source"].get("unresolvedRelations", [])))

    def test_missing_or_non_task_dependency_does_not_create_execution_relation(self):
        raw = session([tool("source-record")], extra={"todos": [
            {"id": "a", "content": "A", "depends_on": ["source-record", "missing"]},
        ]})
        graph = project(raw)[0]["execution"]
        self.assertFalse(any(edge["relation"] == "dependency" for edge in graph["edges"]))

    def test_dispatch_output_and_worker_identity_make_a_subtask_branch(self):
        raw = session([
            message("u", "user", "Work"), message("a", "assistant", "Delegate"),
            tool("dispatch", name="task", output={"agent_id": "worker-a", "status": "error"}),
            message("worker-message", "assistant", "Worker text", actor="worker-a"),
            tool("worker-tool", actor="worker-a"),
        ], extra={"team": {"members": [{"agent_id": "worker-a", "task": "Actual child task", "status": "error"}]}})
        result, actions = project(raw)
        graph = result["execution"]
        worker = next(node for node in graph["nodes"] if node["kind"] == "worker_task")
        dispatch = next(node for node in graph["nodes"] if node["actionId"] == actions[2]["id"])
        self.assertTrue(any(edge["source"] == dispatch["id"] and edge["target"] == worker["id"] and edge["relation"] == "dispatch" for edge in graph["edges"]))
        self.assertEqual(2, len([edge for edge in graph["edges"] if edge["source"] == worker["id"]]))
        self.assertEqual("error", worker["status"])

    def test_all_long_unassigned_records_survive_with_no_success_claim(self):
        raw = session([tool(f"event-{index}") for index in range(1200)])
        result, actions = project(raw)
        graph = result["execution"]
        leaves = [node for node in graph["nodes"] if node["actionId"]]
        self.assertEqual(1200, len(leaves))
        self.assertEqual({action["id"] for action in actions}, {node["actionId"] for node in leaves})
        self.assertTrue(all(node["status"] is None for node in graph["nodes"]))
        self.assertTrue(any("未关联任务" in node["label"] for node in graph["nodes"]))
        self.assertFalse(any(edge["relation"] == "sequence" for edge in graph["edges"]))

    def test_missing_parent_is_explained_without_creating_task(self):
        raw = session([tool("event", data={"task_id": "missing-task"})])
        graph = project(raw)[0]["execution"]
        action = next(node for node in graph["nodes"] if node["actionId"])
        self.assertEqual("missing-task", action["source"]["unresolvedRelations"][0]["value"])
        self.assertFalse(any(node["kind"] == "task" for node in graph["nodes"]))

    def test_cyclic_task_references_do_not_create_execution_cycle(self):
        raw = session([], extra={"todos": [{"id": "a", "content": "A", "parent_id": "b"}, {"id": "b", "content": "B", "parent_id": "a"}]})
        graph = project(raw)[0]["execution"]
        parents = {edge["target"]: edge["source"] for edge in graph["edges"]}
        for node in graph["nodes"]:
            seen = set()
            cursor = node["id"]
            while cursor in parents:
                self.assertNotIn(cursor, seen)
                seen.add(cursor)
                cursor = parents[cursor]
        self.assertTrue(any(node["source"].get("unresolvedRelations") for node in graph["nodes"]))

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

    def test_frontier_dependency_is_causal_reference_not_task_parent(self):
        raw = session([], extra={"frontier": {"intents": [{"id": "it-1", "hypothesis": "Actual hypothesis", "status": "open", "depends_on": ["host::observed-fact"]}]}})
        result = project(raw)[0]
        self.assertFalse(any(edge["relation"] == "task_parent" for edge in result["execution"]["edges"]))
        causal = result["causal"]
        self.assertEqual(2, len(causal["nodes"]))
        self.assertEqual("depends_on", causal["edges"][0]["relation"])
        self.assertEqual("host::observed-fact", causal["edges"][0]["sourceInfo"]["value"])
        self.assertTrue(any(node["source"].get("unresolved") for node in causal["nodes"]))

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

    def test_projection_does_not_mutate_records_or_truncate_source(self):
        raw = session([message("u", "user", "x" * 10000), tool("event")])
        actions = session_to_graph(raw)["actions"]
        before_raw, before_actions = deepcopy(raw), deepcopy(actions)
        result = build_semantic_graphs(raw, actions)
        self.assertEqual(before_raw, raw)
        self.assertEqual(before_actions, actions)
        user = next(node for node in result["execution"]["nodes"] if node["kind"] == "request")
        self.assertEqual(10000, len(user["source"]["record"]["text"]))
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
