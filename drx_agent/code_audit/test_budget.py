"""Budget accounting and status tests."""

from __future__ import annotations

import asyncio

import pytest

from drx_agent.code_audit import test_agent_support as support
from drx_agent.code_audit.agent.budget import BudgetBook
from drx_agent.code_audit.agent.events import BudgetStatus


def test_default_budget_status():
    cfg = support.make_config()
    book = BudgetBook()
    book.bind_config(cfg)
    status = book.status(cfg, False)
    assert isinstance(status, BudgetStatus)
    assert status.hours == 8
    assert status.minutes == 0
    assert status.token_limit == 0
    assert status.infinite_mode is False
    assert status.running is False
    assert status.elapsed == 0
    assert status.estimated is False


def test_finite_mode_has_no_time_or_token_stop():
    cfg = support.make_config(budget_tokens=1, budget_hours=0, budget_minutes=0)
    book = BudgetBook()
    book.bind_config(cfg)
    assert book.limit_reason(cfg) == ""
    book.tokens = 999
    assert book.limit_reason(cfg) == ""


def test_infinite_time_budget_exhaustion():
    cfg = support.make_config(infinite_mode=True, budget_hours=0, budget_minutes=1)
    book = BudgetBook()
    book.bind_config(cfg)
    reasons = []
    book.start(cfg, lambda: reasons.append("stop"))
    book.elapsed = 59.0
    assert book.limit_reason(cfg) == ""
    book.elapsed = 60.0
    assert book.limit_reason(cfg) == "时间预算已耗尽"
    book.finish(cfg)


def test_infinite_token_budget_exhaustion_via_observer():
    cfg = support.make_config(infinite_mode=True, budget_tokens=1500)
    book = BudgetBook()
    book.bind_config(cfg)
    stopped = []
    book.start(cfg, lambda: stopped.append(True))

    class Update:
        request_id = 1
        total_tokens = 1000
        estimated = False
        final = False

    book.observe(Update())
    assert book.tokens == 1000
    assert stopped == []
    update = Update()
    update.total_tokens = 2000
    book.observe(update)
    assert book.tokens == 2000
    assert book.reason == "token 预算已耗尽"
    assert stopped == [True]
    book.finish(cfg)


def test_observer_uses_cumulative_deltas_and_estimates():
    cfg = support.make_config()
    book = BudgetBook()
    book.bind_config(cfg)
    book.start(cfg, lambda: None)

    class Update:
        request_id = 7
        total_tokens = 0
        estimated = True
        final = False

    update = Update()
    update.total_tokens = 100
    book.observe(update)
    update = Update()
    update.total_tokens = 250
    book.observe(update)
    assert book.tokens == 250
    update = Update()
    update.total_tokens = 250
    update.final = True
    update.estimated = True
    book.observe(update)
    assert book.tokens == 250
    assert book.estimated is True
    book.finish(cfg)


def test_configure_recomputes_reason_without_resetting_usage():
    cfg = support.make_config(infinite_mode=True, budget_tokens=100)
    book = BudgetBook()
    book.bind_config(cfg)
    book.tokens = 100
    book.reason = book.limit_reason(cfg)
    assert book.reason == "token 预算已耗尽"
    cfg.agent.budget_tokens = 0
    book.configure(cfg)
    assert book.reason == ""
    assert book.tokens == 100


def test_finish_accumulates_elapsed_and_clears_requests():
    cfg = support.make_config()
    book = BudgetBook()
    book.bind_config(cfg)
    book.start(cfg, lambda: None)

    class Update:
        request_id = 1
        total_tokens = 10
        estimated = True
        final = False

    book.observe(Update())
    reason = book.finish(cfg)
    assert reason == ""
    assert book.started is None
    assert book.requests == {}
