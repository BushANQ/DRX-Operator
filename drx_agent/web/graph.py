"""Evidence-preserving session projection helpers."""
import json
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
