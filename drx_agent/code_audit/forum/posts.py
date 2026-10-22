"""Thread grouping, numbered post pages and the forum_threads output."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .notifications import compact_json
from .search import PostSearch, SearchReadArgs, new_post_search, search_excerpt

DEFAULT_PAGE_SIZE = 60
MAX_PAGE = 64


def _timestamp(value: str) -> float:
    if not value:
        return 0.0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def copy_moderation(dst, src) -> None:
    dst.closed = src.closed
    dst.pinned = src.pinned
    dst.announcement = src.announcement
    dst.moderation_reason = src.moderation_reason


@dataclass
class ThreadPost:
    id: int
    root: object
    replies: list = field(default_factory=list)
    updated_at: str = ""
    last_id: int = 0
    root_missing: bool = False


@dataclass
class PostPage:
    posts: list
    page: int
    page_size: int
    total_posts: int
    total_pages: int
    has_more: bool
    gap: bool


def group_posts(messages: list) -> list[ThreadPost]:
    groups: dict[int, int] = {}
    parents: dict[int, int] = {}
    posts: list[ThreadPost] = []
    for message in messages:
        thread_id = message.thread_id
        if thread_id == 0:
            thread_id = message.id
            if message.reply_to > 0:
                thread_id = parents.get(message.reply_to, 0) or message.reply_to
        parents[message.id] = thread_id
        if thread_id not in groups:
            from .forum import Message

            groups[thread_id] = len(posts)
            posts.append(
                ThreadPost(id=thread_id, root=Message(), root_missing=True)
            )
        post = posts[groups[thread_id]]
        if message.id == thread_id:
            post.root = message
            post.root_missing = False
        else:
            post.replies.append(message)
        copy_moderation(post.root, message)
        if (
            _timestamp(message.created_at) > _timestamp(post.updated_at)
            or (
                _timestamp(message.created_at) == _timestamp(post.updated_at)
                and message.id > post.last_id
            )
        ):
            post.updated_at = message.created_at
            post.last_id = message.id
    posts.sort(
        key=lambda post: (
            1 if post.root.pinned else 0,
            _timestamp(post.updated_at),
            post.last_id,
        ),
        reverse=True,
    )
    return posts


def list_posts(board, page: int, page_size: int, query: PostSearch | None = None) -> PostPage:
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = DEFAULT_PAGE_SIZE
    if page_size > MAX_PAGE:
        page_size = MAX_PAGE
    with board.cond:
        posts = group_posts(board._msgs)
        gap = bool(board._msgs) and board._msgs[0].id > 1
    if query is not None and query.terms:
        posts = [post for post in posts if query.message(post).id != 0]
    total = len(posts)
    pages = (total + page_size - 1) // page_size
    if pages > 0 and page > pages:
        page = pages
    if pages == 0:
        page = 1
    start = (page - 1) * page_size
    end = min(start + page_size, total)
    return PostPage(
        posts=posts[start:end],
        page=page,
        page_size=page_size,
        total_posts=total,
        total_pages=pages,
        has_more=page < pages,
        gap=gap,
    )


def _post_summary(post: ThreadPost, query: PostSearch) -> dict:
    topic = post.root.topic
    if post.root_missing:
        topic = "原帖已过期"
    message = query.message(post)
    hits = query.hits(message)
    preview, offset = search_excerpt(message.content, "content", hits, 256)
    preview_topic, _ = search_excerpt(message.topic, "topic", hits, 128)
    read_args = None
    if hits:
        read_args = SearchReadArgs(message_id=message.id, offset=offset, all=True).to_dict()
    summary = {
        "id": post.id,
        "topic": topic,
        "agent_id": post.root.agent_id,
        "stage": post.root.stage,
    }
    if post.root.agent_name:
        summary["agent_name"] = post.root.agent_name
    summary.update(
        {
            "excerpt": preview,
            "excerpt_topic": preview_topic,
            "excerpt_message_id": message.id,
            "excerpt_truncated": preview != message.content or preview_topic != message.topic,
            "reply_count": len(post.replies),
            "latest_message_id": post.last_id,
            "updated_at": post.updated_at,
            "root_missing": post.root_missing,
            "closed": post.root.closed,
            "pinned": post.root.pinned,
            "announcement": post.root.announcement,
        }
    )
    if post.root.moderation_reason:
        summary["moderation_reason"] = post.root.moderation_reason
    if hits:
        summary["matches"] = [hit.to_dict() for hit in hits]
    summary["excerpt_offset"] = offset
    if read_args is not None:
        summary["read_args"] = read_args
    return summary


def _failure(message: str) -> str:
    return compact_json({"ok": False, "error": message})


def _int_arg(args: dict, key: str) -> int:
    value = args.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid int")
    return value


def thread_page(board, args) -> str:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _failure("无效帖子分页参数；使用 page、limit、query、match")
    if any(key not in ("page", "limit", "query", "match") for key in args):
        return _failure("无效帖子分页参数；使用 page、limit、query、match")
    try:
        page = _int_arg(args, "page")
        limit = _int_arg(args, "limit")
    except ValueError:
        return _failure("无效帖子分页参数；使用 page、limit、query、match")
    query_text = args.get("query", "")
    match = args.get("match", "")
    if not isinstance(query_text, str) or not isinstance(match, str):
        return _failure("无效帖子分页参数；使用 page、limit、query、match")
    if page < 0 or limit < 0:
        return _failure("无效帖子分页参数；使用 page、limit、query、match")
    if match not in ("", "phrase", "all", "any"):
        return _failure("match 必须为 phrase、all 或 any")
    query = new_post_search(query_text, match)
    if len(query_text.encode("utf-8")) > 512 or len(query.terms) > 8:
        return _failure("query 最多 512 UTF-8 字节；多关键词最多 8 项")
    result = list_posts(board, page, limit, query)
    output = {
        "ok": True,
        "posts": [_post_summary(post, query) for post in result.posts],
        "page": result.page,
        "page_size": result.page_size,
        "total_posts": result.total_posts,
        "total_pages": result.total_pages,
        "has_more": result.has_more,
        "gap": result.gap,
    }
    return compact_json(output)
