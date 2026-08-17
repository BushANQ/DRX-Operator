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
        with sqlite3.connect(db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, name TEXT, created_at REAL,
                phase TEXT, metadata TEXT)""")

    def save_session(self, session_id, name, phase, kb_data, messages,
                     active_targets, extra=None):
        db_path = os.path.join(self.storage_dir, "sessions.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (id, name, created_at, phase, metadata) VALUES (?, ?, ?, ?, ?)",
                (session_id, name, time.time(), phase,
                 json.dumps({
                     "active_targets": active_targets,
                     "extra": extra or {},
                 })),
            )
        session_dir = os.path.join(self.storage_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "messages.json"), "w") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        with open(os.path.join(session_dir, "kb.json"), "w") as f:
            json.dump(kb_data, f, ensure_ascii=False, indent=2)

    def load_session(self, session_id):
        db_path = os.path.join(self.storage_dir, "sessions.db")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            metadata = json.loads(row[4]) if row[4] else {}
            session_dir = os.path.join(self.storage_dir, session_id)
            messages = []
            kb_data = {}
            mp = os.path.join(session_dir, "messages.json")
            if os.path.exists(mp):
                with open(mp) as f:
                    messages = json.load(f)
            kp = os.path.join(session_dir, "kb.json")
            if os.path.exists(kp):
                with open(kp) as f:
                    kb_data = json.load(f)
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
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, phase FROM sessions ORDER BY created_at DESC"
            ).fetchall()
            return [
                {"id": r[0], "name": r[1], "created_at": r[2], "phase": r[3]}
                for r in rows
            ]
