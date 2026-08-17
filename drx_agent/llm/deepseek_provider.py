"""DeepSeek (OpenAI-compatible) provider with streaming + tool-call support."""

import json

from drx_agent.llm.base import (
    AgentEvent,
    AgentEventType,
    LLMConfig,
    LLMError,
    LLMProvider,
)


def _extract_usage(usage_obj):
    
    if usage_obj is None:
        return None
    out = {}
    for attr in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = getattr(usage_obj, attr, None)
        if v is not None:
            out[attr] = int(v)
    for attr in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        v = getattr(usage_obj, attr, None)
        if v is not None:
            out[attr] = int(v)
    return out or None


class DeepSeekProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        self.config = config
        try:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url or "https://api.deepseek.com",
            )
        except ImportError:
            raise LLMError("openai package not installed. Run: pip install openai")

    async def chat(self, messages, tools=None, stream=True):
        try:
            kwargs = {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            if stream:
                async for ev in self._stream(kwargs):
                    yield ev
                return

            response = await self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            msg = choice.message

            if msg.content:
                yield AgentEvent(type=AgentEventType.TEXT, content=msg.content)

            assistant_message = {"role": "assistant", "content": msg.content or ""}
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                serialized_calls = []
                for tc in tool_calls:
                    name = tc.function.name
                    raw_args = tc.function.arguments or "{}"
                    try:
                        parsed = json.loads(raw_args)
                    except json.JSONDecodeError:
                        parsed = {"_raw": raw_args}
                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        tool_name=name,
                        tool_input=parsed,
                        metadata={"tool_call_id": tc.id},
                    )
                    serialized_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": name, "arguments": raw_args},
                    })
                assistant_message["tool_calls"] = serialized_calls

            usage = _extract_usage(getattr(response, "usage", None))
            yield AgentEvent(
                type=AgentEventType.DONE,
                metadata={
                    "finish_reason": choice.finish_reason,
                    "assistant_message": assistant_message,
                    "usage": usage,
                    "model": self.config.model,
                },
            )
        except Exception as e:
            yield AgentEvent(type=AgentEventType.ERROR, content=str(e))

    async def _stream(self, kwargs):
        
        # Ask the API for a final usage chunk (stream_options.include_usage=True).
        kwargs = {**kwargs, "stream_options": {"include_usage": True}}
        response = await self.client.chat.completions.create(**kwargs, stream=True)
        accumulated_text: list[str] = []
        tool_call_accum: dict[int, dict] = {}
        finish_reason: str | None = None
        usage_raw = None

        async for chunk in response:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage_raw = chunk_usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            text_chunk = getattr(delta, "content", None)
            if text_chunk:
                accumulated_text.append(text_chunk)
                yield AgentEvent(type=AgentEventType.TEXT, content=text_chunk)

            tc_deltas = getattr(delta, "tool_calls", None) or []
            for tc_delta in tc_deltas:
                idx = getattr(tc_delta, "index", 0) or 0
                slot = tool_call_accum.setdefault(idx, {"id": "", "name": "", "args": ""})
                if getattr(tc_delta, "id", None):
                    slot["id"] = tc_delta.id
                fn = getattr(tc_delta, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments

            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

        serialized_calls = []
        for idx in sorted(tool_call_accum.keys()):
            slot = tool_call_accum[idx]
            if not slot["name"]:
                continue
            try:
                parsed = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                parsed = {"_raw": slot["args"]}
            yield AgentEvent(
                type=AgentEventType.TOOL_CALL,
                tool_name=slot["name"],
                tool_input=parsed,
                metadata={"tool_call_id": slot["id"]},
            )
            serialized_calls.append({
                "id": slot["id"],
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
            })

        assistant_message = {"role": "assistant", "content": "".join(accumulated_text)}
        if serialized_calls:
            assistant_message["tool_calls"] = serialized_calls

        yield AgentEvent(
            type=AgentEventType.DONE,
            metadata={
                "finish_reason": finish_reason,
                "assistant_message": assistant_message,
                "usage": _extract_usage(usage_raw),
                "model": self.config.model,
            },
        )

    def count_tokens(self, messages):
        return sum(len(m.get("content", "") or "") // 4 for m in messages)

