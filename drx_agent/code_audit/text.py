"""Text hygiene helpers.

Model streams can split a UTF-16 surrogate pair across two SSE deltas; each
frame then decodes to a lone surrogate code point.  Lone surrogates cannot be
encoded as UTF-8, so they must be removed before anything is measured, logged,
persisted or written to a terminal stream.
"""

from __future__ import annotations

import json
from typing import Any

REPLACEMENT = "\ufffd"


def sanitize_text(value: Any) -> str:
    """Return *value* as a string with lone surrogates replaced."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value
    return "".join(
        REPLACEMENT if 0xD800 <= ord(char) <= 0xDFFF else char for char in value
    )


def sanitize_data(value: Any) -> Any:
    """Recursively sanitize strings inside JSON-like data."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_data(key): sanitize_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_data(item) for item in value)
    return value


def utf8_len(value: Any) -> int:
    return len(sanitize_text(value).encode("utf-8"))


def compact_json(value: Any) -> str:
    return sanitize_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )
