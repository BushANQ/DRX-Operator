"""Tests for crash dump creation."""

from __future__ import annotations

import os

import pytest

from drx_agent.code_audit import paniclog


def test_write_panic_dump_contains_diagnostics(tmp_path):
    log_dir = str(tmp_path / "logs")
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        path = paniclog.write_panic_dump("audit", exc, log_dir=log_dir)
    assert os.path.dirname(path) == os.path.abspath(log_dir)
    assert os.path.basename(path).startswith("panic-")
    content = open(path, encoding="utf-8").read()
    assert "Panic Log" in content
    assert "Context: audit" in content
    assert "RuntimeError: boom" in content
    assert "Stack trace" in content
    assert "WorkingDir:" in content
    assert "Python:" in content
    if os.name != "nt":
        assert (os.stat(path).st_mode & 0o777) == 0o600


def test_panic_dumps_are_unique(tmp_path):
    log_dir = str(tmp_path / "logs")
    paths = set()
    for index in range(3):
        try:
            raise ValueError(f"boom-{index}")
        except ValueError as exc:
            paths.add(paniclog.write_panic_dump("main", exc, log_dir=log_dir))
    assert len(paths) == 3


def test_recover_with_context_prints_location(tmp_path, capsys):
    try:
        raise KeyError("missing")
    except KeyError as exc:
        path = paniclog.recover_with_context("main", exc)
    captured = capsys.readouterr()
    assert "panic logged to" in captured.err
    assert path
    os.remove(path)
