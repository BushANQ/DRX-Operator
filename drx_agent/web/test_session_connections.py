"""SQLite connection lifetime and transaction regressions (stdlib only)."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


from drx_agent.session import store as MODULE
from drx_agent.session.store import SessionStore
REAL_CONNECT = sqlite3.connect


class TrackedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False
        self.close_calls = 0
        self.transaction_exited = False
        self.fail_after_insert = False
        self.fail_on_select = False

    def __exit__(self, *args):
        result = super().__exit__(*args)
        self.transaction_exited = True
        return result

    def close(self):
        self.closed = True
        self.close_calls += 1
        return super().close()

    def execute(self, sql, *args, **kwargs):
        if self.fail_on_select and sql.lstrip().startswith("SELECT"):
            raise sqlite3.OperationalError("Synthetic read failure")
        cursor = super().execute(sql, *args, **kwargs)
        if self.fail_after_insert and sql.lstrip().startswith("INSERT"):
            raise sqlite3.OperationalError("Synthetic failure after write")
        return cursor


class SessionConnectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="drx-connection-regression-")
        self.addCleanup(self.directory.cleanup)
        self.connections = []
        self.fail_after_insert = False
        self.fail_on_select = False

        def connect(*args, **kwargs):
            kwargs["factory"] = TrackedConnection
            connection = REAL_CONNECT(*args, **kwargs)
            connection.fail_after_insert = self.fail_after_insert
            connection.fail_on_select = self.fail_on_select
            self.connections.append(connection)
            return connection

        self.patcher = patch.object(MODULE.sqlite3, "connect", side_effect=connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        def close_failed_connections():
            # Ensure a failed regression cannot itself leak test resources.
            for connection in self.connections:
                if not connection.closed:
                    connection.close()

        self.addCleanup(close_failed_connections)
        self.store = SessionStore(self.directory.name)

    def assert_connections_closed(self):
        self.assertTrue(self.connections)
        for connection in self.connections:
            self.assertTrue(connection.closed)
            self.assertEqual(1, connection.close_calls)
            self.assertTrue(connection.transaction_exited)
            with self.assertRaises(sqlite3.ProgrammingError):
                sqlite3.Connection.execute(connection, "SELECT 1")

    def save(self, name="Before"):
        self.store.save_session("session", name, "", {}, [], [], extra={"transcript": []})

    def test_initialization_closes_connection_and_persists_schema(self):
        self.assert_connections_closed()
        with closing(REAL_CONNECT(Path(self.directory.name) / "sessions.db")) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        self.assertIn("snapshot", columns)

    def test_all_successful_operations_close_connections(self):
        self.save()
        self.assertEqual("Before", self.store.load_session("session")["name"])
        self.assertEqual(1, len(self.store.list_sessions()))
        self.assertIsNone(self.store.load_session("missing"))
        self.assertEqual(5, len(self.connections))
        self.assert_connections_closed()

    def test_save_commits_before_connection_closes(self):
        self.save("Committed")
        with closing(REAL_CONNECT(Path(self.directory.name) / "sessions.db")) as connection:
            name, snapshot = connection.execute("SELECT name, snapshot FROM sessions WHERE id = ?", ("session",)).fetchone()
        self.assertEqual("Committed", name)
        self.assertEqual(1, json.loads(snapshot)["version"])
        self.assert_connections_closed()

    def test_exception_after_write_rolls_back_and_closes(self):
        self.save("Before")
        self.fail_after_insert = True
        with self.assertRaises(sqlite3.OperationalError):
            self.save("Must roll back")
        with closing(REAL_CONNECT(Path(self.directory.name) / "sessions.db")) as connection:
            name = connection.execute("SELECT name FROM sessions WHERE id = ?", ("session",)).fetchone()[0]
        self.assertEqual("Before", name)
        self.assert_connections_closed()

    def test_bad_snapshot_closes_connection_on_parse_error(self):
        self.save()
        with closing(REAL_CONNECT(Path(self.directory.name) / "sessions.db")) as connection, connection:
            connection.execute("UPDATE sessions SET snapshot = ?", ("{",))
        with self.assertRaises(json.JSONDecodeError):
            self.store.load_session("session")
        self.assert_connections_closed()

    def test_missing_legacy_files_close_connection(self):
        with closing(REAL_CONNECT(Path(self.directory.name) / "sessions.db")) as connection, connection:
            connection.execute("INSERT INTO sessions (id, metadata) VALUES (?, ?)", ("legacy", json.dumps({"active_targets": [], "extra": {}})))
        with self.assertRaises(FileNotFoundError):
            self.store.load_session("legacy")
        self.assert_connections_closed()

    def test_query_failure_closes_connection(self):
        self.fail_on_select = True
        with self.assertRaises(sqlite3.OperationalError):
            self.store.list_sessions()
        self.assert_connections_closed()

    def test_serialization_failure_does_not_open_connection(self):
        opened = len(self.connections)
        with self.assertRaises(TypeError):
            self.store.save_session("bad", "Bad", "", {"bad": object()}, [], [])
        self.assertEqual(opened, len(self.connections))
        self.assert_connections_closed()


if __name__ == "__main__":
    unittest.main()
