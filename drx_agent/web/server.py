"""FastAPI backend for DRX-Operator Web Dashboard.

Exposes REST endpoints for session listing/loading and a WebSocket for
live event streaming. The frontend uses these to drive the React Flow
replay graph.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from drx_agent.web.graph import SessionFormatError, session_to_graph

logger = logging.getLogger(__name__)


def create_app(drx_agent=None, session_dir: str | None = None) -> FastAPI:
    """Build the FastAPI application.

    Parameters
    ----------
    drx_agent:
        An optional live ``DrxAgent`` controller for real-time monitoring.
    session_dir:
        Path to the ``sessions/`` directory containing ``sessions.db``.
    """
    app = FastAPI(title="DRX-Operator Dashboard", version="0.1.0")

    # CORS — allow the Vite dev server during development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ state
    active_websockets: list[WebSocket] = []

    if session_dir is None:
        session_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "sessions")
        )

    def _get_session_store():
        from drx_agent.session.store import SessionStore
        try:
            return SessionStore(session_dir)
        except (sqlite3.Error, OSError) as error:
            raise HTTPException(status_code=500, detail="会话存储暂不可用") from error

    def _load_snapshot(session_id):
        try:
            return _get_session_store().load_session(session_id)
        except (ValueError, FileNotFoundError, KeyError, TypeError) as error:
            raise HTTPException(status_code=422, detail="会话记录无法解析") from error
        except (sqlite3.Error, OSError) as error:
            raise HTTPException(status_code=500, detail="会话存储暂不可用") from error

    # -------------------------------------------------------------- REST API

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "timestamp": time.time()}

    @app.get("/api/sessions")
    async def list_sessions():
        """Return all saved sessions, newest first."""
        store = _get_session_store()
        try:
            sessions = store.list_sessions()
        except (sqlite3.Error, OSError) as error:
            raise HTTPException(status_code=500, detail="会话存储暂不可用") from error
        return JSONResponse(content=sessions)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        """Load a full session snapshot and transform it into a React Flow
        compatible graph structure for replay."""
        raw = _load_snapshot(session_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="Session not found")

        try:
            graph = _session_to_graph(raw)
        except SessionFormatError as error:
            raise HTTPException(status_code=422, detail="会话记录无法解析") from error
        return JSONResponse(content=graph)

    @app.get("/api/sessions/{session_id}/timeline")
    async def get_session_timeline(session_id: str):
        """Return the timeline events for step-by-step replay."""
        raw = _load_snapshot(session_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="Session not found")

        try:
            timeline = _session_to_timeline(raw)
        except SessionFormatError as error:
            raise HTTPException(status_code=422, detail="会话记录无法解析") from error
        return JSONResponse(content=timeline)

    # ------------------------------------------------------------ WebSocket

    @app.websocket("/ws/events")
    async def websocket_events(ws: WebSocket):
        """Stream live EventBus events to connected dashboards."""
        await ws.accept()
        active_websockets.append(ws)
        try:
            # If we have a live agent, subscribe to its event bus
            if drx_agent is not None:
                from drx_agent.event_bus import EventType

                queue: asyncio.Queue = asyncio.Queue(maxsize=512)

                def _forward(event):
                    try:
                        payload = {
                            "type": event.type.value,
                            "data": event.data,
                            "timestamp": event.timestamp,
                        }
                        queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        pass

                for et in EventType:
                    drx_agent.event_bus.subscribe(et, _forward)

                try:
                    while True:
                        payload = await queue.get()
                        await ws.send_json(payload)
                except WebSocketDisconnect:
                    pass
                finally:
                    for et in EventType:
                        drx_agent.event_bus.unsubscribe(et, _forward)
            else:
                # No live agent — just keep alive and wait for disconnect
                while True:
                    await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if ws in active_websockets:
                active_websockets.remove(ws)

    # ------------------------------------------------------ static frontend

    frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
    if os.path.isdir(frontend_dist):
        app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            # Try exact file first, then SPA fallback
            file_path = os.path.join(frontend_dist, full_path)
            if full_path and os.path.isfile(file_path):
                return FileResponse(file_path)
            return FileResponse(os.path.join(frontend_dist, "index.html"))

    return app


def _session_to_graph(raw: dict) -> dict:
    return session_to_graph(raw)


def _session_to_timeline(raw: dict) -> list[dict]:
    return session_to_graph(raw)["actions"]
