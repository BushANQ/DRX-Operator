"""Tool-result buffer: bounded pages, byte-exact paging and isolation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid

import pytest

from drx_agent.code_audit.tools import Registry


def _new_base() -> str:
    """Unique per process and per test so parallel pytest runs cannot collide."""
    root = os.path.join(tempfile.gettempdir(), "drx-audit-tests")
    os.makedirs(root, exist_ok=True)
    base = os.path.join(root, "%d-%s" % (os.getpid(), uuid.uuid4().hex))
    os.makedirs(base)
    return base


def _remove_base(base: str) -> None:
    def handle(func, target, error):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(base, onexc=handle)
    except TypeError:  # pragma: no cover - Python < 3.12
        shutil.rmtree(base, onerror=lambda func, target, info: handle(func, target, info[1]))
    try:
        os.rmdir(os.path.dirname(base))
    except OSError:
        pass


def make_registry(max_chars: int = 1024) -> tuple[Registry, str]:
    workspace = _new_base()
    with open(os.path.join(workspace, "big.txt"), "w", encoding="utf-8", newline="") as handle:
        for index in range(300):
            handle.write("line %03d 中文🙂\"\\<>&\n" % index)
    return Registry(workspace, max_chars), workspace


def cleanup(workspace: str) -> None:
    _remove_base(workspace)


def test_buffer_serialized_budget_and_utf8_round_trip():
    registry, workspace = make_registry()
    try:
        bounded, full = registry.call_with_full_result(None, "read_file", {"path": "big.txt", "limit": 300})
        assert len(full.encode("utf-8")) > 1024
        assert len(bounded.encode("utf-8")) <= 1024
        page = json.loads(bounded)
        assert page["truncated"] is True
        assert page["buffer_id"]
        assert page["ok"] is True
        assert page["tool"] == "read_file"
        rebuilt = page["preview"]
        buffer_id = page["buffer_id"]
        while not page["eof"]:
            offset = page["next_offset"]
            args = {"buffer_id": buffer_id, "offset": offset, "limit": 127}
            chunk = registry.call("read_tool_buffer", args)
            assert len(chunk.encode("utf-8")) <= 1024
            page = json.loads(chunk)
            assert page["ok"] is True
            assert page["next_offset"] > offset
            rebuilt += page["content"]
        assert rebuilt == full
        negative = registry.call("read_tool_buffer", {"buffer_id": buffer_id, "offset": -1})
        assert json.loads(negative)["ok"] is False
    finally:
        cleanup(workspace)


def test_buffer_latest_only_worker_isolation_and_invalidation():
    registry, workspace = make_registry()
    try:
        _, full = registry.call_with_full_result(None, "read_file", {"path": "big.txt", "limit": 300})
        first = json.loads(registry.call_with_full_result(None, "read_file", {"path": "big.txt"})[0])
        args = {"buffer_id": first["buffer_id"]}
        fork = registry.fork()
        try:
            assert json.loads(fork.call("read_tool_buffer", args))["ok"] is False
        finally:
            fork.close()
        registry.call("todo_create", {"title": "小结果"})
        assert json.loads(registry.call("read_tool_buffer", args))["ok"] is False

        first = json.loads(registry.call_with_full_result(None, "read_file", {"path": "big.txt"})[0])
        args = {"buffer_id": first["buffer_id"]}
        registry.call("unknown_tool", {})
        assert json.loads(registry.call("read_tool_buffer", args))["ok"] is False

        first = json.loads(registry.call_with_full_result(None, "read_file", {"path": "big.txt"})[0])
        args = {"buffer_id": first["buffer_id"]}
        replacement = json.loads(
            registry.call_with_full_result(None, "read_file", {"path": "big.txt", "limit": 200})[0]
        )
        assert json.loads(registry.call("read_tool_buffer", args))["ok"] is False
        registry.close()
        assert (
            json.loads(registry.call("read_tool_buffer", {"buffer_id": replacement["buffer_id"]}))["ok"]
            is False
        )
    finally:
        cleanup(workspace)


def test_buffer_rejects_mid_rune_offset():
    registry, workspace = make_registry()
    try:
        bounded, full = registry.call_with_full_result(
            None, "read_file", {"path": "big.txt"}
        )
        page = json.loads(bounded)
        buffer_id = page["buffer_id"]
        data = full.encode("utf-8")
        rune_offset = data.index("中".encode("utf-8"))
        split = rune_offset + 1
        rejected = json.loads(
            registry.call("read_tool_buffer", {"buffer_id": buffer_id, "offset": split, "limit": 1})
        )
        assert rejected["ok"] is False
        allowed = json.loads(
            registry.call(
                "read_tool_buffer",
                {"buffer_id": buffer_id, "offset": rune_offset, "limit": 3},
            )
        )
        assert allowed["ok"] is True
        assert allowed["content"] == "中"
        assert allowed["next_offset"] == rune_offset + 3
    finally:
        cleanup(workspace)


def test_buffer_error_strings():
    registry, workspace = make_registry()
    try:
        page = json.loads(registry.call_with_full_result(None, "read_file", {"path": "big.txt"})[0])
        buffer_id = page["buffer_id"]
        wrong = json.loads(registry.call("read_tool_buffer", {"buffer_id": "buffer-wrong"}))
        assert "失效" in wrong["error"]
        negative = json.loads(
            registry.call("read_tool_buffer", {"buffer_id": buffer_id, "limit": -5})
        )
        assert "不能为负数" in negative["error"]
        beyond = json.loads(
            registry.call("read_tool_buffer", {"buffer_id": buffer_id, "offset": 10 ** 9})
        )
        assert "超过 buffer 长度" in beyond["error"]
        invalid = registry.call("read_tool_buffer", "not-json")
        assert json.loads(invalid)["ok"] is False
    finally:
        cleanup(workspace)

