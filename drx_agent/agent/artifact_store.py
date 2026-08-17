"""Disk-backed artifact store — the "result storage" layer.

Offloads large tool results to disk with previews; read_artifact fetches
full content by id."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class ArtifactStore:
    def __init__(self, base_dir: str) -> None:
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict] = {}
        self._index_path = self.dir / "_index.json"
        self._load_index()

    def _load_index(self) -> None:
        try:
            if self._index_path.is_file():
                self._index = json.loads(self._index_path.read_text("utf-8"))
        except Exception:
            self._index = {}

    def _save_index(self) -> None:
        try:
            self._index_path.write_text(
                json.dumps(self._index, ensure_ascii=False), "utf-8"
            )
        except Exception:
            pass

    def store(self, content: str, tool: str = "", kind: str = "tool_result") -> str:
        """Persist *content*, return its artifact id."""
        art_id = f"a{uuid.uuid4().hex[:8]}"
        path = self.dir / f"{art_id}.txt"
        try:
            path.write_text(content, encoding="utf-8")
        except Exception:
        # Persist failure → keep inline; caller handles None by not offloading.
            return ""
        self._index[art_id] = {
            "id": art_id,
            "path": str(path),
            "tool": tool,
            "kind": kind,
            "size": len(content),
            "ts": time.time(),
            "preview": content[:160].replace("\n", " "),
        }
        self._save_index()
        return art_id

    def read(self, art_id: str, offset: int = 0, limit: int = 0) -> str | None:
        meta = self._index.get(art_id)
        if not meta:
            return None
        try:
            text = Path(meta["path"]).read_text("utf-8", errors="replace")
        except Exception:
            return None
        if offset:
            text = text[offset:]
        if limit and limit > 0:
            text = text[:limit]
        return text

    def meta(self, art_id: str) -> dict | None:
        return self._index.get(art_id)

    def list(self) -> list[dict]:
        return sorted(self._index.values(), key=lambda m: m.get("ts", 0))

    def make_pointer(self, content: str, tool: str = "", head: int = 1200,
                     tail: int = 400, kind: str = "tool_result") -> str | None:
        """Store *content* and return a compact head+tail+pointer string."""
        art_id = self.store(content, tool=tool, kind=kind)
        if not art_id:
            return None
        h = content[:head]
        t = content[-tail:] if len(content) > head + tail else ""
        dropped = len(content) - len(h) - len(t)
        mid = (
            f"\n…[{dropped} 字符已存档 → artifact://{art_id} "
            f"(read_artifact('{art_id}') 取回全文)]…\n"
        )
        return h + mid + t if t else (h + mid)

