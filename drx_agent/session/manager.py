import uuid
import time

from drx_agent.agent.knowledge_base import KnowledgeBase
from drx_agent.session.store import SessionStore
from drx_agent.session.usage import restore_usage


class SessionManager:
    def __init__(self, storage_dir: str):
        self.store = SessionStore(storage_dir)

    def save(self, kb, messages, active_targets, name="", phase="",
             todos=None, mode="", session_usage=None, frontier=None,
             handoff=None, stage=None, forum=None, claims=None,
             moderator=None, irc=None, project_note=None, team=None, transcript=None,
             model_selection=None, execution_capture=None, swarm=None) -> str:
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
                "handoff": handoff or {},
                "stage": stage or {},
                "forum": forum or {},
                "claims": claims or {},
                "moderator": moderator or {},
                "irc": irc or {},
                "project_note": project_note or {},
                "team": team or {},
                "transcript": transcript,
                "model_selection": model_selection,
                "execution_capture": execution_capture,
                "swarm": swarm,
            },
        )
        return session_id

    def restore(self, session_id: str) -> dict | None:
        data = self.store.load_session(session_id)
        if data is None:
            return None
        kb = KnowledgeBase.from_dict(data["kb_data"])
        meta = data["metadata"]
        extra = meta.get("extra", {})
        usage = restore_usage(extra.get("session_usage"))
        return {
            "kb": kb,
            "messages": data["messages"],
            "active_targets": meta.get("active_targets", []),
            "phase": data.get("phase", ""),
            "todos": extra.get("todos", []),
            "mode": extra.get("mode", "act") or "act",
            "session_usage": usage,
            "frontier": extra.get("frontier", {}),
            "handoff": extra.get("handoff", {}),
            "stage": extra.get("stage", {}),
            "forum": extra.get("forum", {}),
            "claims": extra.get("claims", {}),
            "moderator": extra.get("moderator", {}),
            "irc": extra.get("irc", {}),
            "project_note": extra.get("project_note", {}),
            "team": extra.get("team", {}),
            "transcript": extra.get("transcript"),
            "model_selection": extra.get("model_selection"),
            "execution_capture": extra.get("execution_capture"),
            "swarm": extra.get("swarm"),
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
