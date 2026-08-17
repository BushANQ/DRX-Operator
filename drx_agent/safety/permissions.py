"""Declarative permission engine for tool calls.

Orthogonal to the L0-L4 SafetyGate: matches a tool call against an ordered
list of glob rules and returns allow / ask / deny. First matching rule
wins; no match → default allow. Interactive sessions can extend an ask to
"allow for the rest of the session" via the phrase `always`."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field


Action = str


@dataclass
class PermissionRule:
    tool: str = "*"
    match: str = "*"
    action: Action = "ask"
    note: str = ""

    def matches(self, tool_name: str, args_repr: str) -> bool:
        if not fnmatch.fnmatchcase(tool_name, self.tool):
            return False
        if self.match and self.match != "*":
            if not fnmatch.fnmatchcase(args_repr, self.match):
                return False
        return True


@dataclass
class PermissionDecision:
    action: Action
    rule: PermissionRule | None = None
    args_repr: str = ""

    @property
    def reason(self) -> str:
        if self.rule is None:
            return "no rule matched (default)"
        return self.rule.note or (
            f"matched rule: {self.rule.tool} / {self.rule.match} → {self.rule.action}"
        )


# Default rules: deliberately liberal (operator can tighten via config); first match wins.
DEFAULT_RULES: list[PermissionRule] = [
    PermissionRule("execute_bash", "*rm -rf /*", "deny", "destructive root delete"),
    PermissionRule("execute_bash", "*mkfs*", "deny", "filesystem formatting"),
    PermissionRule("execute_bash", "*dd if=*of=/dev/*", "deny", "raw device write"),
    PermissionRule("execute_bash", "*:(){ :|:&*", "deny", "fork bomb"),
    PermissionRule("shell_exec", "*rm -rf /*", "deny", "destructive root delete"),
    PermissionRule("write_file", "*path=/etc/*", "ask", "writing under /etc"),
    PermissionRule("write_file", "*path=/System/*", "deny", "macOS System volume"),
    PermissionRule("write_file", "*path=/usr/*", "ask", "writing under /usr"),
    PermissionRule("edit_file", "*path=/etc/*", "ask", "editing under /etc"),
    PermissionRule("edit_file", "*path=/System/*", "deny", "macOS System volume"),
    PermissionRule("*", "*", "allow", "default"),
]


class PermissionEngine:
    """Matches tool calls against an ordered rule list; first match wins, default action allow."""

    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self.rules: list[PermissionRule] = list(rules) if rules else list(DEFAULT_RULES)
        self._session_grants: dict[tuple[str, str], bool] = {}
        self._session_tool_grants: set[str] = set()

    @staticmethod
    def _args_repr(args: dict) -> str:
        
        parts: list[str] = []
        for k in sorted(args.keys()):
            v = args[k]
            if isinstance(v, str):
                parts.append(f"{k}={v}")
            else:
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        return " ".join(parts)

    def check(self, tool_name: str, args: dict) -> PermissionDecision:
        args_repr = self._args_repr(args)

        if tool_name in self._session_tool_grants:
            return PermissionDecision(action="allow", args_repr=args_repr)
        if self._session_grants.get((tool_name, args_repr)):
            return PermissionDecision(action="allow", args_repr=args_repr)

        for rule in self.rules:
            if rule.matches(tool_name, args_repr):
                return PermissionDecision(
                    action=rule.action, rule=rule, args_repr=args_repr
                )
        return PermissionDecision(action="allow", args_repr=args_repr)

    def grant_session(self, tool_name: str, args_repr: str = "") -> None:
        """Remember this approval for the rest of the session."""
        if args_repr:
            self._session_grants[(tool_name, args_repr)] = True
        else:
            self._session_tool_grants.add(tool_name)

    def add_rule(self, rule: PermissionRule, prepend: bool = True) -> None:
        if prepend:
            self.rules.insert(0, rule)
        else:
            if self.rules and self.rules[-1].tool == "*" and self.rules[-1].match == "*":
                self.rules.insert(-1, rule)
            else:
                self.rules.append(rule)

