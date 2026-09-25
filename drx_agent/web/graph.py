"""Pure, lossless projections of saved session records for the web dashboard.

Presentation labels describe record types, never inferred security outcomes.
Missing evidence stays missing; importing legacy messages never creates a clock.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from urllib.parse import urlsplit


class SessionFormatError(ValueError):
    """A saved session cannot be represented without discarding malformed data."""


KINDS = {
    "message": ("消息", "#38bdf8"),
    "tool": ("工具", "#06b6d4"),
    "approval": ("审批", "#f59e0b"),
    "worker": ("协作", "#a855f7"),
    "error": ("错误", "#f43f5e"),
}


def _text(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _label(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value, field):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SessionFormatError(f"{field} 必须为对象")
    return value


def _sequence(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise SessionFormatError(f"{field} 必须为列表")
    return value


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None
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


def _flatten(value, field, *, targets=False):
    """Support current host buckets and legacy flat lists, preserving every item."""
    if value is None:
        return []
    if isinstance(value, dict):
        entries = value.items()
    elif isinstance(value, list):
        entries = [(None, item) for item in value]
    else:
        raise SessionFormatError(f"{field} 必须为对象或列表")
    result = []
    for host, bucket in entries:
        items = bucket if isinstance(bucket, list) else [bucket]
        for item in items:
            if targets and isinstance(item, str):
                item = {"host": item}
            if not isinstance(item, dict):
                raise SessionFormatError(f"{field} 含无效记录")
            record = deepcopy(item)
            if host is not None and not record.get("host"):
                record["host"] = str(host)
            result.append(record)
    return result


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


def _legacy_records(messages):
    """Join explicitly identified calls/results without inventing times/statuses."""
    records = []
    calls = {}

    def call(message, index, identity, name, payload, suffix, status=None):
        actor = _label(message.get("agent_id"))
        record = {
            "id": json.dumps(["legacy-tool", actor, identity], ensure_ascii=False) if identity is not None else f"legacy-{index}-{suffix}",
            "kind": "tool", "actor": actor,
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
        _validate_text_fields(message, ("role", "agent_id", "status", "name", "id"))
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
            records.append({**deepcopy(message), "id": message.get("id") or f"legacy-{index}-message", "kind": "message",
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
                       status="error" if block.get("is_error") is True else block.get("status"),
                       suffix=f"result-{block_index}")
    return records


def _is_verified(finding):
    status = finding.get("status")
    _validate_text_fields(finding, ("status",))
    if status == "retracted" or finding.get("superseded_by"):
        return False
    return status in {"confirmed", "exploited"} or finding.get("verified") is True


def _validate_text_fields(record, fields):
    for field in fields:
        if record.get(field) is not None and not isinstance(record[field], str):
            raise SessionFormatError(f"{field} 必须为文本")


def _record_identity(record, index, used):
    source_id = _label(record.get("id"))
    base = "event-" + hashlib.sha256(source_id.encode()).hexdigest() if source_id else f"event-position-{index}"
    occurrence = used.get(base, 0)
    used[base] = occurrence + 1
    return f"{base}-{occurrence}" if occurrence else base


def session_to_graph(raw):
    if not isinstance(raw, dict):
        raise SessionFormatError("session 必须为对象")
    try:
        json.dumps(raw, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as error:
        raise SessionFormatError("session 含无效 JSON 数据") from error
    raw = _mapping(raw, "session")
    metadata = _mapping(raw.get("metadata"), "metadata")
    extra = _mapping(metadata.get("extra"), "extra")
    kb = _mapping(raw.get("kb_data"), "kb_data")
    targets = _flatten(kb.get("targets"), "targets", targets=True)
    findings = _flatten(kb.get("findings"), "findings")
    credentials = _flatten(kb.get("credentials"), "credentials")
    if not targets:
        targets = _flatten(_sequence(metadata.get("active_targets"), "active_targets"), "active_targets", targets=True)
    target_host, target_url, target_notes = _target_summary(targets)
    transcript = extra.get("transcript")
    records = _sequence(transcript, "transcript") if transcript is not None else _legacy_records(_sequence(raw.get("messages"), "messages"))
    if any(not isinstance(record, dict) for record in records):
        raise SessionFormatError("transcript 含无效记录")
    timestamps = [_timestamp(record.get("timestamp")) for record in records]
    base_time = next((stamp for stamp in timestamps if stamp is not None), None)
    actions, nodes, edges = [], [], []
    stages = {}
    used_ids = {}

    def remember_stage(key, title=None, action_index=None):
        if not key:
            return
        stage = stages.setdefault(key, {"key": key, "title": title or key, "count": 0, "firstActionIndex": None})
        if action_index is not None:
            stage["count"] += 1
            if stage["firstActionIndex"] is None:
                stage["firstActionIndex"] = action_index

    for index, record in enumerate(records):
        _validate_text_fields(record, ("id", "kind", "role", "tool", "actor", "status", "title"))
        _mapping(record.get("data"), "event.data")
        kind = _label(record.get("kind")) or "event"
        role, tool = _label(record.get("role")), _label(record.get("tool"))
        category, color = KINDS.get(kind, ("事件", "#64748b"))
        text = _text(record.get("text"))
        title = _label(record.get("title"))
        if not title:
            if kind == "tool":
                title = f"工具 · {tool}" if tool else "工具记录"
            elif text:
                title = text.splitlines()[0][:100] or category
            else:
                title = category
        stage_key, stage_title = _explicit_stage(record)
        remember_stage(stage_key, stage_title, index)
        timestamp = timestamps[index]
        elapsed = timestamp - base_time if timestamp is not None and base_time is not None else None
        if elapsed is not None and not math.isfinite(elapsed):
            elapsed = None
        identity = _record_identity(record, index, used_ids)
        status = _label(record.get("status"))
        full_input, full_output = _text(record.get("input")), _text(record.get("output"))
        action = {
            "step": index, "id": identity, "cardId": identity,
            "sourceId": record.get("id"), "originalIndex": index,
            "kind": kind, "role": role, "title": title,
            "category": category, "categoryColor": color,
            "actor": _label(record.get("actor")), "tool": tool,
            "input": full_input, "output": full_output, "fullInput": full_input, "fullOutput": full_output,
            "text": text, "thought": text if role == "assistant" else None,
            "timestamp": timestamp, "timeSeconds": elapsed, "timeOffset": _time_offset(elapsed),
            "status": status, "stageKey": stage_key, "stageTitle": stage_title,
            "data": deepcopy(record),
        }
        actions.append(action)
        nodes.append({
            "id": identity, "type": "eventNode", "position": {"x": (index % 3) * 340, "y": (index // 3) * 160},
            "data": {"actionId": identity, "step": index, "title": title,
                     "subtitle": tool or role or kind, "status": status, "color": color},
        })
        if index:
            previous_id = nodes[index - 1]["id"]
            edges.append({"id": f"sequence-{previous_id}-{identity}", "source": previous_id, "target": identity,
                          "type": "smoothstep", "data": {"relation": "sequence"}})

    saved_stage = extra.get("stage")
    if saved_stage is not None:
        if isinstance(saved_stage, str):
            remember_stage(_label(saved_stage))
        elif isinstance(saved_stage, dict):
            for transition in _sequence(saved_stage.get("history"), "stage.history"):
                if not isinstance(transition, dict):
                    raise SessionFormatError("stage.history 含无效记录")
                remember_stage(_label(transition.get("from")))
                remember_stage(_label(transition.get("to")))
            remember_stage(_label(saved_stage.get("stage")) or _label(saved_stage.get("key")), _label(saved_stage.get("title")))
        else:
            raise SessionFormatError("stage 必须为对象或文本")
    summary = {
        "sessionId": raw.get("id"), "name": _label(raw.get("name")), "createdAt": _timestamp(raw.get("created_at")),
        "targetHost": target_host, "targetUrl": target_url, "targetNotes": target_notes,
        "totalActions": len(actions), "totalStages": len(stages),
        "targetsCount": len(targets), "findingsCount": len(findings), "verifiedFindingsCount": sum(_is_verified(f) for f in findings),
        "credsCount": len(credentials), "targets": targets, "findings": findings, "creds": credentials,
    }
    return {"nodes": nodes, "edges": edges, "actions": actions, "timeline": actions,
            "stages": list(stages.values()), "summary": summary}
