"""Anthropic Claude provider with streaming + tool_use support.

Converts between OpenAI-format tool schemas/messages and Anthropic's
tool_use content blocks so the agent keeps one canonical format."""

import json

from drx_agent.llm.base import (
    AgentEvent,
    AgentEventType,
    LLMConfig,
    LLMError,
    LLMProvider,
)


def _to_anthropic_tools(openai_tools):
    
    out = []
    for t in openai_tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        out.append({
            "name": name,
            "description": fn.get("description", "") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _to_anthropic_messages(messages):
    
    system_parts = []
    out = []
    pending_user_tool_results: list[dict] = []

    def _flush_tool_results():
        if pending_user_tool_results:
            out.append({"role": "user", "content": list(pending_user_tool_results)})
            pending_user_tool_results.clear()

    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        if role == "tool":
            # Tool results accumulate and flush as one user message before the next assistant turn.
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            pending_user_tool_results.append({
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": text,
            })
            continue

        _flush_tool_results()

        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # Multimodal: OpenAI image_url blocks → Anthropic image source blocks; {type:text} passes through.
                # Anthropic accepts {type:image, source:{type:url}}
                converted = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "image_url":
                        url = (block.get("image_url") or {}).get("url", "")
                        if url.startswith("data:"):
                            try:
                                head, b64 = url.split(",", 1)
                                media_type = head.split(";")[0][5:] or "image/png"
                            except Exception:
                                media_type, b64 = "image/png", ""
                            converted.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64,
                                },
                            })
                        else:
                            # Anthropic accepts {type:image, source:{type:url}}
                            converted.append({
                                "type": "image",
                                "source": {"type": "url", "url": url},
                            })
                    else:
                        converted.append(block)
                out.append({"role": "user", "content": converted})
            else:
                out.append({"role": "user", "content": content or ""})
            continue

        if role == "assistant":
            blocks = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "input": args,
                })
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            out.append({"role": "assistant", "content": blocks})
            continue

    _flush_tool_results()
    system = "\n\n".join(system_parts) if system_parts else ""
    return system, out


def _extract_usage(usage_obj):
    if usage_obj is None:
        return None
    out = {}
    # Anthropic reports input_tokens / output_tokens.
    for attr, dest in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("cache_read_input_tokens", "prompt_cache_hit_tokens"),
        ("cache_creation_input_tokens", "prompt_cache_miss_tokens"),
    ):
        v = getattr(usage_obj, attr, None)
        if v is not None:
            out[dest] = int(v)
    if "prompt_tokens" in out and "completion_tokens" in out:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out or None


class AnthropicProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        self.config = config
        try:
            import anthropic
            self.client = anthropic.AsyncAnthropic(api_key=config.api_key)
        except ImportError:
            raise LLMError("anthropic package not installed. Run: pip install anthropic")

    async def chat(self, messages, tools=None, stream=True):
        try:
            system, anthropic_messages = _to_anthropic_messages(messages)
            anthropic_tools = _to_anthropic_tools(tools) if tools else None

            kwargs = {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "messages": anthropic_messages,
            }
            if system:
                kwargs["system"] = system
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools

            if stream:
                async for ev in self._stream(kwargs):
                    yield ev
                return

            response = await self.client.messages.create(**kwargs)
            text_parts = []
            serialized_calls = []
            for block in response.content or []:
                btype = getattr(block, "type", None)
                if btype == "text":
                    text = getattr(block, "text", "")
                    if text:
                        text_parts.append(text)
                        yield AgentEvent(type=AgentEventType.TEXT, content=text)
                elif btype == "tool_use":
                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        tool_name=getattr(block, "name", ""),
                        tool_input=dict(getattr(block, "input", {}) or {}),
                        metadata={"tool_call_id": getattr(block, "id", "")},
                    )
                    serialized_calls.append({
                        "id": getattr(block, "id", ""),
                        "type": "function",
                        "function": {
                            "name": getattr(block, "name", ""),
                            "arguments": json.dumps(
                                dict(getattr(block, "input", {}) or {}),
                                ensure_ascii=False,
                            ),
                        },
                    })

            assistant_message = {"role": "assistant", "content": "".join(text_parts)}
            if serialized_calls:
                assistant_message["tool_calls"] = serialized_calls

            yield AgentEvent(
                type=AgentEventType.DONE,
                metadata={
                    "finish_reason": getattr(response, "stop_reason", None),
                    "assistant_message": assistant_message,
                    "usage": _extract_usage(getattr(response, "usage", None)),
                    "model": self.config.model,
                },
            )
        except Exception as e:
            yield AgentEvent(type=AgentEventType.ERROR, content=str(e))

    async def _stream(self, kwargs):
        text_parts: list[str] = []
        # Accumulator for tool_use: block_index -> {id, name, input_json_str}
        tool_blocks: dict[int, dict] = {}
        stop_reason = None
        usage_raw = None

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    dtype = getattr(delta, "type", "")
                    if dtype == "text_delta":
                        text = getattr(delta, "text", "")
                        if text:
                            text_parts.append(text)
                            yield AgentEvent(type=AgentEventType.TEXT, content=text)
                    elif dtype == "input_json_delta":
                        idx = getattr(event, "index", 0)
                        slot = tool_blocks.get(idx)
                        if slot is not None:
                            slot["args"] += getattr(delta, "partial_json", "")
                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block is None:
                        continue
                    btype = getattr(block, "type", "")
                    if btype == "tool_use":
                        idx = getattr(event, "index", 0)
                        tool_blocks[idx] = {
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "args": "",
                        }
                elif etype == "message_delta":
                    sr = getattr(getattr(event, "delta", None), "stop_reason", None)
                    if sr:
                        stop_reason = sr
                elif etype == "message_stop":
                    pass
            # After the stream context exits, the SDK aggregates usage.
            try:
                final_msg = await stream.get_final_message()
                usage_raw = getattr(final_msg, "usage", None)
            except Exception:
                pass

        serialized_calls = []
        for idx in sorted(tool_blocks.keys()):
            slot = tool_blocks[idx]
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
                "id": slot["id"], "type": "function",
                "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
            })

        assistant_message = {"role": "assistant", "content": "".join(text_parts)}
        if serialized_calls:
            assistant_message["tool_calls"] = serialized_calls

        yield AgentEvent(
            type=AgentEventType.DONE,
            metadata={
                "finish_reason": stop_reason,
                "assistant_message": assistant_message,
                "usage": _extract_usage(usage_raw),
                "model": self.config.model,
            },
        )

    def count_tokens(self, messages):
        return sum(len((m.get("content") or "") if isinstance(m.get("content"), str) else "") // 4 for m in messages)

