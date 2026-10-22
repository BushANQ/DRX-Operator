"""Stage close requests and unanimous close voting."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from .notifications import excerpt, compact_json

CLOSE_CONSENSUS_TIMEOUT = 180.0

CONTINUE_AUDIT_WORK = (
    "关闭尚未获准；继续自己的审计：自行选择尚未覆盖的其他文件、入口或模块，"
    "建立具体待办并读取源码、追踪新的调用链。不要等待或催促其他成员，不要反复请求关闭，"
    "也不要围绕其他 Agent 的已有结论重复复核来消磨时间；没有新的独立审计进展，"
    "不重复提交同一关闭请求。"
)

APPROVE_VOTES = ("approve", "approved", "yes", "同意")
REJECT_VOTES = ("reject", "rejected", "no", "不同意", "否决")


@dataclass
class CloseDecision:
    status: str
    request_id: int = 0
    required: int = 0
    approved: int = 0
    deadline: float = 0.0
    summary: str = ""
    next_steps: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        data = {"status": self.status}
        if self.request_id:
            data["request_id"] = self.request_id
        data["required"] = self.required
        data["approved"] = self.approved
        if self.deadline:
            data["deadline"] = _iso(self.deadline)
        if self.summary:
            data["summary"] = self.summary
        if self.next_steps:
            data["next_steps"] = self.next_steps
        if self.reason:
            data["reason"] = self.reason
        return data


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


def _clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def close_vote(vote: str) -> tuple[bool, bool]:
    value = (vote or "").strip().lower()
    if value in APPROVE_VOTES:
        return True, False
    if value in REJECT_VOTES:
        return False, True
    return False, False


def set_consensus_members(board, stage: str, ids) -> None:
    members = {agent_id for agent_id in ids if str(agent_id).strip()}
    with board.cond:
        board._consensus_members[stage] = members


def reset_consensus(board, stage: str) -> None:
    with board.cond:
        board._consensus.pop(stage, None)
        board._signal_locked()


def members_locked(board, stage: str) -> dict[str, bool]:
    members: dict[str, bool] = {}
    configured = board._consensus_members.get(stage, set())
    for agent_id, participant in board._agents.items():
        if (
            agent_id != "moderator"
            and participant.stage != "moderator"
            and participant.stage == stage
            and (not configured or agent_id in configured)
        ):
            members[agent_id] = True
    return members


def close_decision(request: dict, reason: str) -> CloseDecision:
    approved = sum(
        1
        for agent_id, vote in request["votes"].items()
        if vote and request["members"].get(agent_id)
    )
    return CloseDecision(
        status=request["status"],
        request_id=request["id"],
        required=len(request["members"]),
        approved=approved,
        deadline=request["deadline"],
        summary=request["summary"],
        next_steps=request["next_steps"],
        reason=reason,
    )


def _announce(board, content: str) -> None:
    if not content.strip():
        return
    try:
        board.post("coordinator", "system", "*", 0, "审计协商", content)
    except ValueError:
        pass


def announce_consensus(board, content: str) -> None:
    _announce(board, content)


def close_announcement(topic: str, actor: str, decision: CloseDecision) -> str:
    content = "%s：%s（明确同意 %d/%d，截止 %s）。" % (
        actor,
        decision.reason,
        decision.approved,
        decision.required,
        _clock(decision.deadline),
    )
    if decision.summary and topic == "审计关闭请求":
        content += " 请求摘要：" + excerpt(decision.summary, 768)
    return content


def request_close(board, agent_id, stage, summary, next_steps, vote) -> CloseDecision:
    return request_close_at(board, agent_id, stage, summary, next_steps, vote, time.time())


def request_close_at(board, agent_id, stage, summary, next_steps, vote, now) -> CloseDecision:
    vote = (vote or "").strip().lower()
    approve, reject = close_vote(vote)
    if not approve and not reject:
        return CloseDecision(status="invalid", reason="vote must be approve or reject")
    summary = (summary or "").strip()
    if not summary:
        summary = "审计团队已完成当前审计。"
    next_steps = (next_steps or "").strip()

    decision = None
    announce = ""
    callback = None
    start_id = 0
    with board.cond:
        participant = board._agents.get(agent_id)
        if (
            participant is None
            or participant.stage != stage
            or agent_id == "moderator"
            or stage == "moderator"
        ):
            return CloseDecision(status="invalid", reason="未注册的 Agent 或阶段不匹配")
        members = members_locked(board, stage)
        if not members.get(agent_id):
            return CloseDecision(status="invalid", reason="该身份不是本阶段的审计 Agent")
        request = board._consensus.get(stage)
        if request is not None and request["status"] == "pending" and now >= request["deadline"]:
            request["status"] = "timed_out"
            decision = close_decision(
                request,
                "其他 Agent 未在 60 秒内全部明确同意，关闭请求已超时，本轮请求不获准。"
                + CONTINUE_AUDIT_WORK,
            )
            announce = close_announcement("审计关闭请求超时", participant.name, decision)
            board._signal_locked()
        elif request is not None and request["status"] == "approved":
            return close_decision(request, "本阶段关闭请求已经一致通过")
        elif request is None or request["status"] != "pending":
            if reject:
                return CloseDecision(
                    status="invalid",
                    reason="当前没有可否决的关闭请求；先发起新的 end_audit 请求",
                )
            board._next_consensus_id += 1
            request = {
                "id": board._next_consensus_id,
                "stage": stage,
                "summary": summary,
                "next_steps": next_steps,
                "deadline": now + CLOSE_CONSENSUS_TIMEOUT,
                "members": dict(members),
                "votes": {agent_id: True},
                "status": "pending",
            }
            board._consensus[stage] = request
            start_id = request["id"]
            decision = close_decision(
                request,
                "关闭请求 pending，尚需本阶段所有 Agent 明确同意。" + CONTINUE_AUDIT_WORK,
            )
            announce = close_announcement("审计关闭请求", participant.name, decision)
            if decision.approved == decision.required:
                request["status"] = "approved"
                decision = close_decision(request, "本阶段所有 Agent 已一致同意关闭")
                callback = board._on_consensus
        else:
            if reject:
                request["status"] = "rejected"
                decision = close_decision(
                    request,
                    "Agent %s 已否决关闭请求，本轮请求已拒绝。%s"
                    % (participant.name, CONTINUE_AUDIT_WORK),
                )
                announce = close_announcement("审计关闭请求被否决", participant.name, decision)
                board._signal_locked()
            else:
                request["votes"][agent_id] = True
                decision = close_decision(
                    request,
                    "已记录同意票，关闭请求仍 pending，尚需其他 Agent 明确同意。"
                    + CONTINUE_AUDIT_WORK,
                )
                announce = close_announcement("审计关闭投票", participant.name, decision)
                if decision.approved == decision.required:
                    request["status"] = "approved"
                    decision = close_decision(request, "本阶段所有 Agent 已一致同意关闭")
                    callback = board._on_consensus
        board._signal_locked()

    if start_id and decision.status == "pending":
        delay = max(0.0, now + CLOSE_CONSENSUS_TIMEOUT - time.time())
        timer = threading.Timer(delay, expire_close, args=(board, stage, start_id))
        timer.daemon = True
        timer.start()
    if announce:
        announce_consensus(board, announce)
    if callback is not None:
        callback(stage)
    return decision


def expire_close(board, stage: str, request_id: int) -> None:
    announce = ""
    with board.cond:
        request = board._consensus.get(stage)
        if (
            request is None
            or request["id"] != request_id
            or request["status"] != "pending"
            or time.time() < request["deadline"]
        ):
            return
        request["status"] = "timed_out"
        decision = close_decision(
            request,
            "其他 Agent 未在 60 秒内全部明确同意，关闭请求已超时，本轮请求不获准。"
            + CONTINUE_AUDIT_WORK,
        )
        announce = close_announcement("审计关闭请求超时", "系统", decision)
        board._signal_locked()
    announce_consensus(board, announce)


def consensus_approved(board, agent_id: str, stage: str):
    with board.cond:
        request = board._consensus.get(stage)
        if request is None or request["status"] != "approved":
            return None
        if not request["members"].get(agent_id):
            return None
        return close_decision(request, "本阶段所有 Agent 已一致同意关闭")


def consensus_approved_for_stage(board, stage: str):
    with board.cond:
        request = board._consensus.get(stage)
        if request is None or request["status"] != "approved":
            return None
        return close_decision(request, "本阶段所有 Agent 已一致同意关闭")


def encode_decision(decision: CloseDecision) -> str:
    return compact_json(decision.to_dict())
