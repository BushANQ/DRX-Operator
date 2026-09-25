"""Regression cases for the individually audited dashboard defects."""
import unittest

from drx_agent.web.server import _session_to_graph


def snapshot(records=None, kb=None):
    return {"id": "test", "metadata": {"extra": {"transcript": records or []}},
            "messages": [], "kb_data": kb or {}}


class DashboardRegressions(unittest.TestCase):
    def test_missing_target_does_not_invent_a_host_port_or_service(self):
        summary = _session_to_graph(snapshot())["summary"]
        self.assertEqual(summary["targetsCount"], 0)
        self.assertIsNone(summary["targetHost"])
        self.assertIsNone(summary["targetUrl"])
        self.assertIsNone(summary["targetNotes"])

    def test_explicit_https_url_is_preserved_without_guessing_ports(self):
        summary = _session_to_graph(snapshot(kb={"targets": {
            "example.test": {"url": "https://example.test/path", "open_ports": [443]},
        }}))["summary"]
        self.assertEqual(summary["targetUrl"], "https://example.test/path")
        self.assertNotIn("3000", summary["targetNotes"])

    def test_host_buckets_preserve_all_findings_and_credentials(self):
        graph = _session_to_graph(snapshot(kb={
            "findings": {"example.test": [{"claim": "first"}, {"claim": "second"}]},
            "credentials": {"example.test": [{"username": str(i)} for i in range(25)]},
        }))
        self.assertEqual(graph["summary"]["findingsCount"], 2)
        self.assertEqual(graph["summary"]["credsCount"], 25)
        self.assertEqual(len(graph["summary"]["creds"]), 25)
        self.assertEqual(graph["summary"]["findings"][1]["host"], "example.test")

    def test_repeated_requests_are_not_dropped_or_sampled(self):
        records = [{"id": f"call-{i}", "kind": "tool", "tool": "http_fetch",
                    "input": {"url": f"https://example.test/{i}"}, "output": str(i)}
                   for i in range(100)]
        graph = _session_to_graph(snapshot(records))
        self.assertEqual(len(graph["actions"]), 100)
        self.assertEqual(graph["summary"]["totalActions"], 100)
        self.assertEqual(graph["actions"][-1]["fullOutput"], "99")


if __name__ == "__main__":
    unittest.main()
