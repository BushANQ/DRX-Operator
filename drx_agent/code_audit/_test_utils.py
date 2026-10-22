"""Shared scratch-directory helpers for the audit test suite."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import uuid

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_RUNTIME_ROOT = os.path.join(_PACKAGE_DIR, "_test_tmp_runtime")
_created: list[str] = []


def _probe(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-probe")
        with open(probe, "w", encoding="utf-8") as fp:
            fp.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _cleanup() -> None:
    for path in _created:
        shutil.rmtree(path, ignore_errors=True)
    _created.clear()
    try:
        if os.path.isdir(_RUNTIME_ROOT) and not os.listdir(_RUNTIME_ROOT):
            os.rmdir(_RUNTIME_ROOT)
    except OSError:
        pass


atexit.register(_cleanup)


def writable_temp_dir(prefix: str = "drx-audit-") -> str:
    """Create a scratch directory that the current environment can write to.

    Sandboxes on some platforms deny writes inside freshly created private
    temp directories (mode 0700).  Try the system temp area first, then fall
    back to a repository-local runtime directory created with os.makedirs.
    """
    token = f"{prefix}{os.getpid()}-{uuid.uuid4().hex[:8]}"
    candidates = [
        os.path.join(tempfile.gettempdir(), "drx-audit-tests", token),
        os.path.join(_RUNTIME_ROOT, token),
    ]
    for candidate in candidates:
        if _probe(candidate):
            _created.append(candidate)
            return candidate
    raise OSError("no writable temporary directory available for tests")
