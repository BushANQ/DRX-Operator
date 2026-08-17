"""Local HTTP callback listener for confirming blind/out-of-band exploits.

Runs a small HTTP server on a local interface that records every request
(path, method, headers, source IP, body, ts). Each start() returns a unique
/cb/<token>/ URL prefix; tunnel externally (ngrok/cloudflared) if needed."""

from __future__ import annotations

import http.server
import logging
import socket
import socketserver
import threading
import time
import uuid

logger = logging.getLogger(__name__)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_instance: "OOBListener | None" = None

    def log_message(self, fmt, *args):
        return

    def _serve(self) -> None:
        listener = self.server_instance
        if listener is None:
            self.send_response(500)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = b""
        if length > 0:
            try:
                body = self.rfile.read(min(length, 64 * 1024))
            except Exception:
                body = b""

        listener._record(
            method=self.command,
            path=self.path,
            headers=dict(self.headers.items()),
            client_addr=self.client_address[0] if self.client_address else "",
            body=body,
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        try:
            self.wfile.write(b"ok")
        except Exception:
            pass

    def do_GET(self):     self._serve()
    def do_POST(self):    self._serve()
    def do_PUT(self):     self._serve()
    def do_DELETE(self):  self._serve()
    def do_HEAD(self):    self._serve()
    def do_OPTIONS(self): self._serve()
    def do_PATCH(self):   self._serve()


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class OOBListener:
    """Singleton-ish callback listener. Use start() / stop() / interactions()."""

    def __init__(self) -> None:
        self._server: _ThreadedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._interactions: list[dict] = []
        self._lock = threading.Lock()
        self._token: str | None = None
        self._bind_host = "127.0.0.1"
        self._bind_port: int | None = None
        self._max_records = 1000


    @property
    def running(self) -> bool:
        return self._server is not None

    def start(
        self,
        port: int | None = None,
        bind: str = "0.0.0.0",
        token: str | None = None,
    ) -> dict:
        if self.running:
            return self.info()

        self._bind_host = bind
        self._token = token or uuid.uuid4().hex[:8]

        if port is None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((bind, 0))
                port = s.getsockname()[1]
        self._bind_port = port

        handler_cls = type(
            "_BoundHandler",
            (_CallbackHandler,),
            {"server_instance": self},
        )
        try:
            self._server = _ThreadedHTTPServer((bind, port), handler_cls)
        except OSError as e:
            raise RuntimeError(f"could not bind {bind}:{port}: {e}") from e

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"OOBListener-{port}",
            daemon=True,
        )
        self._thread.start()
        return self.info()

    def stop(self) -> None:
        srv = self._server
        if srv is None:
            return
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        self._server = None
        self._thread = None


    def _record(
        self,
        method: str,
        path: str,
        headers: dict,
        client_addr: str,
        body: bytes,
    ) -> None:
        # Requests outside /cb/<token>/ are still recorded (scanners probing /);
        # token_match tells the agent whether this is "their" hit.
        token_match = False
        if self._token and path.startswith(f"/cb/{self._token}"):
            token_match = True
        try:
            body_text = body.decode("utf-8", errors="replace")
        except Exception:
            body_text = repr(body[:512])
        record = {
            "ts": time.time(),
            "method": method,
            "path": path,
            "client": client_addr,
            "headers": headers,
            "body": body_text[:4000],
            "body_length": len(body),
            "token_match": token_match,
        }
        with self._lock:
            self._interactions.append(record)
            if len(self._interactions) > self._max_records:
                self._interactions = self._interactions[-self._max_records :]


    def info(self) -> dict:
        host = self._bind_host
        if host in ("0.0.0.0", "::"):
            host = self._best_local_ip()
        base = f"http://{host}:{self._bind_port}" if self._bind_port else None
        callback_url = (
            f"{base}/cb/{self._token}/" if base and self._token else None
        )
        return {
            "running": self.running,
            "bind": f"{self._bind_host}:{self._bind_port}" if self._bind_port else None,
            "base_url": base,
            "callback_url": callback_url,
            "token": self._token,
            "interactions": len(self._interactions),
        }

    def interactions(
        self,
        since_ts: float = 0.0,
        token_only: bool = False,
        last_n: int | None = None,
    ) -> list[dict]:
        with self._lock:
            data = list(self._interactions)
        if since_ts > 0:
            data = [r for r in data if r["ts"] > since_ts]
        if token_only:
            data = [r for r in data if r.get("token_match")]
        if last_n is not None and last_n > 0:
            data = data[-last_n:]
        return data


    @staticmethod
    def _best_local_ip() -> str:
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

