"""Evidence-preserving session projection helpers."""
import json
from copy import deepcopy
import math
from datetime import datetime
from urllib.parse import urlsplit

def _text(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)

def _label(value):
    return value.strip() if isinstance(value, str) and value.strip() else None

def _web_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            return value
    except ValueError:
        pass
    return None

def _target_summary(targets):
    if not targets:
        return None, None, None
    target = targets[0]
    host = _label(target.get("host"))
    url = _web_url(target.get("url")) or _web_url(target.get("target_url")) or _web_url(host)
    if url and (host is None or _web_url(host)):
        host = urlsplit(url).hostname
    parts = []
    ports = target.get("open_ports")
    if isinstance(ports, list) and ports:
        parts.append("端口 " + ", ".join(str(port) for port in ports))
    services = target.get("services")
    if isinstance(services, dict) and services:
        known_services = [f"{port}: {_text(service)}" for port, service in services.items() if service not in (None, "", {})]
        if known_services:
            parts.append("服务 " + ", ".join(known_services))
    if _label(target.get("notes")):
        parts.append(target["notes"])
    return host, url, " · ".join(parts) or None


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            numeric = float(value)
            return numeric if math.isfinite(numeric) else None
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                # A timezone-free date cannot determine an absolute timestamp.
                return parsed.timestamp() if parsed.tzinfo is not None else None
            except (ValueError, OverflowError):
                pass
    return None

def _time_offset(seconds):
    if seconds is None:
        return None
    sign = "-" if seconds < 0 else "+"
    seconds = abs(seconds)
    minutes, remaining = divmod(seconds, 60)
    if minutes:
        return f"T{sign}{int(minutes)}m{remaining:05.2f}".rstrip("0").rstrip(".") + "s"
    return f"T{sign}{seconds:g}s"

def _explicit_stage(record):
    """Only event-owned stage metadata can assign an event to a stage."""
    for source in (record, record.get("data", {})):
        if not isinstance(source, dict):
            continue
        stage = source.get("stage")
        key = _label(source.get("stageKey")) or _label(source.get("stage_key"))
        title = _label(source.get("stageTitle")) or _label(source.get("stage_title"))
        if isinstance(stage, str):
            key = key or _label(stage)
        elif isinstance(stage, dict):
            key = key or _label(stage.get("key")) or _label(stage.get("stage"))
            title = title or _label(stage.get("title"))
        if key or title:
            return key or title, title or key
    return None, None


class SessionFormatError(ValueError):
    """A saved session cannot be represented without discarding malformed data."""

def _sequence(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise SessionFormatError(f"{field} 必须为列表")
    return value

def _legacy_records(messages):
    """Join explicitly identified calls/results without inventing times/statuses."""
    records = []
    calls = {}

    def call(message, index, identity, name, payload, suffix, status=None):
        actor = _label(message.get("agent_id"))
        record = {
            "id": f"legacy-{index}-{suffix}", "kind": "tool", "actor": actor,
            "tool": name, "input": payload, "timestamp": message.get("timestamp"),
            "status": status, "data": {"call": deepcopy(message)},
        }
        for field in ("stage", "stageKey", "stageTitle", "stage_key", "stage_title"):
            if field in message:
                record[field] = deepcopy(message[field])
        stage_key, stage_title = _explicit_stage(message)
        if stage_key:
            record.update(stageKey=stage_key, stageTitle=stage_title)
        records.append(record)
        if identity is not None:
            calls.setdefault(str(identity), []).append(record)

    def result(message, index, identity, output, *, name=None, status=None, suffix="result"):
        actor = _label(message.get("agent_id"))
        candidates = calls.get(str(identity), []) if identity is not None else []
        if actor is not None:
            candidates = [candidate for candidate in candidates if candidate.get("actor") == actor]
        candidates = [candidate for candidate in candidates if "output" not in candidate]
        if len(candidates) == 1:
            record = candidates[0]
            record["output"] = deepcopy(output)
            # The assistant envelope's completion is not the tool's outcome.
            # Likewise an unannotated result cannot confirm a call's old status.
            record["status"] = status
            record["data"]["result"] = deepcopy(message)
            record["data"]["resultTimestamp"] = message.get("timestamp")
        else:
            records.append({
                "id": f"legacy-{index}-{suffix}", "kind": "tool", "role": message.get("role"),
                "actor": actor, "tool": name, "output": deepcopy(output),
                "timestamp": message.get("timestamp"), "status": status,
                "data": {"result": deepcopy(message)},
            })

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise SessionFormatError("messages 含无效记录")
        role = message.get("role")
        content = message.get("content")
        if role in {"tool", "function"}:
            result(message, index, message.get("tool_call_id"), content,
                   name=message.get("name"), status=message.get("status"))
            continue
        tool_calls = _sequence(message.get("tool_calls"), "tool_calls")
        blocks = content if isinstance(content, list) else []
        text_blocks = [block for block in blocks if not isinstance(block, dict) or block.get("type") not in {"tool_use", "tool_result"}]
        has_tool_blocks = any(isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"} for block in blocks)
        if text_blocks or (not isinstance(content, list) and content not in (None, "")) or not (tool_calls or has_tool_blocks):
            text = "\n".join((_text(block.get("text", block)) or "") if isinstance(block, dict) else (_text(block) or "") for block in text_blocks) if isinstance(content, list) else _text(content)
            records.append({**deepcopy(message), "id": f"legacy-{index}-message", "kind": "message",
                            "actor": _label(message.get("agent_id")), "text": text,
                            "data": {"message": deepcopy(message)}})
        for call_index, item in enumerate(tool_calls):
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                raise SessionFormatError("tool_calls 含无效记录")
            function = item["function"]
            call(message, index, item.get("id"), function.get("name"), function.get("arguments"), f"call-{call_index}", item.get("status"))
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                call(message, index, block.get("id"), block.get("name"), block.get("input"), f"block-{block_index}", block.get("status"))
            elif block.get("type") == "tool_result":
                result(message, index, block.get("tool_use_id"), block.get("content"),
                       status="error" if block.get("is_error") is True else message.get("status"),
                       suffix=f"result-{block_index}")
    return records
