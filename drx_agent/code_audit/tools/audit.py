"""Finding records, local finding submission and audit termination."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass
class Finding:
    id: int = 0
    finding_key: str = ""
    severity: str = ""
    title: str = ""
    path: str = ""
    line: int = 0
    evidence: str = ""
    impact: str = ""
    recommendation: str = ""
    cwe: str = ""

    @property
    def key(self) -> str:
        return self.finding_key

    @key.setter
    def key(self, value: str) -> None:
        self.finding_key = value

    def to_dict(self) -> dict:
        data = {"id": self.id}
        if self.finding_key:
            data["finding_key"] = self.finding_key
        data["severity"] = self.severity
        data["title"] = self.title
        data["path"] = self.path
        if self.line:
            data["line"] = self.line
        data["evidence"] = self.evidence
        data["impact"] = self.impact
        data["recommendation"] = self.recommendation
        if self.cwe:
            data["cwe"] = self.cwe
        return data

    def copy(self) -> "Finding":
        return Finding(
            id=self.id,
            finding_key=self.finding_key,
            severity=self.severity,
            title=self.title,
            path=self.path,
            line=self.line,
            evidence=self.evidence,
            impact=self.impact,
            recommendation=self.recommendation,
            cwe=self.cwe,
        )


@dataclass
class AuditState:
    ended: bool = False
    summary: str = ""
    next_steps: str = ""

    def to_dict(self) -> dict:
        data = {"ended": self.ended}
        if self.summary:
            data["summary"] = self.summary
        if self.next_steps:
            data["next_steps"] = self.next_steps
        return data


@dataclass
class AuditSummary:
    summary: str
    findings: list = field(default_factory=list)
    next_steps: str = ""

    def to_dict(self) -> dict:
        data = {"summary": self.summary}
        if self.next_steps:
            data["next_steps"] = self.next_steps
        data["findings"] = [finding.to_dict() for finding in self.findings]
        return data


def finding_key(finding: Finding) -> str:
    data = finding.to_dict()
    data.pop("finding_key", None)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def report_finding(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    title = args.get("title", "")
    path = args.get("path", "")
    evidence = args.get("evidence", "")
    if not isinstance(title, str):
        return Result(ok=False, error="title is required")
    if not isinstance(path, str):
        return Result(ok=False, error="path is required")
    if not isinstance(evidence, str):
        return Result(ok=False, error="evidence is required")
    if not title:
        return Result(ok=False, error="title is required")
    if not path:
        return Result(ok=False, error="path is required")
    if not evidence:
        return Result(ok=False, error="evidence is required")
    severity = args.get("severity", "")
    if not isinstance(severity, str):
        return Result(ok=False, error="invalid severity")
    if not severity:
        severity = "medium"
    line = args.get("line", 0)
    if isinstance(line, bool) or not isinstance(line, int):
        line = 0
    finding = Finding(
        id=len(registry.findings) + 1,
        severity=severity,
        title=title,
        path=path,
        line=line,
        evidence=evidence,
        impact=args.get("impact", "") if isinstance(args.get("impact", ""), str) else "",
        recommendation=args.get("recommendation", "")
        if isinstance(args.get("recommendation", ""), str)
        else "",
        cwe=args.get("cwe", "") if isinstance(args.get("cwe", ""), str) else "",
    )
    registry.findings.append(finding)
    return Result(ok=True, data=finding.to_dict(), message="finding recorded")


def end_audit(registry, args: dict):
    from .registry import Result

    if not isinstance(args, dict):
        return Result(ok=False, error="invalid arguments")
    vote = args.get("vote", "")
    if not isinstance(vote, str) or vote.strip().lower() != "approve":
        return Result(ok=False, error="end_audit requires an explicit team approve vote")
    summary = args.get("summary", "")
    if not isinstance(summary, str):
        summary = ""
    if not summary:
        summary = "Audit completed."
    next_steps = args.get("next_steps", "")
    if not isinstance(next_steps, str):
        next_steps = ""
    registry.audit = AuditState(ended=True, summary=summary, next_steps=next_steps)
    return Result(
        ok=True,
        data=AuditSummary(
            summary=summary,
            findings=[finding.copy() for finding in registry.findings],
            next_steps=next_steps,
        ).to_dict(),
        message="audit ended",
    )
