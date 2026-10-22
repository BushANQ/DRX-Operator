"""Single-slot tool-result buffer with byte-exact UTF-8 paging."""

from __future__ import annotations

import itertools
import json
import secrets
import threading

PROCESS_ID = secrets.token_hex(16)
_BUFFER_SEQ = itertools.count(1)
_SEQ_LOCK = threading.Lock()


def buffer_error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False, separators=(",", ":"))


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def utf8_prefix(data: bytes, n: int) -> int:
    """Back a byte index up to the nearest UTF-8 character boundary."""
    if n >= len(data):
        return len(data)
    if n < 0:
        n = 0
    while n > 0 and (data[n] & 0xC0) == 0x80:
        n -= 1
    return n


def fit_buffer_page(page: dict, content: str, budget: int, preview: bool) -> str:
    data = content.encode("utf-8")
    n = len(data)
    if n > budget // 2:
        n = utf8_prefix(data, budget // 2)
    while True:
        page["next_offset"] = page["offset"] + n
        page["eof"] = page["next_offset"] == page["total_bytes"]
        chunk = data[:n].decode("utf-8", errors="ignore")
        if preview:
            page["preview"] = chunk
            page.pop("content", None)
        else:
            page["content"] = chunk
            page.pop("preview", None)
        encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
        encoded_bytes = _byte_len(encoded)
        if encoded_bytes <= budget:
            return encoded
        if n == 0:
            return buffer_error("工具 buffer 元数据超过返回上限")
        next_size = n - (encoded_bytes - budget)
        if next_size < 0:
            next_size = n // 2
        n = utf8_prefix(data, next_size)


class ResultBuffer:
    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._id = ""
        self._content = ""

    def clear(self) -> None:
        with self._lock:
            self._id = ""
            self._content = ""

    def bound_result(self, tool: str, full: str) -> str:
        with self._lock:
            self._id = ""
            self._content = ""
            if _byte_len(full) <= self.max_chars:
                return full
            with _SEQ_LOCK:
                sequence = next(_BUFFER_SEQ)
            buffer_id = "buffer-%s-%d" % (PROCESS_ID, sequence)
            self._id = buffer_id
            self._content = full
        try:
            parsed = json.loads(full)
            ok = bool(parsed.get("ok")) if isinstance(parsed, dict) else False
        except (ValueError, TypeError):
            ok = False
        page = {
            "ok": ok,
            "truncated": True,
            "buffer_id": buffer_id,
            "tool": tool,
            "total_bytes": _byte_len(full),
            "offset": 0,
            "next_offset": 0,
            "eof": False,
        }
        return fit_buffer_page(page, full, self.max_chars, True)

    def read_tool_buffer(self, args: dict) -> str:
        buffer_id = args.get("buffer_id", "")
        offset = args.get("offset", 0)
        limit = args.get("limit", 0)
        if not isinstance(buffer_id, str):
            return buffer_error("无效的 buffer 参数")
        if isinstance(offset, bool) or not isinstance(offset, int):
            return buffer_error("无效的 buffer 参数")
        if isinstance(limit, bool) or not isinstance(limit, int):
            return buffer_error("无效的 buffer 参数")
        if offset < 0 or limit < 0:
            return buffer_error("offset 和 limit 不能为负数")
        if limit == 0 or limit > self.max_chars:
            limit = self.max_chars
        if limit < 4:
            limit = 4
        with self._lock:
            if not self._id or buffer_id != self._id:
                return buffer_error(
                    "buffer 已失效或属于另一个 Agent；调用非 read_tool_buffer 工具即清除旧 buffer，"
                    "请重新调用原工具"
                )
            content = self._content
            data = content.encode("utf-8")
            total = len(data)
            if offset > total:
                return buffer_error("offset 超过 buffer 长度")
            tail = data[offset:]
            if tail and (tail[0] & 0xC0) == 0x80:
                return buffer_error("offset 必须是 UTF-8 字符边界；请使用上次返回的 next_offset")
            end = utf8_prefix(tail, limit)
            chunk = tail[:end].decode("utf-8", errors="ignore")
            page = {
                "ok": True,
                "buffer_id": buffer_id,
                "offset": offset,
                "total_bytes": total,
                "next_offset": offset,
                "eof": False,
            }
            return fit_buffer_page(page, chunk, self.max_chars, False)
