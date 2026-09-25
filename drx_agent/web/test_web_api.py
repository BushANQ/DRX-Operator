"""Session API failure-state regression tests; use isolated temporary SQLite only."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


from fastapi.testclient import TestClient
from drx_agent.session.store import SessionStore
from drx_agent.web.server import create_app


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="drx-api-regression-")
        self.addCleanup(self.directory.cleanup)
        self.store = SessionStore(self.directory.name)
        self.client = TestClient(create_app(session_dir=self.directory.name), raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def insert(self, session_id, *, snapshot=None, metadata=None):
        with closing(sqlite3.connect(Path(self.directory.name) / "sessions.db")) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO sessions (id, name, created_at, phase, metadata, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, "Synthetic API test", 1000, "", metadata, snapshot),
                )

    def assert_error_on_both_routes(self, session_id, expected):
        for suffix in ("", "/timeline"):
            with self.subTest(suffix=suffix):
                response = self.client.get(f"/api/sessions/{session_id}{suffix}")
                self.assertEqual(expected, response.status_code, response.text)
                self.assertIn("application/json", response.headers.get("content-type", ""))
                detail = response.json().get("detail")
                self.assertTrue(detail)
                self.assertNotIn(self.directory.name, response.text)
                self.assertNotIn("private-database-detail", response.text)
                self.assertNotIn("Traceback", response.text)
                self.assertNotIn("actions", response.json())

    def test_missing_session_is_404(self):
        self.assert_error_on_both_routes("missing", 404)

    def test_invalid_json_snapshot_is_422(self):
        self.insert("invalid-json", snapshot="{")
        self.assert_error_on_both_routes("invalid-json", 422)

    def test_unknown_snapshot_version_is_422(self):
        self.insert("unsupported", snapshot=json.dumps({"version": 999}))
        self.assert_error_on_both_routes("unsupported", 422)

    def test_missing_snapshot_fields_are_422(self):
        self.insert("missing-fields", snapshot=json.dumps({"version": 1}))
        self.assert_error_on_both_routes("missing-fields", 422)

    def test_invalid_transcript_shape_is_422(self):
        self.insert("invalid-transcript", snapshot=json.dumps({
            "version": 1, "metadata": {"active_targets": [], "extra": {"transcript": "invalid"}},
            "messages": [], "kb_data": {},
        }))
        self.assert_error_on_both_routes("invalid-transcript", 422)

    def test_invalid_status_shape_is_422(self):
        self.insert("invalid-status", snapshot=json.dumps({
            "version": 1, "metadata": {"active_targets": [], "extra": {"transcript": [{"id": "r1", "kind": "tool", "status": []}]}},
            "messages": [], "kb_data": {},
        }))
        self.assert_error_on_both_routes("invalid-status", 422)

    def test_missing_legacy_snapshot_files_are_422(self):
        self.insert("missing-legacy", metadata=json.dumps({"active_targets": [], "extra": {}}))
        self.assert_error_on_both_routes("missing-legacy", 422)

    def test_storage_error_is_500_without_private_details(self):
        with patch.object(SessionStore, "load_session", side_effect=sqlite3.DatabaseError(
            f"private-database-detail {self.directory.name}/sessions.db"
        )):
            self.assert_error_on_both_routes("storage-error", 500)

    def test_store_initialization_error_is_500_without_private_details(self):
        with patch("drx_agent.session.store.SessionStore", side_effect=sqlite3.OperationalError(
            f"private-database-detail {self.directory.name}/sessions.db"
        )):
            self.assert_error_on_both_routes("storage-unavailable", 500)

    def test_valid_empty_session_is_200_with_empty_data(self):
        self.store.save_session("empty", "Synthetic empty", "", {}, [], [], extra={"transcript": []})
        graph = self.client.get("/api/sessions/empty")
        self.assertEqual(200, graph.status_code, graph.text)
        self.assertEqual([], graph.json()["actions"])
        self.assertEqual([], graph.json()["nodes"])
        timeline = self.client.get("/api/sessions/empty/timeline")
        self.assertEqual(200, timeline.status_code, timeline.text)
        self.assertEqual([], timeline.json())


if __name__ == "__main__":
    unittest.main()
