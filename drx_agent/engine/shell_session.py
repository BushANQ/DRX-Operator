"""Stateful pseudo-tty shell sessions.

Wraps a subprocess attached to a pseudo-terminal so interactive shells
(job control, prompt, sudo) behave like a user expects; sessions are
keyed by id in ShellSessionManager."""

from __future__ import annotations

import asyncio
import errno
import math
import os
import select
import signal
import subprocess
import time
import uuid

try:  # Pseudo-terminals are POSIX-only; importing the rest of the agent must
    import fcntl  # not fail on platforms without them.
    import pty
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    pty = None  # type: ignore[assignment]

from drx_agent.engine.process import check_cancelled, terminate_process_sync

class ShellSession:
    """A single persistent shell attached to a PTY."""

    def __init__(self, command: str, name: str = "", env: dict | None = None) -> None:
        check_cancelled()
        if pty is None or fcntl is None:
            raise RuntimeError(
                "persistent shell sessions require a POSIX pseudo-terminal"
            )
        self.session_id = f"sh-{uuid.uuid4().hex[:6]}"
        self.command = command
        self.name = name or self.session_id
        self.created_at = time.time()
        self.last_activity = time.time()
        self.closed = False
        self._closed_reason = ""

        master_fd, slave_fd = pty.openpty()
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.master_fd = master_fd

        spawn_env = os.environ.copy()
        spawn_env["TERM"] = spawn_env.get("TERM", "xterm-256color")
        if env:
            spawn_env.update(env)

        try:
            self.proc = subprocess.Popen(
                command,
                shell=True,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                env=spawn_env,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)

        self._pending: bytearray = bytearray()
        try:
            self._drain(self._pending, hard_timeout=0.5, idle_timeout=0.2)
        except BaseException:
            self.close(reason="startup interrupted")
            raise


    def _drain(
        self,
        buf: bytearray,
        hard_timeout: float = 5.0,
        idle_timeout: float = 0.3,
    ) -> None:
        if not math.isfinite(hard_timeout) or hard_timeout < 0:
            raise ValueError("shell timeout must be finite and nonnegative")
        if not math.isfinite(idle_timeout) or idle_timeout < 0:
            raise ValueError("shell idle timeout must be finite and nonnegative")
        deadline = time.monotonic() + hard_timeout
        last_data_at = time.monotonic()
        while True:
            check_cancelled()
            now = time.monotonic()
            if now >= deadline:
                break
            try:
                r, _, _ = select.select([self.master_fd], [], [], min(0.05, deadline - now))
            except (OSError, ValueError):
                break
            if r:
                try:
                    chunk = os.read(self.master_fd, 16384)
                except OSError as e:
                    if e.errno in (errno.EIO, errno.EBADF):
                        break
                    if e.errno == errno.EAGAIN:
                        continue
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                last_data_at = now
            else:
                if buf and (now - last_data_at) >= idle_timeout:
                    break


    def send(
        self,
        data: str,
        timeout: float = 10.0,
        idle_timeout: float = 0.4,
        append_newline: bool = True,
    ) -> str:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("shell timeout must be finite and nonnegative")
        if self.closed:
            output = self.peek(timeout=0)
            return output or f"(session closed: {self._closed_reason or 'manual'})"
        self.last_activity = time.time()
        deadline = time.monotonic() + timeout
        try:
            check_cancelled()
            if not self.is_alive():
                self._drain(self._pending, hard_timeout=timeout, idle_timeout=idle_timeout)
                self.close(reason=f"process exited (rc={self.proc.returncode})")
            else:
                if data:
                    payload = data.encode("utf-8", errors="replace")
                    if append_newline and not payload.endswith(b"\n"):
                        payload += b"\n"
                    offset = 0
                    while offset < len(payload):
                        check_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("shell input write timed out")
                        _, writable, _ = select.select([], [self.master_fd], [], min(0.05, remaining))
                        if writable:
                            try:
                                offset += os.write(self.master_fd, memoryview(payload)[offset:])
                            except BlockingIOError:
                                continue
                self._drain(self._pending, hard_timeout=max(0, deadline - time.monotonic()), idle_timeout=idle_timeout)
        except asyncio.CancelledError:
            self.close(reason="cancelled")
            raise
        except OSError as exc:
            return f"(write error: {exc})"
        output = self._pending.decode("utf-8", errors="replace")
        self._pending.clear()
        return output or (f"(session closed: {self._closed_reason})" if self.closed else "")

    def peek(self, timeout: float = 1.0) -> str:
        if not self.closed:
            try:
                self._drain(self._pending, hard_timeout=timeout, idle_timeout=timeout / 2)
            except asyncio.CancelledError:
                self.close(reason="cancelled")
                raise
        output = self._pending.decode("utf-8", errors="replace")
        self._pending.clear()
        return output

    def signal(self, sig_name: str = "SIGINT") -> None:
        """Send a signal to the foreground process group of the PTY."""
        if self.closed:
            return
        try:
            sig = getattr(signal, sig_name.upper(), signal.SIGINT)
            os.killpg(self.proc.pid, sig)
        except Exception:
            pass

    def close(self, reason: str = "manual") -> None:
        if self.closed:
            return
        self.closed = True
        self._closed_reason = reason
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        terminate_process_sync(self.proc)

    def is_alive(self) -> bool:
        if self.closed:
            return False
        return self.proc.poll() is None

    def info(self) -> dict:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "command": self.command,
            "alive": self.is_alive(),
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "closed_reason": self._closed_reason or None,
        }


class ShellSessionManager:
    """Pool of named shell sessions."""

    def __init__(self, max_sessions: int = 8) -> None:
        self.sessions: dict[str, ShellSession] = {}
        self.max_sessions = max_sessions

    def _reap_dead(self) -> None:
        for sid in list(self.sessions.keys()):
            sess = self.sessions[sid]
            if not sess.is_alive():
                sess.close(reason="process exited")
                del self.sessions[sid]

    def open(self, command: str, name: str = "") -> ShellSession:
        self._reap_dead()
        if len(self.sessions) >= self.max_sessions:
            raise RuntimeError(
                f"max_sessions ({self.max_sessions}) reached; close some first"
            )
        sess = ShellSession(command, name=name)
        self.sessions[sess.session_id] = sess
        return sess

    def get(self, session_id: str) -> ShellSession | None:
        return self.sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        sess = self.sessions.pop(session_id, None)
        if sess is None:
            return False
        sess.close(reason="manual")
        return True

    def list(self) -> list[dict]:
        return [s.info() for s in self.sessions.values()]

    def close_all(self) -> None:
        failures = []
        for sess in list(self.sessions.values()):
            try:
                sess.close(reason="shutdown")
            except Exception as exc:
                failures.append(f"{sess.session_id}: {exc}")
        self.sessions.clear()
        if failures:
            raise RuntimeError("Shell cleanup failed: " + "; ".join(failures))

