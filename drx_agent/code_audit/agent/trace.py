"""Incremental JSON-array trace files for one audit agent."""

from __future__ import annotations

import json
import os
import threading
from typing import Any

_TRACE_FOOTER = "]\n"


class TraceLog:
    """Append messages to a JSON array without rewriting the whole file."""

    def __init__(self, path: str, new_file: bool) -> None:
        self.path = path
        self.new_file = new_file
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def create(cls, directory: str) -> "TraceLog":
        directory = directory or "log_sessions"
        os.makedirs(directory, mode=0o700, exist_ok=True)
        from datetime import datetime

        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        for index in range(1000):
            name = base + ".json" if index == 0 else "%s-%03d.json" % (base, index)
            path = os.path.abspath(os.path.join(directory, name))
            try:
                handle = open(path, "x", encoding="utf-8")
            except FileExistsError:
                continue
            try:
                handle.write("[\n" + _TRACE_FOOTER)
            finally:
                handle.close()
            return cls(path, True)
        raise OSError("cannot create unique trace log in %s" % directory)

    @classmethod
    def resume(cls, path: str) -> "TraceLog":
        if not path:
            raise ValueError("trace path is empty")
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        if os.path.exists(path):
            return cls(path, False)
        handle = open(path, "x", encoding="utf-8")
        try:
            handle.write("[\n" + _TRACE_FOOTER)
        finally:
            handle.close()
        return cls(path, True)

    def append_message(self, message: Any) -> None:
        payload = message_to_dict(message)
        with self._lock:
            if self._closed:
                return
            with open(self.path, "r+b") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size < len(_TRACE_FOOTER):
                    raise ValueError("trace log footer missing: %s" % self.path)
                footer_at = size - len(_TRACE_FOOTER)
                handle.seek(footer_at)
                tail = handle.read(len(_TRACE_FOOTER))
                if tail != _TRACE_FOOTER:
                    raise ValueError("trace log footer invalid: %s" % self.path)
                prefix_len = min(64, footer_at)
                handle.seek(footer_at - prefix_len)
                prefix = handle.read(prefix_len)
                has_messages = not prefix.endswith("[\n")
                entry = "  " + json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ) + "\n" + _TRACE_FOOTER
                if has_messages:
                    entry = ",\n" + entry
                handle.seek(footer_at)
                from ..text import sanitize_text

                handle.write(sanitize_text(entry).encode("utf-8"))

    def close(self) -> None:
        self._closed = True


def message_to_dict(message: Any) -> dict[str, Any]:
    """Serialize a message carrying the fields the trace needs."""

    data: dict[str, Any] = {}
    for attr in ("role", "content", "type", "call_id", "name", "arguments", "id"):
        value = getattr(message, attr, None)
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        if attr == "role" and not value:
            continue
        data[attr] = value
    return data
