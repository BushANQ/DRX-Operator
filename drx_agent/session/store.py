from contextlib import closing
import json
import os
import sqlite3
import time


class SessionStore:
    def __init__(self, storage_dir: str):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        db_path = os.path.join(self.storage_dir, "sessions.db")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, name TEXT, created_at REAL,
                phase TEXT, metadata TEXT)""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "snapshot" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN snapshot TEXT")

    def save_session(self, session_id, name, phase, kb_data, messages,
                     active_targets, extra=None):
        # Serialize before opening the transaction: neither an encoding failure nor
        # an interrupted database write may publish a partially saved session.
        snapshot = json.dumps({
            "version": 1,
            "metadata": {
                "active_targets": active_targets,
                "extra": extra if extra is not None else {},
            },
            "messages": messages,
            "kb_data": kb_data,
        }, ensure_ascii=False)
        db_path = os.path.join(self.storage_dir, "sessions.db")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (id, name, created_at, phase, metadata, snapshot)
                   VALUES (?, ?, ?, ?, NULL, ?)""",
                (session_id, name, time.time(), phase, snapshot),
            )

    def load_session(self, session_id):
        db_path = os.path.join(self.storage_dir, "sessions.db")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                "SELECT id, name, created_at, phase, metadata, snapshot "
                "FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            if row[5] is not None:
                snapshot = json.loads(row[5])
                if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
                    raise ValueError(f"Unsupported session snapshot: {session_id}")
                metadata = snapshot["metadata"]
                messages = snapshot["messages"]
                kb_data = snapshot["kb_data"]
            else:
                # Legacy sessions used metadata in SQLite and two required files.
                # A missing file is an incomplete save, not an empty session.
                metadata = json.loads(row[4])
                session_dir = os.path.join(self.storage_dir, session_id)
                with open(os.path.join(session_dir, "messages.json"), encoding="utf-8") as f:
                    messages = json.load(f)
                with open(os.path.join(session_dir, "kb.json"), encoding="utf-8") as f:
                    kb_data = json.load(f)
            if (
                not isinstance(metadata, dict)
                or not isinstance(metadata.get("active_targets"), list)
                or not isinstance(metadata.get("extra", {}), dict)
                or not isinstance(messages, list)
                or not all(isinstance(message, dict) for message in messages)
                or not isinstance(kb_data, dict)
            ):
                raise ValueError(f"Invalid session snapshot: {session_id}")
            return {
                "id": row[0],
                "name": row[1],
                "created_at": row[2],
                "phase": row[3],
                "metadata": metadata,
                "messages": messages,
                "kb_data": kb_data,
            }

    def list_sessions(self):
        db_path = os.path.join(self.storage_dir, "sessions.db")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                "SELECT id, name, created_at, phase FROM sessions ORDER BY created_at DESC"
            ).fetchall()
            return [
                {"id": r[0], "name": r[1], "created_at": r[2], "phase": r[3]}
                for r in rows
            ]
