import uuid
import time

from drx_agent.agent.knowledge_base import KnowledgeBase
from drx_agent.session.store import SessionStore


class SessionManager:
    def __init__(self, storage_dir: str):
        self.store = SessionStore(storage_dir)

    def save(self, kb, messages, active_targets, name="", phase="",
             todos=None, mode="", session_usage=None, frontier=None) -> str:
        session_id = str(uuid.uuid4())[:12]
        self.store.save_session(
            session_id=session_id,
            name=name or f"session-{session_id}",
            phase=phase,
            kb_data=kb.to_dict(),
            messages=messages,
            active_targets=active_targets,
            extra={
                "todos": todos or [],
                "mode": mode or "act",
                "session_usage": session_usage or {},
                "frontier": frontier or {},
            },
        )
        return session_id

    def restore(self, session_id: str) -> dict | None:
        data = self.store.load_session(session_id)
        if not data:
            return None
        kb = KnowledgeBase.from_dict(data["kb_data"])
        meta = data.get("metadata", {}) or {}
        extra = meta.get("extra", {}) or {}
        return {
            "kb": kb,
            "messages": data.get("messages", []),
            "active_targets": meta.get("active_targets", []),
            "phase": data.get("phase", ""),
            "todos": extra.get("todos", []),
            "mode": extra.get("mode", "act"),
            "session_usage": extra.get("session_usage", {}),
            "frontier": extra.get("frontier", {}),
        }

    def checkpoint(self, kb, phase, messages, active_targets) -> str:
        return self.save(
            kb=kb,
            messages=messages,
            active_targets=active_targets,
            name=f"checkpoint-{phase}-{time.strftime('%H%M%S')}",
            phase=phase,
        )

    def list_sessions(self) -> list[dict]:
        return self.store.list_sessions()
