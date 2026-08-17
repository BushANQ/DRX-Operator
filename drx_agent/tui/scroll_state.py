TAIL_SENTINEL = -1


class ScrollState:
    """Flat Line-Offset Scroll — reference: DeepSeek-TUI scrolling.rs"""

    def __init__(self):
        self._offset: int = TAIL_SENTINEL
        self._total_lines: int = 0

    @property
    def is_tailing(self) -> bool:
        return self._offset == TAIL_SENTINEL

    @property
    def offset(self) -> int:
        return max(0, self._offset) if self._offset != TAIL_SENTINEL else 0

    def resolve_offset(self, visible_lines: int, total_lines: int) -> int:
        self._total_lines = total_lines
        if self._offset == TAIL_SENTINEL:
            return max(0, total_lines - visible_lines)
        return min(self._offset, max(0, total_lines - visible_lines))

    def scroll_up(self, delta: int, visible_lines: int, total_lines: int) -> None:
        if self._offset == TAIL_SENTINEL:
            self._offset = max(0, total_lines - visible_lines)
        self._offset = max(0, self._offset - delta)

    def scroll_down(self, delta: int, visible_lines: int, total_lines: int) -> None:
        if self._offset == TAIL_SENTINEL:
            return
        max_offset = max(0, total_lines - visible_lines)
        self._offset = min(max_offset, self._offset + delta)
        if self._offset >= max_offset:
            self._offset = TAIL_SENTINEL

    def page_up(self, visible_lines: int, total_lines: int) -> None:
        self.scroll_up(visible_lines, visible_lines, total_lines)

    def page_down(self, visible_lines: int, total_lines: int) -> None:
        self.scroll_down(visible_lines, visible_lines, total_lines)

    def scroll_to_bottom(self) -> None:
        self._offset = TAIL_SENTINEL

    def scroll_to_top(self) -> None:
        self._offset = 0

    def on_new_content(self, visible_lines: int, total_lines: int) -> int:
        """Called when new content arrives; returns current offset"""
        if self._offset == TAIL_SENTINEL:
            return max(0, total_lines - visible_lines)
        self._offset += 1
        return self._offset
