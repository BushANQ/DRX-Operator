"""Evidence-preserving session projection helpers."""
import json
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
