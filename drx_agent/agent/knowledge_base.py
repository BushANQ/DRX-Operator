import time
import uuid
from dataclasses import asdict, dataclass, field

from drx_agent.agent.finding import Finding


@dataclass
class Credential:
    """A single captured credential (password / hash / token / key)."""

    host: str
    username: str
    secret: str
    type: str = "password"
    service: str = ""
    port: int | None = None
    source: str = ""
    verified: bool = False
    notes: str = ""
    id: str = field(default_factory=lambda: f"cred-{uuid.uuid4().hex[:6]}")
    ts: float = field(default_factory=time.time)

    def fingerprint(self) -> tuple:
        """Identity for dedup: host+user+service+port+secret-prefix."""
        return (
            self.host,
            self.username,
            self.service,
            self.port,
            self.secret[:64],
        )


class KnowledgeBase:
    def __init__(self):
        self._targets: dict[str, dict] = {}
        self._findings: dict[str, list[Finding]] = {}
    # Credential vault keyed by host; target dicts hold lightweight id pointers so they stay JSON-serializable.
        self._credentials: dict[str, list[Credential]] = {}
        self.blackboard = None

    def update_target(self, host: str, **kwargs) -> None:
        if host not in self._targets:
            self._targets[host] = {
                "host": host,
                "open_ports": [],
                "services": {},
                "vulns": [],
                "owned": False,
                "creds": [],
                "notes": "",
            }
        self._targets[host].update(kwargs)

    def get_target(self, host: str) -> dict | None:
        return self._targets.get(host)

    def list_targets(self) -> list[dict]:
        return list(self._targets.values())

    def add_finding(self, host: str, finding: Finding) -> None:
        if host not in self._findings:
            self._findings[host] = []
        self._findings[host].append(finding)
        if host in self._targets and finding.cve:
            if finding.cve not in self._targets[host]["vulns"]:
                self._targets[host]["vulns"].append(finding.cve)

    def get_findings(self, host: str) -> list[Finding]:
        return self._findings.get(host, [])

    def all_findings(self) -> list[tuple[str, Finding]]:
        out: list[tuple[str, Finding]] = []
        for host, fs in self._findings.items():
            out.extend((host, f) for f in fs)
        return out

    def finding_total(self) -> int:
        return sum(len(fs) for fs in self._findings.values())

    def findings_since(self, start: int) -> list[tuple[str, str]]:
        flat = self.all_findings()
        return [(host, f.claim) for host, f in flat[max(start, 0) :]]

    def update_finding_status(
        self, host: str, claim_substr: str, status: str, superseded_by: str = ""
    ) -> Finding | None:
        if status not in Finding.VALID_STATUSES:
            return None
        for f in self._findings.get(host, []):
            if claim_substr.lower() in f.claim.lower():
                f.status = status
                f.verified = status in ("confirmed", "exploited")
                if superseded_by:
                    f.superseded_by = superseded_by
                return f
        return None

    def mark_owned(self, host: str, method: str = "") -> None:
        if host in self._targets:
            self._targets[host]["owned"] = True
            if method:
                self._targets[host]["notes"] += f"\nowned via: {method}"

    def owned_targets(self) -> list[dict]:
        return [t for t in self._targets.values() if t.get("owned")]


    def add_credential(self, cred: Credential) -> Credential:
        """Add (or dedup) a credential. Returns the stored Credential."""
        self.update_target(cred.host)
        bucket = self._credentials.setdefault(cred.host, [])
        fp = cred.fingerprint()
        for existing in bucket:
            if existing.fingerprint() == fp:
                if cred.verified and not existing.verified:
                    existing.verified = True
                if cred.source and cred.source != existing.source:
                    existing.notes = (
                        existing.notes + (f" / also-from:{cred.source}" if existing.notes else f"also-from:{cred.source}")
                    )
                return existing
        bucket.append(cred)
        target = self._targets[cred.host]
        target.setdefault("creds", [])
        target["creds"].append(cred.id)
        return cred

    def list_credentials(self, host: str | None = None) -> list[Credential]:
        if host is not None:
            return list(self._credentials.get(host, []))
        out: list[Credential] = []
        for bucket in self._credentials.values():
            out.extend(bucket)
        return out

    def mark_credential_verified(
        self, host: str, username: str, service: str = "", port: int | None = None
    ) -> Credential | None:
        for c in self._credentials.get(host, []):
            if c.username != username:
                continue
            if service and c.service and c.service != service:
                continue
            if port is not None and c.port is not None and c.port != port:
                continue
            c.verified = True
            return c
        return None

    def remove_credential(self, cred_id: str) -> bool:
        for host, bucket in list(self._credentials.items()):
            for c in list(bucket):
                if c.id == cred_id:
                    bucket.remove(c)
                    if host in self._targets:
                        try:
                            self._targets[host]["creds"].remove(cred_id)
                        except ValueError:
                            pass
                    return True
        return False

    def to_dict(self) -> dict:
        data = {
            "targets": self._targets,
            "findings": {
                host: [f.to_dict() for f in fs]
                for host, fs in self._findings.items()
            },
            "credentials": {
                host: [asdict(c) for c in creds]
                for host, creds in self._credentials.items()
            },
            "finding_count": sum(len(f) for f in self._findings.values()),
        }
        if self.blackboard is not None:
            data["blackboard"] = self.blackboard.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeBase":
        kb = cls()
        kb._targets = data.get("targets", {})
        for host, fs in (data.get("findings") or {}).items():
            kb._findings[host] = [Finding.from_dict(f) for f in fs]
        for host, creds in (data.get("credentials") or {}).items():
            kb._credentials[host] = [Credential(**c) for c in creds]
        bb_data = data.get("blackboard")
        if isinstance(bb_data, dict):
            from drx_agent.agent.blackboard import Blackboard

            kb.blackboard = Blackboard.from_dict(bb_data)
        return kb
