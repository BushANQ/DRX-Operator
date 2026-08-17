"""Safety gate for controlling access to risky operations.

Risk levels escalate from L0 (fully auto-approved reconnaissance) through
L4 (destructive operations requiring a confirmation phrase).  The gate
remembers session-level approvals so the user does not have to re-approve
the same operation-target pair within a session.
"""

from dataclasses import dataclass
from enum import Enum
import uuid
import time


class RiskLevel(str, Enum):
    L0 = "L0"  # Reconnaissance — always auto-approved.
    L1 = "L1"  # Passive vulnerability scanning — auto-approved, logged.
    L2 = "L2"  # Active exploitation — requires explicit approve() call.
    L3 = "L3"  # Credential / persistence attacks — requires approve() call.
    L4 = "L4"  # Destructive / irreversible — requires confirmation phrase.


@dataclass
class ApprovalRequest:
    request_id: str
    operation: str
    risk_level: RiskLevel
    target: str
    timestamp: float
    approved: bool = False
    requires_approval: bool = False
    requires_confirmation_phrase: bool = False


@dataclass
class CheckResult:
    approved: bool
    requires_approval: bool = False
    requires_confirmation_phrase: bool = False
    request_id: str | None = None


class SafetyGate:
    """Gate that checks whether an operation should be allowed to proceed.

    L0  — always auto-approved.
    L1  — auto-approved *per session* (target+op remembered).
    L2  — requires user approve() for the specific request.
    L3  — requires user approve() for the specific request.
    L4  — requires a confirmation phrase (destroy-action-phrase).
    """

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._session_approvals: set[str] = set()

    def check(self, operation: str, risk_level: RiskLevel, target: str) -> CheckResult:
        
        session_key = f"{operation}:{target}"
        if session_key in self._session_approvals:
            return CheckResult(approved=True)

        if risk_level == RiskLevel.L0:
            return CheckResult(approved=True)

        req_id = str(uuid.uuid4())[:8]
        req = ApprovalRequest(
            request_id=req_id,
            operation=operation,
            risk_level=risk_level,
            target=target,
            timestamp=time.time(),
            requires_approval=risk_level in (RiskLevel.L2, RiskLevel.L3),
            requires_confirmation_phrase=(risk_level == RiskLevel.L4),
        )

        if risk_level == RiskLevel.L1:
            req.approved = True
            self._session_approvals.add(session_key)
            return CheckResult(approved=True)

        self._pending[req_id] = req
        return CheckResult(
            approved=False,
            requires_approval=req.requires_approval,
            requires_confirmation_phrase=req.requires_confirmation_phrase,
            request_id=req_id,
        )

    def approve(self, request_id: str) -> bool:
        """Approve a pending request; True if it existed."""
        if request_id in self._pending:
            req = self._pending[request_id]
            req.approved = True
            self._session_approvals.add(f"{req.operation}:{req.target}")
            del self._pending[request_id]
            return True
        return False

    def deny(self, request_id: str) -> None:
        
        self._pending.pop(request_id, None)

    def get_pending(self) -> list[ApprovalRequest]:
        
        return list(self._pending.values())

