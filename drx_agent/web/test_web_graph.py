"""Regression checks for evidence-preserving dashboard projections (stdlib only)."""

from copy import deepcopy
import json
import unittest


from drx_agent.web.graph import session_to_graph, SessionFormatError

def snapshot(records=None, *, messages=None, kb=None, active_targets=None, extra=None):
    metadata_extra = {} if records is None else {"transcript": records}
    metadata_extra.update(extra or {})
    return {
        "id": "synthetic-session", "name": "Synthetic session", "created_at": 1000,
        "phase": "synthesis", "metadata": {"active_targets": active_targets or [], "extra": metadata_extra},
        "messages": messages or [], "kb_data": kb or {},
    }


def tool(index, **fields):
    return {"id": f"call-{index}", "kind": "tool", "actor": "master", "tool": "http_fetch",
            "input": {"url": f"https://example.test/{index}"}, "output": f"result {index}", **fields}


class GraphTests(unittest.TestCase):
    def test_empty_session_has_no_fabricated_target_or_events(self):
        graph = session_to_graph(snapshot())
        self.assertEqual([], graph["actions"])
        self.assertEqual([], graph["nodes"])
        self.assertEqual([], graph["edges"])
        self.assertEqual([], graph["stages"])
        for field in ("targetHost", "targetUrl", "targetNotes"):
            self.assertIsNone(graph["summary"][field])
        self.assertEqual(0, graph["summary"]["totalActions"])

    def test_explicit_empty_transcript_is_authoritative(self):
        graph = session_to_graph(snapshot([], messages=[{"role": "assistant", "content": "Old message"}]))
        self.assertEqual([], graph["actions"])

    def test_active_target_url_is_preserved_without_added_port(self):
        summary = session_to_graph(snapshot(active_targets=["https://example.test/path"]))["summary"]
        self.assertEqual("https://example.test/path", summary["targetUrl"])
        self.assertEqual("example.test", summary["targetHost"])
        self.assertIsNone(summary["targetNotes"])
        self.assertEqual(1, summary["targetsCount"])

    def test_port_and_service_do_not_invent_url(self):
        summary = session_to_graph(snapshot(kb={"targets": {"example.test": {"open_ports": [443], "services": {"443": "HTTPS"}}}}))["summary"]
        self.assertEqual("example.test", summary["targetHost"])
        self.assertIsNone(summary["targetUrl"])
        self.assertIn("443", summary["targetNotes"])
        self.assertNotIn("3000", summary["targetNotes"])

    def test_missing_service_and_port_stay_missing(self):
        summary = session_to_graph(snapshot(kb={"targets": {"example.test": {"host": "example.test", "open_ports": [], "services": {}}}}))["summary"]
        self.assertIsNone(summary["targetUrl"])
        self.assertIsNone(summary["targetNotes"])

    def test_explicit_url_is_preserved_exactly(self):
        summary = session_to_graph(snapshot(kb={"targets": {"example.test": {"url": "https://example.test:8443/base", "notes": "Recorded note"}}}))["summary"]
        self.assertEqual("https://example.test:8443/base", summary["targetUrl"])
        self.assertEqual("Recorded note", summary["targetNotes"])

    def test_findings_and_credentials_flatten_all_host_buckets(self):
        findings = [{"claim": f"Claim {i}"} for i in range(24)]
        creds = [{"id": f"credential-{i}"} for i in range(23)]
        graph = session_to_graph(snapshot(kb={"findings": {"first.test": findings, "second.test": [{"claim": "Other"}]}, "credentials": {"first.test": creds}}))
        summary = graph["summary"]
        self.assertEqual(25, summary["findingsCount"])
        self.assertEqual(25, len(summary["findings"]))
        self.assertEqual(23, summary["credsCount"])
        self.assertEqual(23, len(summary["creds"]))
        self.assertEqual("first.test", summary["findings"][0]["host"])
        self.assertEqual("second.test", summary["findings"][-1]["host"])
        self.assertEqual("first.test", summary["creds"][0]["host"])

    def test_legacy_flat_kb_collections_are_supported(self):
        summary = session_to_graph(snapshot(kb={"targets": ["example.test"], "findings": [{"claim": "Claim"}], "credentials": [{"id": "cred"}]}))["summary"]
        self.assertEqual((1, 1, 1), (summary["targetsCount"], summary["findingsCount"], summary["credsCount"]))

    def test_verified_count_requires_explicit_positive_status(self):
        findings = [{"verified": True}, {"status": "confirmed"}, {"status": "exploited"},
                    {"status": "suspected", "confidence": 1.0}, {"verified": "true"},
                    {"verified": True, "status": "retracted"}, {"verified": True, "superseded_by": "new"}]
        summary = session_to_graph(snapshot(kb={"findings": {"example.test": findings}}))["summary"]
        self.assertEqual(7, summary["findingsCount"])
        self.assertEqual(3, summary["verifiedFindingsCount"])

    def test_all_transcript_kinds_and_short_english_text_survive(self):
        records = [{"id": str(i), "kind": kind, "role": "assistant", "text": "Hi", "data": {"index": i}}
                   for i, kind in enumerate(["message", "tool", "approval", "worker", "error", "custom"])]
        graph = session_to_graph(snapshot(records))
        self.assertEqual(6, len(graph["actions"]))
        self.assertEqual([record["kind"] for record in records], [action["kind"] for action in graph["actions"]])
        self.assertEqual("Hi", graph["actions"][0]["text"])

    def test_error_only_session_does_not_become_success(self):
        actions = session_to_graph(snapshot([{"kind": "error", "text": "Failed", "status": "error"}]))["actions"]
        self.assertEqual(1, len(actions))
        self.assertEqual("error", actions[0]["status"])
        self.assertEqual("Failed", actions[0]["text"])

    def test_same_title_distinct_calls_are_never_deduplicated_or_sampled(self):
        actions = session_to_graph(snapshot([tool(i) for i in range(100)]))["actions"]
        self.assertEqual(100, len(actions))
        self.assertEqual([f"result {i}" for i in range(100)], [action["fullOutput"] for action in actions])
        self.assertEqual(100, len({action["id"] for action in actions}))

    def test_recorded_times_and_statuses_are_preserved(self):
        records = [tool(i, timestamp=stamp, status=status) for i, (stamp, status) in enumerate([(1000, "error"), (1060, "running"), (4600, "done")])]
        actions = session_to_graph(snapshot(records))["actions"]
        self.assertEqual([1000, 1060, 4600], [action["timestamp"] for action in actions])
        self.assertEqual([0, 60, 3600], [action["timeSeconds"] for action in actions])
        self.assertEqual(["error", "running", "done"], [action["status"] for action in actions])
        self.assertEqual([None] * 3, [action["stageKey"] for action in actions])

    def test_missing_or_invalid_clock_stays_unknown(self):
        records = [tool(0), tool(1, timestamp="not a time"), tool(2, timestamp="2026-09-25T12:00:00"), tool(3, timestamp="NaN")]
        for action in session_to_graph(snapshot(records))["actions"]:
            for field in ("timestamp", "timeSeconds", "timeOffset", "status"):
                self.assertIsNone(action[field])

    def test_numeric_and_timezone_aware_timestamps(self):
        actions = session_to_graph(snapshot([tool(0, timestamp="0"), tool(1, timestamp="1970-01-01T00:01:00Z")]))["actions"]
        self.assertEqual([0, 60], [action["timestamp"] for action in actions])

    def test_negative_clock_offsets_are_not_clamped(self):
        actions = session_to_graph(snapshot([tool(0, timestamp=100), tool(1, timestamp=90)]))["actions"]
        self.assertEqual(-10, actions[1]["timeSeconds"])
        self.assertEqual("T-10s", actions[1]["timeOffset"])

    def test_explicit_event_stage_is_only_source_of_action_membership(self):
        records = [tool(0, stageKey="recon", stageTitle="Recorded recon"),
                   tool(1, data={"stage": {"key": "verify", "title": "Recorded verify"}}), tool(2)]
        graph = session_to_graph(snapshot(records, extra={"stage": {"stage": "synthesis"}}))
        self.assertEqual(["recon", "verify", None], [action["stageKey"] for action in graph["actions"]])
        stages = {stage["key"]: stage for stage in graph["stages"]}
        self.assertEqual({"key": "recon", "title": "Recorded recon", "count": 1, "firstActionIndex": 0}, stages["recon"])
        self.assertEqual(0, stages["synthesis"]["count"])
        self.assertIsNone(stages["synthesis"]["firstActionIndex"])

    def test_saved_stage_history_is_not_applied_to_unlabeled_actions(self):
        graph = session_to_graph(snapshot([tool(0, timestamp=101)], extra={"stage": {"stage": "research", "history": [{"from": "recon", "to": "research", "ts": 100}]}}))
        self.assertIsNone(graph["actions"][0]["stageKey"])
        self.assertEqual(["recon", "research"], [stage["key"] for stage in graph["stages"]])
        self.assertTrue(all(stage["count"] == 0 for stage in graph["stages"]))

    def test_graph_has_one_node_per_real_action_and_sequence_edges(self):
        graph = session_to_graph(snapshot([tool(i) for i in range(7)]))
        self.assertEqual(7, len(graph["nodes"]))
        self.assertEqual(6, len(graph["edges"]))
        self.assertTrue(all(node["type"] == "eventNode" for node in graph["nodes"]))
        self.assertEqual([a["cardId"] for a in graph["actions"]], [node["id"] for node in graph["nodes"]])
        for index, edge in enumerate(graph["edges"]):
            self.assertEqual({"relation": "sequence"}, edge["data"])
            self.assertEqual(graph["nodes"][index]["id"], edge["source"])
            self.assertEqual(graph["nodes"][index + 1]["id"], edge["target"])

    def test_source_record_ids_survive_insertions_and_reordering(self):
        first = session_to_graph(snapshot([tool(1), tool(2)]))["actions"]
        second = session_to_graph(snapshot([tool(2), tool(0), tool(1)]))["actions"]
        before = {action["sourceId"]: action["id"] for action in first}
        after = {action["sourceId"]: action["id"] for action in second}
        self.assertEqual(before["call-1"], after["call-1"])
        self.assertEqual(before["call-2"], after["call-2"])

    def test_duplicate_or_missing_source_ids_do_not_drop_events(self):
        records = [tool(1), tool(1), {"kind": "error", "text": "No id"}]
        actions = session_to_graph(snapshot(records))["actions"]
        self.assertEqual(3, len({action["id"] for action in actions}))
        self.assertIsNone(actions[-1]["sourceId"])

    def test_full_event_details_and_large_outputs_survive_without_mutation(self):
        record = tool(0, output="x" * 5000, data={"arbitrary": {"detail": [1, 2, 3]}}, custom="retained")
        original = snapshot([record])
        before = deepcopy(original)
        action = session_to_graph(original)["actions"][0]
        self.assertEqual(record, action["data"])
        self.assertEqual(5000, len(action["fullOutput"]))
        self.assertEqual(before, original)
        action["data"]["data"]["arbitrary"]["detail"].append(4)
        self.assertEqual(before, original)

    def test_openai_legacy_result_is_joined_without_current_time_or_success(self):
        messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "http_fetch", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "content": "Recorded result"}]
        actions = session_to_graph(snapshot(messages=messages))["actions"]
        self.assertEqual(1, len(actions))
        self.assertEqual("Recorded result", actions[0]["fullOutput"])
        self.assertIsNone(actions[0]["timestamp"])
        self.assertIsNone(actions[0]["status"])
        self.assertEqual(messages[1], actions[0]["data"]["data"]["result"])

    def test_legacy_result_preserves_explicit_error_status(self):
        messages = [{"role": "assistant", "content": "", "timestamp": 100, "tool_calls": [{"id": "c1", "function": {"name": "run", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "timestamp": 110, "status": "error", "content": "Failed"}]
        action = session_to_graph(snapshot(messages=messages))["actions"][0]
        self.assertEqual(100, action["timestamp"])
        self.assertEqual("error", action["status"])
        self.assertEqual(110, action["data"]["data"]["resultTimestamp"])

    def test_assistant_envelope_done_is_not_tool_success(self):
        messages = [{"role": "assistant", "status": "done", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "run", "arguments": "{}"}}
        ]}]
        action = session_to_graph(snapshot(messages=messages))["actions"][0]
        self.assertIsNone(action["status"])

    def test_unannotated_result_does_not_confirm_old_call_status(self):
        messages = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "status": "running", "function": {"name": "run", "arguments": "{}"}}
        ]}, {"role": "tool", "tool_call_id": "c1", "content": "Recorded result"}]
        action = session_to_graph(snapshot(messages=messages))["actions"][0]
        self.assertIsNone(action["status"])

    def test_anthropic_tool_blocks_preserve_text_call_and_error_result(self):
        messages = [{"role": "assistant", "content": [{"type": "text", "text": "Plan"}, {"type": "tool_use", "id": "c1", "name": "run", "input": {"x": 1}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "Failed", "is_error": True}]}]
        actions = session_to_graph(snapshot(messages=messages))["actions"]
        self.assertEqual(2, len(actions))
        self.assertEqual("Plan", actions[0]["text"])
        self.assertEqual("Failed", actions[1]["fullOutput"])
        self.assertEqual("error", actions[1]["status"])
        self.assertIsNone(actions[1]["timestamp"])

    def test_anthropic_result_does_not_inherit_user_envelope_status(self):
        messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "run", "input": {}}]},
                    {"role": "user", "status": "done", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "Result"}]}]
        action = session_to_graph(snapshot(messages=messages))["actions"][0]
        self.assertIsNone(action["status"])

    def test_orphan_and_ambiguous_results_are_kept_separate(self):
        messages = [{"role": "assistant", "content": "", "agent_id": actor, "tool_calls": [{"id": "same", "function": {"name": "run", "arguments": "{}"}}]} for actor in ("a", "b")]
        messages.append({"role": "tool", "tool_call_id": "same", "content": "Unattributed result"})
        actions = session_to_graph(snapshot(messages=messages))["actions"]
        self.assertEqual(3, len(actions))
        self.assertEqual([None, None, "Unattributed result"], [action["fullOutput"] for action in actions])

    def test_legacy_actor_prevents_cross_agent_result_pairing(self):
        messages = [{"role": "assistant", "content": "", "agent_id": actor, "tool_calls": [{"id": "same", "function": {"name": "run", "arguments": "{}"}}]} for actor in ("a", "b")]
        messages.append({"role": "tool", "agent_id": "b", "tool_call_id": "same", "content": "B result"})
        actions = session_to_graph(snapshot(messages=messages))["actions"]
        self.assertEqual([None, "B result"], [action["fullOutput"] for action in actions])

    def test_empty_text_blocks_do_not_crash(self):
        actions = session_to_graph(snapshot(messages=[{"role": "assistant", "content": [{"type": "text", "text": ""}]}]))["actions"]
        self.assertEqual(1, len(actions))
        self.assertIsNone(actions[0]["text"])

    def test_malformed_collections_raise_readable_errors(self):
        cases = [None, snapshot(extra={"transcript": "bad"}), snapshot(["bad"]),
                 snapshot(kb={"findings": {"host": ["bad"]}}), snapshot(extra={"stage": {"history": "bad"}}),
                 snapshot(messages=[{"role": "assistant", "tool_calls": ["bad"]}]),
                 snapshot([tool(0, data="bad")]), snapshot([tool(0, timestamp=float("nan"))])]
        cases.extend([snapshot([tool(0, status=[])]), snapshot(messages=[{"role": {}}]),
                      snapshot(kb={"findings": {"host": [{"status": []}]}})])
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(SessionFormatError):
                    session_to_graph(invalid)

    def test_projection_is_json_serializable(self):
        graph = session_to_graph(snapshot([tool(0, input={"zero": 0, "false": False}, output=0)]))
        self.assertEqual("0", graph["actions"][0]["fullOutput"])
        self.assertTrue(json.dumps(graph, allow_nan=False))


if __name__ == "__main__":
    unittest.main()
