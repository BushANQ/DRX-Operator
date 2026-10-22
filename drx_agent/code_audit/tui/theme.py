"""Colour palette, text measurement and line painting for the audit terminal UI.

The UI is drawn as fixed cell grids: every line is measured and clipped by
display width, then painted with the style that its content calls for.  The
helpers here are shared by the screen widgets and by the snapshot exporter so
both always agree on the exact characters.
"""

from __future__ import annotations

import re
import unicodedata

from rich.cells import cell_len
from rich.text import Text

# Palette (ANSI-256 values resolved to their RGB equivalents).
FRAME = "#585858"
FOCUS = "#5fd7d7"
MUTED = "#9e9e9e"
SELECTED_FG = "#afffff"
SELECTED_BG = "#005f5f"
DANGER = "#ff5f5f"
WARNING = "#ffd75f"
SUCCESS = "#87d787"

DEFAULT_BG = "#000000"

TRUNCATION_SUFFIX = " …（显示截断）"

_ANSI = re.compile(
    "\\x1b(?:\\[[0-?]*[ -/]*[@-~]|\\][^\\x07\\x1b]*(?:\\x07|\\x1b\\\\))"
)

_STYLE_KINDS = ("accent", "muted", "body", "border_frame", "border_focus", "plain")


def display_width(text: str) -> int:
    """Return the terminal cell width of *text* (ANSI escapes are not expected)."""
    if not text:
        return 0
    return cell_len(text)


def safe_text(text: str) -> str:
    """Strip escape sequences and characters that would corrupt the cell grid."""
    text = _ANSI.sub("", text or "")
    out: list[str] = []
    for char in text:
        if char == "\n":
            out.append(char)
            continue
        if char == "\t":
            out.append(" ")
            continue
        code = ord(char)
        if unicodedata.category(char) == "Cc":
            continue
        if char in ("\u2028", "\u2029"):
            continue
        if 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:
            continue
        out.append(char)
    return "".join(out)


def bounded_text(text: str, limit: int) -> str:
    """Cut *text* to at most *limit* UTF-8 bytes on a rune boundary."""
    if limit <= 0:
        return ""
    from ..text import sanitize_text

    text = sanitize_text(text)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit]
    while cut:
        try:
            return cut.decode("utf-8") + TRUNCATION_SUFFIX
        except UnicodeDecodeError:
            cut = cut[:-1]
    return TRUNCATION_SUFFIX


def truncate_cells(text: str, width: int) -> str:
    """Hard-clip *text* to *width* terminal cells without ellipsis."""
    if width <= 0:
        return ""
    used = 0
    out: list[str] = []
    for char in text:
        size = cell_len(char)
        if size > width:
            char = "?"
            size = 1
        if used + size > width:
            break
        out.append(char)
        used += size
    return "".join(out)


def fit_line(text: str, width: int) -> str:
    """Sanitize, flatten newlines and hard-clip to *width* cells."""
    if width <= 0:
        return ""
    flat = safe_text(text).replace("\n", " ")
    return truncate_cells(flat, width)


def wrap_lines(text: str, width: int) -> list[str]:
    """Wrap *text* rune by rune; wide glyphs alone in a too-narrow column become '?'."""
    width = max(1, width)
    text = safe_text(text)
    result: list[str] = []
    for logical in text.split("\n"):
        buffer: list[str] = []
        used = 0
        for char in logical:
            size = cell_len(char)
            if size > width:
                char = "?"
                size = 1
            if used + size > width:
                result.append("".join(buffer))
                buffer = []
                used = 0
            buffer.append(char)
            used += size
        result.append("".join(buffer))
    return result


def forum_excerpt(text: str, width: int, rows: int) -> list[str]:
    """Collapse whitespace, bound the body and show at most *rows* lines."""
    collapsed = " ".join(safe_text(text).split())
    lines = wrap_lines(bounded_text(collapsed, 480), width)
    if len(lines) <= rows:
        return lines
    lines = lines[:rows]
    lines[rows - 1] = fit_line(lines[rows - 1], max(0, width - 2)) + fit_line(" …", width)
    return lines


def _status_prefix(trimmed: str) -> str:
    if trimmed.startswith("["):
        end = trimmed.find("]")
        if end >= 0:
            return trimmed[1:end].lower()
    return ""


def classify_line(raw: str) -> tuple[str | None, str | None, bool, bool]:
    """Return ``(foreground, background, bold, selected)`` for one rendered line."""
    trimmed = raw.strip()
    selected = trimmed.startswith(">")
    if selected:
        trimmed = trimmed[1:].strip()
    if trimmed.startswith("#"):
        parts = trimmed.split(" ", 1)
        trimmed = parts[1].strip() if len(parts) > 1 else ""
    status = _status_prefix(trimmed)

    if (
        status in ("critical", "high", "failed", "失败")
        or trimmed.startswith("! ")
    ):
        return DANGER, (SELECTED_BG if selected else None), True, selected
    if status in ("medium", "waiting", "paused", "等待") or trimmed.startswith(
        "保留缺口："
    ):
        return WARNING, (SELECTED_BG if selected else None), False, selected
    if status in ("completed", "完成", "low"):
        return SUCCESS, (SELECTED_BG if selected else None), False, selected
    if trimmed.startswith("──"):
        if trimmed.strip("─ ") == "":
            return FRAME, (SELECTED_BG if selected else None), False, selected
        return FOCUS, (SELECTED_BG if selected else None), True, selected
    if (
        status in ("+", "-", "running", "运行")
        or trimmed.startswith("标题：")
        or trimmed.startswith("回复 #")
    ):
        return FOCUS, (SELECTED_BG if selected else None), True, selected
    if (
        status in ("idle", "pending", "cancelled", "待开始", "停止")
        or trimmed.startswith("作者：")
        or trimmed.startswith("更新：")
        or trimmed.startswith("范围：")
        or trimmed.startswith("提示：")
        or trimmed.startswith("位置 ·")
    ):
        return MUTED, (SELECTED_BG if selected else None), False, selected
    if selected:
        return SELECTED_FG, SELECTED_BG, True, True
    return None, None, False, False


def paint_line(text: str) -> Text:
    """Paint one body line using the classification rules."""
    fg, bg, bold, _selected = classify_line(text)
    if fg is None and bg is None and not bold:
        return Text(text)
    style = ""
    if bold:
        style += "bold "
    if fg:
        style += fg
    if bg:
        style += f" on {bg}"
    return Text(text, style=style.strip())


def paint_accent(text: str) -> Text:
    return Text(text, style=f"bold {FOCUS}")


def paint_muted(text: str) -> Text:
    return Text(text, style=MUTED)


def paint_border(text: str, focused: bool = False) -> Text:
    return Text(text, style=FOCUS if focused else FRAME)


def style_line(kind: str, text: str) -> Text:
    """Convert a plain line plus its region style token into styled rich text."""
    if kind == "accent":
        return paint_accent(text)
    if kind == "muted":
        return paint_muted(text)
    if kind == "border_focus":
        return paint_border(text, True)
    if kind == "border_frame":
        return paint_border(text, False)
    if kind == "plain":
        return Text(text)
    return paint_line(text)


def render_row(segments: list[tuple[str, str]]) -> Text:
    """Compose one output row from styled segments."""
    row = Text()
    for text, kind in segments:
        if text:
            row.append_text(style_line(kind, text))
    return row

