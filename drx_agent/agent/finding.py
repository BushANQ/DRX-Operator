from dataclasses import asdict, dataclass, field


@dataclass
class Evidence:
    type: str
    value: str = ""
    source: str = ""
    cve: str = ""
    version_range: str = ""
    payload: str = ""
    result: str = ""


@dataclass
class Finding:
    claim: str
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    verified: bool = False
    cve: str = ""
    severity: str = "info"
    status: str = "suspected"
    superseded_by: str = ""

    VALID_STATUSES = ("suspected", "confirmed", "exploited", "retracted")

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "confidence": self.confidence,
            "evidence": [asdict(e) for e in self.evidence],
            "verified": self.verified,
            "cve": self.cve,
            "severity": self.severity,
            "status": self.status,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        status = data.get("status", "confirmed" if data.get("verified") else "suspected")
        if status not in cls.VALID_STATUSES:
            status = "suspected"
        return cls(
            claim=data.get("claim", ""),
            confidence=data.get("confidence", 0.0),
            evidence=[Evidence(**e) for e in data.get("evidence", [])],
            verified=data.get("verified", False),
            cve=data.get("cve", ""),
            severity=data.get("severity", "info"),
            status=status,
            superseded_by=data.get("superseded_by", ""),
        )

    def evidence_chain(self) -> str:
        if not self.evidence:
            return "(no evidence — needs verification)"
        return " → ".join(
            f"[{e.type}] {e.value or e.cve or e.payload}" for e in self.evidence
        )
