"""Crash dumps for unexpected audit-mode failures.

A top-level failure writes a self-contained log next to the process so an
interrupted audit can be diagnosed later, then the process exits with a
failure code instead of leaving a corrupted terminal state.
"""

from __future__ import annotations

import os
import platform
import sys
import time
import traceback


def _timestamp() -> tuple[str, str]:
    now = time.time()
    local = time.localtime(now)
    stamp = time.strftime("%Y%m%d-%H%M%S", local) + "-%09d" % int((now % 1) * 1e9)
    return stamp, time.strftime("%Y-%m-%dT%H:%M:%S", local) + (
        "%+.9f" % (now % 1)
    ).replace("+0", "+").replace("-0", "-")


def write_panic_dump(context: str, error: BaseException, *, log_dir: str = "logs") -> str:
    """Write one exclusive crash log and return its absolute path."""
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    stamp, rfc3339 = _timestamp()
    path = os.path.abspath(os.path.join(log_dir, f"panic-{stamp}.log"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fp:
            fp.write("Panic Log\n")
            fp.write(f"Time: {rfc3339}\n")
            fp.write(f"Context: {context}\n")
            fp.write(f"Binary: {sys.argv[0]}\n")
            fp.write(f"WorkingDir: {os.getcwd()}\n")
            fp.write(f"Args: {sys.argv}\n")
            fp.write(f"Python: {sys.version.splitlines()[0]}\n")
            fp.write(f"Platform: {platform.platform()}\n")
            fp.write(f"\nPanic: {type(error).__name__}: {error}\n\n")
            fp.write("Stack trace:\n")
            fp.write("".join(traceback.format_exception(type(error), error, error.__traceback__)))
            fp.write("\n")
    finally:
        if not fp.closed:  # pragma: no cover - defensive
            fp.close()
    return path


def recover_with_context(context: str, error: BaseException) -> str:
    """Write the dump, print its location, and return the path.

    The caller is responsible for the process exit code; returning the path
    keeps the behaviour testable without terminating the interpreter.
    """
    try:
        path = write_panic_dump(context, error)
    except OSError as exc:
        print(f"failed to write panic log: {exc}", file=sys.stderr)
        return ""
    print(f"panic logged to {path}, exiting...", file=sys.stderr)
    return path
