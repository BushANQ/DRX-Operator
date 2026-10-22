"""Literal forum search primitives.

Search is a case-insensitive literal substring match.  Terms must all occur in
one retained message (a message's topic and body may supply different terms);
terms are never combined across replies.
"""

from __future__ import annotations

from dataclasses import dataclass

from .notifications import excerpt_from

# Python's str.lower() performs full case mapping for a few characters, which
# can change the character count (for example U+0130). A simple 1:1
# mapping, so use an override where the difference would shift search offsets.
_SIMPLE_LOWER = {"\u0130": "i"}


def simple_lower(text: str) -> str:
    out: list[str] = []
    for ch in text:
        low = ch.lower()
        if len(low) == 1:
            out.append(low)
        else:
            out.append(_SIMPLE_LOWER.get(ch, low[0]))
    return "".join(out)


@dataclass
class SearchHit:
    term: str
    field: str
    offset: int
    end_offset: int
    line: int
    column: int

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "field": self.field,
            "offset": self.offset,
            "end_offset": self.end_offset,
            "line": self.line,
            "column": self.column,
        }


@dataclass
class SearchReadArgs:
    message_id: int
    offset: int
    all: bool = True

    def to_dict(self) -> dict:
        return {"message_id": self.message_id, "offset": self.offset, "all": self.all}


def original_span(text: str, before: int, count: int) -> tuple[int, int]:
    """Map a rune span in the lowered text back to byte offsets in the original."""
    data = text.encode("utf-8")
    start = end = len(data)
    rune_index = 0
    offset = 0
    for ch in text:
        if rune_index == before:
            start = offset
        if rune_index == before + count:
            end = offset
            break
        rune_index += 1
        offset += len(ch.encode("utf-8"))
    return start, end


def _prefix(text: str, byte_offset: int) -> str:
    return text.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore")


class PostSearch:
    def __init__(self, terms: list[str] | None = None, all_terms: bool = True) -> None:
        self.terms = list(terms or [])
        self.all = all_terms

    def matches(self, message) -> bool:
        if not self.terms:
            return True
        topic = simple_lower(message.topic)
        body = simple_lower(message.content)
        for term in self.terms:
            found = term in topic or term in body
            if found and not self.all:
                return True
            if not found and self.all:
                return False
        return self.all

    def message(self, post):
        if not post.root_missing and self.matches(post.root):
            return post.root
        for reply in post.replies:
            if self.matches(reply):
                return reply
        from .forum import Message

        return Message()

    def hits(self, message) -> list[SearchHit]:
        hits: list[SearchHit] = []
        body_lower = simple_lower(message.content)
        topic_lower = simple_lower(message.topic)
        for term in self.terms:
            field = "content"
            text = message.content
            lower = body_lower
            index = lower.find(term)
            if index < 0:
                field = "topic"
                text = message.topic
                lower = topic_lower
                index = lower.find(term)
            if index < 0:
                continue
            start, end = original_span(text, index, len(term))
            prefix = _prefix(text, start)
            line = prefix.count("\n") + 1
            line_start = prefix.rfind("\n") + 1
            column = len(prefix[line_start:]) + 1
            hits.append(
                SearchHit(
                    term=term,
                    field=field,
                    offset=start,
                    end_offset=end,
                    line=line,
                    column=column,
                )
            )
        return hits


def new_post_search(query: str, mode: str = "") -> PostSearch:
    query = simple_lower((query or "").strip())
    if not query:
        return PostSearch()
    if mode in ("all", "any"):
        return PostSearch(query.split(), mode == "all")
    return PostSearch([query], True)


def search_excerpt(text: str, field: str, hits: list[SearchHit], limit: int) -> tuple[str, int]:
    offset = 0
    for hit in hits:
        if hit.field != field:
            continue
        offset = hit.offset
        data = text.encode("utf-8")
        for _ in range(16):
            if offset <= 0:
                break
            index = offset - 1
            while index > 0 and (data[index] & 0xC0) == 0x80:
                index -= 1
            offset -= offset - index
        break
    return excerpt_from(text, offset, limit), offset
