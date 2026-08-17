import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List


class EventType(Enum):
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SUB_AGENT_DISPATCH = "sub_agent_dispatch"
    SUB_AGENT_RESULT = "sub_agent_result"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESPONSE = "approval_response"
    TARGET_SWITCH = "target_switch"
    SESSION_SAVE = "session_save"
    SESSION_RESTORE = "session_restore"
    STATUS_UPDATE = "status_update"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self):
        self._subscribers: Dict[EventType, List[Handler]] = {
            t: [] for t in EventType
        }
        self._lock = threading.Lock()

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        with self._lock:
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: Handler) -> None:
        with self._lock:
            try:
                self._subscribers[event_type].remove(handler)
            except ValueError:
                pass

    def publish(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._subscribers[event.type])
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logging.exception("Handler failed")
