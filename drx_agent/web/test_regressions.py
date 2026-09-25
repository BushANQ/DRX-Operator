"""Regression cases for the individually audited dashboard defects."""
import unittest

from drx_agent.web.server import _session_to_graph


def snapshot(records=None, kb=None):
    return {"id": "test", "metadata": {"extra": {"transcript": records or []}},
            "messages": [], "kb_data": kb or {}}


class DashboardRegressions(unittest.TestCase):
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
