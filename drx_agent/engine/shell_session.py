"""Stateful pseudo-tty shell sessions.

Wraps a subprocess attached to a pseudo-terminal so interactive shells
(job control, prompt, sudo) behave like a user expects; sessions are
keyed by id in ShellSessionManager."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import subprocess
import time
import uuid


class ShellSession:
    """A single persistent shell attached to a PTY."""

    def __init__(self, command: str, name: str = "", env: dict | None = None) -> None:
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
                preexec_fn=os.setsid,
                env=spawn_env,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)

        self._pending: bytearray = bytearray()
        self._drain(self._pending, hard_timeout=0.5, idle_timeout=0.2)


    def _drain(
        self,
        buf: bytearray,
        hard_timeout: float = 5.0,
        idle_timeout: float = 0.3,
    ) -> None:
        
        deadline = time.time() + hard_timeout
        last_data_at = time.time()
        while True:
            now = time.time()
            if now >= deadline:
                break
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.05)
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
        if self.closed:
            return f"(session closed: {self._closed_reason or 'manual'})"
        if not self.is_alive():
            self._closed_reason = f"process exited (rc={self.proc.returncode})"
            self.closed = True
            return f"(session closed: {self._closed_reason})"

        self.last_activity = time.time()
        if data:
            payload = data.encode("utf-8", errors="replace")
            if append_newline and not payload.endswith(b"\n"):
                payload += b"\n"
            try:
                os.write(self.master_fd, payload)
            except OSError as e:
                return f"(write error: {e})"

        buf = bytearray(self._pending)
        self._pending.clear()
        self._drain(buf, hard_timeout=timeout, idle_timeout=idle_timeout)
        return buf.decode("utf-8", errors="replace")

    def peek(self, timeout: float = 1.0) -> str:
        if self.closed:
            return ""
        buf = bytearray(self._pending)
        self._pending.clear()
        self._drain(buf, hard_timeout=timeout, idle_timeout=timeout / 2)
        return buf.decode("utf-8", errors="replace")

    def signal(self, sig_name: str = "SIGINT") -> None:
        """Send a signal to the foreground process group of the PTY."""
        if self.closed:
            return
        try:
            sig = getattr(signal, sig_name.upper(), signal.SIGINT)
            pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, sig)
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
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

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
        for sess in list(self.sessions.values()):
            sess.close(reason="shutdown")
        self.sessions.clear()

