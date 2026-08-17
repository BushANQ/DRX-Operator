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

    def has_evidence(self) -> bool:
        return len(self.evidence) > 0

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "confidence": self.confidence,
            "evidence": [asdict(e) for e in self.evidence],
            "verified": self.verified,
            "cve": self.cve,
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        return cls(
            claim=data.get("claim", ""),
            confidence=data.get("confidence", 0.0),
            evidence=[Evidence(**e) for e in data.get("evidence", [])],
            verified=data.get("verified", False),
            cve=data.get("cve", ""),
            severity=data.get("severity", "info"),
        )

    def evidence_chain(self) -> str:
        if not self.evidence:
            return "(no evidence — needs verification)"
        return " → ".join(
            f"[{e.type}] {e.value or e.cve or e.payload}" for e in self.evidence
        )
