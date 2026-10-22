"""Task-scoped audit budgets (wall clock and provider token usage)."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .events import BudgetStatus


@dataclass
class _RequestUsage:
    request_id: int = 0
    total_tokens: int = 0
    estimated: bool = True
    final: bool = False


class BudgetBook:
    """Accumulates elapsed time and token usage for one team run."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.started: float | None = None
        self.tokens = 0
        self.estimated = False
        self.estimated_requests = 0
        self.requests: dict[int, _RequestUsage] = {}
        self.reason = ""
        self._timer: asyncio.TimerHandle | None = None
        self._on_exhausted = None

    # -- status ---------------------------------------------------------
    def status(self, cfg: Any, running: bool) -> BudgetStatus:
        elapsed = self.elapsed
        if self.started is not None:
            elapsed += time.monotonic() - self.started
        return BudgetStatus(
            infinite_mode=bool(cfg.agent.infinite_mode),
            hours=int(cfg.agent.budget_hours),
            minutes=int(cfg.agent.budget_minutes),
            token_limit=int(cfg.agent.budget_tokens),
            used_tokens=int(self.tokens),
            elapsed=elapsed,
            running=running,
            stop_reason=self.reason,
            estimated=self.estimated or self.estimated_requests > 0,
        )

    # -- limits ---------------------------------------------------------
    def limit_reason(self, cfg: Any) -> str:
        if not cfg.agent.infinite_mode:
            return ""
        duration = cfg.agent.budget_duration()
        if duration > 0 and self.status(cfg, True).elapsed >= duration:
            return "时间预算已耗尽"
        if cfg.agent.budget_tokens > 0 and self.tokens >= cfg.agent.budget_tokens:
            return "token 预算已耗尽"
        return ""

    def configure(self, cfg: Any) -> None:
        self.reason = self.limit_reason(cfg)

    # -- lifecycle ------------------------------------------------------
    def start(self, cfg: Any, on_exhausted) -> None:
        self.reason = self.limit_reason(cfg)
        self._on_exhausted = on_exhausted
        self.started = time.monotonic()
        self.requests = {}
        self.estimated_requests = 0
        duration = cfg.agent.budget_duration()
        if cfg.agent.infinite_mode and duration > 0:
            delay = max(0.0, duration - self.elapsed)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._timer = loop.call_later(delay, self._expire_time, cfg)

    def _expire_time(self, cfg: Any) -> None:
        if self.reason:
            return
        self.reason = "时间预算已耗尽"
        if self._on_exhausted is not None:
            self._on_exhausted()

    def observe(self, update: Any) -> None:
        """Apply one cumulative usage update for a network request."""

        total = int(getattr(update, "total_tokens", 0) or 0)
        if total < 0:
            return
        request_id = int(getattr(update, "request_id", 0) or 0)
        final = bool(getattr(update, "final", False))
        estimated = bool(getattr(update, "estimated", True))
        previous = self.requests.get(request_id)
        previous_total = previous.total_tokens if previous is not None else 0
        delta = total - previous_total
        if delta > 0 and self.tokens > 2**63 - 1 - delta:
            self.tokens = 2**63 - 1
        else:
            self.tokens += delta
        if previous is not None and previous.estimated:
            self.estimated_requests -= 1
        if final:
            self.requests.pop(request_id, None)
            self.estimated = self.estimated or estimated
        else:
            self.requests[request_id] = _RequestUsage(request_id, total, estimated, False)
            if estimated:
                self.estimated_requests += 1
        # Token limits only apply in infinite mode, mirroring the engine policy.
        reason = self.limit_reason(self._cfg) if self._cfg is not None else ""
        if reason and not self.reason:
            self.reason = reason
            if self._on_exhausted is not None:
                self._on_exhausted()

    _cfg: Any = None

    def bind_config(self, cfg: Any) -> None:
        self._cfg = cfg

    def finish(self, cfg: Any) -> str:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self.started is not None:
            self.elapsed += time.monotonic() - self.started
            self.started = None
        self.estimated = self.estimated or self.estimated_requests > 0
        self.estimated_requests = 0
        self.requests = {}
        return self.reason
