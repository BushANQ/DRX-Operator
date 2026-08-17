"""Maps operation names to risk levels.

This is the central policy table.  Add new operations here to have them
automatically classified by the safety gate.
"""

from drx_agent.safety.gate import RiskLevel

OPERATION_RISK_MAP: dict[str, RiskLevel] = {
    # L0 — Reconnaissance (safe, read-only).
    "port_scan": RiskLevel.L0,
    "dns_enum": RiskLevel.L0,
    "http_probe": RiskLevel.L0,
    "banner_grab": RiskLevel.L0,
    "ssl_analyze": RiskLevel.L0,
    "dir_brute": RiskLevel.L0,
    # L1 — Passive vulnerability scanning (auto-approved per session).
    "sqli_detect": RiskLevel.L1,
    "xss_detect": RiskLevel.L1,
    "ssti_detect": RiskLevel.L1,
    "lfi_detect": RiskLevel.L1,
    # L2 — Active exploitation (requires explicit user approval).
    "rce_payload": RiskLevel.L2,
    "webshell_upload": RiskLevel.L2,
    "deser_attack": RiskLevel.L2,
    "oob_callback": RiskLevel.L2,
    # L3 — Credential / persistence attacks (requires explicit approval).
    "pass_the_hash": RiskLevel.L3,
    "kerberoast": RiskLevel.L3,
    "credential_theft": RiskLevel.L3,
    "ssh_key_inject": RiskLevel.L3,
    "cron_backdoor": RiskLevel.L3,
    # L4 — Destructive / irreversible (requires confirmation phrase).
    "rm_files": RiskLevel.L4,
    "dd_disk": RiskLevel.L4,
    "system_config_change": RiskLevel.L4,
    "ddos": RiskLevel.L4,
}


def get_risk_level(operation: str) -> RiskLevel:
    """Return the risk level for *operation*, defaulting to L2."""
    return OPERATION_RISK_MAP.get(operation, RiskLevel.L2)
