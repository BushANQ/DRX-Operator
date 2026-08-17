from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator
from enum import Enum

class AgentEventType(str, Enum):
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TEXT = "text"
    ERROR = "error"
    DONE = "done"

@dataclass
class AgentEvent:
    type: AgentEventType
    content: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

@dataclass
class LLMConfig:
    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096

class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None, stream: bool = True) -> AsyncIterator[AgentEvent]:
        ...

    @abstractmethod
    def count_tokens(self, messages: list[dict]) -> int:
        ...

class LLMError(Exception):
    pass
