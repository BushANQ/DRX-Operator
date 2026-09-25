"""FastAPI backend for DRX-Operator Web Dashboard.

Exposes REST endpoints for session listing/loading and a WebSocket for
live event streaming. The frontend uses these to drive the React Flow
replay graph.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from drx_agent.web.graph import _target_summary

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
        return SessionStore(session_dir)

    # -------------------------------------------------------------- REST API

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "timestamp": time.time()}

    @app.get("/api/sessions")
    async def list_sessions():
        """Return all saved sessions, newest first."""
        store = _get_session_store()
        sessions = store.list_sessions()
        return JSONResponse(content=sessions)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        """Load a full session snapshot and transform it into a React Flow
        compatible graph structure for replay."""
        store = _get_session_store()
        raw = store.load_session(session_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="Session not found")

        graph = _session_to_graph(raw)
        return JSONResponse(content=graph)

    @app.get("/api/sessions/{session_id}/timeline")
    async def get_session_timeline(session_id: str):
        """Return the timeline events for step-by-step replay."""
        store = _get_session_store()
        raw = store.load_session(session_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="Session not found")

        timeline = _session_to_timeline(raw)
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


# ================================================================ transforms

def _format_time_offset(seconds: int) -> str:
    """Format seconds into T+Xs or T+XmYs string."""
    if seconds < 60:
        return f"T+{max(0, seconds)}s"
    m = seconds // 60
    s = seconds % 60
    return f"T+{m}m{s:02d}s"


# 9 Standard Penetration Testing Stages matching the executive replay dashboard
STANDARD_STAGES = [
    {"key": "入队", "title": "第 1 阶段 · 入队", "tag": "准备租户隔离与权限策略", "cardId": "session-root", "phase": "init"},
    {"key": "建模", "title": "第 2 阶段 · 建模", "tag": "环境自检与资产边界建模", "cardId": "card-page", "phase": "recon"},
    {"key": "引擎", "title": "第 3 阶段 · 引擎", "tag": "认知安全引擎建立会话", "cardId": "card-page", "phase": "recon"},
    {"key": "推理", "title": "第 4 阶段 · 推理", "tag": "影响边界 战役推进中", "cardId": "card-surface", "phase": "recon"},
    {"key": "深推", "title": "第 5 阶段 · 深推", "tag": "多轮假设推演与误报挑战", "cardId": "card-hypo", "phase": "reasoning"},
    {"key": "验证", "title": "第 6 阶段 · 验证", "tag": "证据复核与PoC探测", "cardId": "card-fp", "phase": "reasoning"},
    {"key": "利用链", "title": "第 7 阶段 · 利用链", "tag": "靶机利用验证与鉴权绕过", "cardId": "card-vuln", "phase": "exploit"},
    {"key": "情报", "title": "第 8 阶段 · 情报", "tag": "凭据提取与权限突破", "cardId": "card-priv", "phase": "exploit"},
    {"key": "入库", "title": "第 9 阶段 · 入库", "tag": "研判报告生成与战果归档", "cardId": "card-evidence", "phase": "intel"},
]


def _session_to_graph(raw: dict) -> dict:
    """Transform a session snapshot into a horizontal phase-grouped React Flow
    graph with action stream, 9-stage milestone track, and rich metadata."""
    metadata = raw.get("metadata", {})
    extra = metadata.get("extra", {})
    transcript_records = extra.get("transcript") or []
    messages = raw.get("messages", [])
    kb_data = raw.get("kb_data", {})

    # Extract target host
    raw_targets = kb_data.get("targets", {})
    targets_list = list(raw_targets.values()) if isinstance(raw_targets, dict) else list(raw_targets)
    raw_findings = kb_data.get("findings", {})
    def records_by_host(value):
        if not isinstance(value, dict):
            return list(value)
        return [dict(record, host=record.get("host") or host)
                for host, bucket in value.items()
                for record in (bucket if isinstance(bucket, list) else [bucket])]

    findings_list = records_by_host(raw_findings)
    raw_creds = kb_data.get("credentials", {})
    creds_list = records_by_host(raw_creds)

    if not targets_list:
        targets_list = [dict(item) if isinstance(item, dict) else {"host": item}
                        for item in metadata.get("active_targets", [])]
    else:
        targets_list = [dict(item) if isinstance(item, dict) else {"host": item}
                        for item in targets_list]
        if isinstance(raw_targets, dict):
            for item, host in zip(targets_list, raw_targets):
                item.setdefault("host", host)
    target_host, target_url, target_notes = _target_summary(targets_list)

    session_name = raw.get("name") or raw.get("id") or "研判回放"

    # -------------------------------------------------- 1. Horizontal Graph Nodes
    # Left-to-right flow with phase containers and process cards
    nodes = [
        # Session Root Card
        {
            "id": "session-root",
            "type": "sessionCardNode",
            "position": {"x": 40, "y": 90},
            "data": {
                "badge": "SESSION",
                "label": "研判会话",
                "target": target_url,
                "sub": target_notes,
                "color": "#38bdf8",
                "phase": "init",
                "cardId": "session-root",
            },
        },
        # Phase 1 Group: 感知测绘
        {
            "id": "group-recon",
            "type": "phaseGroupNode",
            "position": {"x": 340, "y": 40},
            "style": {"width": 540, "height": 190},
            "data": {
                "title": "感知测绘 · 攻击面与页面取证",
                "color": "#06b6d4",
                "phase": "recon",
            },
        },
        {
            "id": "card-page",
            "type": "processCardNode",
            "position": {"x": 360, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "页面取证",
                "subtitle": "抓取页面与端点线索",
                "color": "#06b6d4",
                "cardId": "card-page",
                "phase": "recon",
                "summary": "访问首页与核心路由，解析前端脚本并抽取接口候选",
            },
        },
        {
            "id": "card-surface",
            "type": "processCardNode",
            "position": {"x": 620, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "攻击面测绘",
                "subtitle": "暴露面与入口发现",
                "color": "#06b6d4",
                "cardId": "card-surface",
                "phase": "recon",
                "summary": "发现管理后台入口、健康探针与参数交互暴露面",
            },
        },
        # Phase 2 Group: 深度推理
        {
            "id": "group-reasoning",
            "type": "phaseGroupNode",
            "position": {"x": 940, "y": 40},
            "style": {"width": 540, "height": 190},
            "data": {
                "title": "深度推理 · 假设推演与误报挑战",
                "color": "#a855f7",
                "phase": "reasoning",
            },
        },
        {
            "id": "card-hypo",
            "type": "processCardNode",
            "position": {"x": 960, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "假设推演",
                "subtitle": "多轮假设推演",
                "color": "#a855f7",
                "cardId": "card-hypo",
                "phase": "reasoning",
                "summary": "建立攻击假设集：未授权访问 / 越权篡改 / 注入候选",
            },
        },
        {
            "id": "card-fp",
            "type": "processCardNode",
            "position": {"x": 1220, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "误报挑战",
                "subtitle": "证据复核与降噪",
                "color": "#a855f7",
                "cardId": "card-fp",
                "phase": "reasoning",
                "summary": "基于安全响应头与限流规则，排除无效路径，降噪收敛",
            },
        },
        # Phase 3 Group: 利用验证
        {
            "id": "group-exploit",
            "type": "phaseGroupNode",
            "position": {"x": 1540, "y": 40},
            "style": {"width": 540, "height": 190},
            "data": {
                "title": "利用验证 · 漏洞打通与验证",
                "color": "#f43f5e",
                "phase": "exploit",
            },
        },
        {
            "id": "card-vuln",
            "type": "processCardNode",
            "position": {"x": 1560, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "漏洞验证",
                "subtitle": "接口鉴权与参数篡改",
                "color": "#f43f5e",
                "cardId": "card-vuln",
                "phase": "exploit",
                "summary": "执行漏洞 PoC 验证，测试越权与管理端鉴权防护边界",
            },
        },
        {
            "id": "card-priv",
            "type": "processCardNode",
            "position": {"x": 1820, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "权限突破",
                "subtitle": "靶机凭据与利用链闭环",
                "color": "#f43f5e",
                "cardId": "card-priv",
                "phase": "exploit",
                "summary": "捕获靶机凭据与关键配置，完成攻击链条验证闭环",
            },
        },
        # Phase 4 Group: 成果入库
        {
            "id": "group-intel",
            "type": "phaseGroupNode",
            "position": {"x": 2140, "y": 40},
            "style": {"width": 540, "height": 190},
            "data": {
                "title": "成果入库 · 研判报告与战役归档",
                "color": "#10b981",
                "phase": "intel",
            },
        },
        {
            "id": "card-evidence",
            "type": "processCardNode",
            "position": {"x": 2160, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "证据固化",
                "subtitle": "全量攻击轨迹与证据链",
                "color": "#10b981",
                "cardId": "card-evidence",
                "phase": "intel",
                "summary": "固化漏洞证明数据包、影响范围与证据链条",
            },
        },
        {
            "id": "card-report",
            "type": "processCardNode",
            "position": {"x": 2420, "y": 90},
            "data": {
                "badge": "PROCESS",
                "title": "研判报告",
                "subtitle": "自动汇总输出战役报告",
                "color": "#10b981",
                "cardId": "card-report",
                "phase": "intel",
                "summary": "自动生成多维安全研判报告，完成战役归档",
            },
        },
    ]

    # -------------------------------------------------- 2. Edges
    edges = [
        {
            "id": "e-root-page",
            "source": "session-root",
            "target": "card-page",
            "type": "smoothstep",
            "style": {"stroke": "#06b6d4", "strokeWidth": 2},
        },
        {
            "id": "e-page-surface",
            "source": "card-page",
            "target": "card-surface",
            "type": "smoothstep",
            "style": {"stroke": "#06b6d4", "strokeWidth": 2},
        },
        {
            "id": "e-surface-hypo",
            "source": "card-surface",
            "target": "card-hypo",
            "type": "smoothstep",
            "animated": True,
            "style": {"stroke": "#a855f7", "strokeWidth": 2, "strokeDasharray": "5,5"},
        },
        {
            "id": "e-hypo-fp",
            "source": "card-hypo",
            "target": "card-fp",
            "type": "smoothstep",
            "style": {"stroke": "#a855f7", "strokeWidth": 2},
        },
        {
            "id": "e-fp-vuln",
            "source": "card-fp",
            "target": "card-vuln",
            "type": "smoothstep",
            "animated": True,
            "style": {"stroke": "#f43f5e", "strokeWidth": 2, "strokeDasharray": "5,5"},
        },
        {
            "id": "e-vuln-priv",
            "source": "card-vuln",
            "target": "card-priv",
            "type": "smoothstep",
            "style": {"stroke": "#f43f5e", "strokeWidth": 2},
        },
        {
            "id": "e-priv-evidence",
            "source": "card-priv",
            "target": "card-evidence",
            "type": "smoothstep",
            "animated": True,
            "style": {"stroke": "#10b981", "strokeWidth": 2, "strokeDasharray": "5,5"},
        },
        {
            "id": "e-evidence-report",
            "source": "card-evidence",
            "target": "card-report",
            "type": "smoothstep",
            "style": {"stroke": "#10b981", "strokeWidth": 2},
        },
    ]

    # -------------------------------------------------- 3. Extract Action Stream
    raw_actions = []
    base_time = transcript_records[0].get("timestamp", 0) if transcript_records else 0

    if transcript_records:
        for i, rec in enumerate(transcript_records):
            kind = rec.get("kind")
            role = rec.get("role")
            text = rec.get("text") or ""
            labels = {"tool": "工具", "approval": "审批", "worker": "协作", "error": "错误", "message": "消息"}
            category = labels.get(kind, "事件")
            title = str(rec.get("tool") or text.split("\n", 1)[0] or kind or "无")
            raw_actions.append({
                "category": category, "title": title, "actor": rec.get("actor"),
                "tool": rec.get("tool"), "input": rec.get("input"), "output": rec.get("output"),
                "thought": text if role == "assistant" else None,
                "text": text, "timestamp": rec.get("timestamp"), "status": rec.get("status"),
                "stageKey": rec.get("stageKey") or rec.get("data", {}).get("stageKey"),
                "stageTitle": rec.get("stageTitle") or rec.get("data", {}).get("stageTitle"),
                "data": rec,
            })
    else:
        # Fallback from messages
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            actor = msg.get("agent_id", "master")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)

            if role == "user":
                raw_actions.append({
                    "category": "调度",
                    "title": f"任务启动 · {str(content)[:35]}",
                    "actor": actor,
                    "tool": "",
                    "input": str(content),
                    "output": "",
                    "thought": "",
                })
            elif role == "assistant":
                raw_actions.append({
                    "category": "推理",
                    "title": f"认知推理 · {str(content)[:35]}",
                    "actor": actor,
                    "tool": "",
                    "input": "",
                    "output": "",
                    "thought": str(content),
                })
            for call in msg.get("tool_calls", []):
                fn = call.get("function", {})
                t_name = fn.get("name", "tool")
                raw_actions.append({
                    "category": "探测" if "fetch" in t_name or "search" in t_name else "利用",
                    "title": f"执行工具 · {t_name}",
                    "actor": actor,
                    "tool": t_name,
                    "input": fn.get("arguments", ""),
                    "output": "",
                    "thought": "",
                })

    # Keep every recorded operation, including repeated calls with distinct inputs.
    condensed = raw_actions
    if not condensed:
        nodes = []
        edges = []

    # Assign smooth timestamps
    timestamps = [
        2, 5, 13, 21, 29, 37, 45, 59, 67, 81, 95, 110, 125, 140, 160, 185, 210, 240, 270, 305,
        340, 375, 410, 450, 490, 530, 570, 620, 670, 720, 780, 840, 900, 960, 1020, 1080, 1140, 1200
    ]

    action_stream = []
    stage_counts = {s["key"]: 0 for s in STANDARD_STAGES}

    for idx, act in enumerate(condensed):
        t_sec = timestamps[idx] if idx < len(timestamps) else idx * 30 + 2
        offset_str = _format_time_offset(t_sec)

        stage_idx = min(len(STANDARD_STAGES) - 1, int(idx / len(condensed) * len(STANDARD_STAGES)))
        st = STANDARD_STAGES[stage_idx]
        stage_counts[st["key"]] += 1

        cat_colors = {
            "调度": "#38bdf8",
            "探测": "#06b6d4",
            "推理": "#a855f7",
            "利用": "#f43f5e",
            "证据": "#10b981",
        }

        # Format input/output strings safely
        inp_str = act["input"]
        out_str = act["output"]
        if isinstance(inp_str, (dict, list)):
            inp_str = json.dumps(inp_str, ensure_ascii=False, indent=2)
        if isinstance(out_str, (dict, list)):
            out_str = json.dumps(out_str, ensure_ascii=False, indent=2)

        action_stream.append({
            "step": idx,
            "id": f"act-{idx}",
            "timeOffset": offset_str,
            "timeSeconds": t_sec,
            "category": act["category"],
            "categoryColor": cat_colors.get(act["category"], "#38bdf8"),
            "title": act["title"],
            "actor": act["actor"],
            "tool": act.get("tool", ""),
            "input": str(inp_str)[:1000] if inp_str else "",
            "output": str(out_str)[:1000] if out_str else "",
            "fullInput": str(inp_str) if inp_str else "",
            "fullOutput": str(out_str) if out_str else "",
            "thought": act.get("thought"),
            "text": act.get("text"),
            "data": act.get("data"),
            "stageKey": st["key"],
            "stageIndex": stage_idx + 1,
            "stageTitle": st["title"],
            "stageTag": st["tag"],
            "cardId": st["cardId"],
            "status": "done",
        })

    # Prepare stage milestone data
    stages_data = []
    for s in STANDARD_STAGES:
        stages_data.append({
            **s,
            "count": stage_counts.get(s["key"], 1),
        })

    # Summary
    summary = {
        "sessionId": raw.get("id", ""),
        "name": session_name,
        "targetHost": target_host,
        "targetUrl": target_url,
        "targetNotes": target_notes,
        "createdAt": raw.get("created_at", 0),
        "totalActions": len(action_stream),
        "totalStages": len(STANDARD_STAGES),
        "findingsCount": len(findings_list),
        "targetsCount": len(targets_list),
        "credsCount": len(creds_list),
        "findings": findings_list,
        "targets": targets_list,
        "creds": creds_list,
    }

    return {
        "nodes": nodes,
        "edges": edges,
        "actions": action_stream,
        "timeline": action_stream,  # backwards compatibility
        "stages": stages_data,
        "summary": summary,
    }


def _session_to_timeline(raw: dict) -> list[dict]:
    """Return the structured action timeline."""
    graph = _session_to_graph(raw)
    return graph.get("actions", [])
