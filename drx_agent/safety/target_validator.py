"""Validates that a target is safe to interact with.

Blocks government / military domains and known public-infrastructure
networks.  An optional allow-list restricts operations to specific
IP ranges or domain suffixes.
"""

import ipaddress

# Domains that are always blocked regardless of the allow-list.
BLOCKED_DOMAINS: set[str] = {".gov", ".mil"}

# Networks that are always blocked (common public DNS / CDN infrastructure).
BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("8.8.8.0/24"),
    ipaddress.ip_network("1.1.1.0/24"),
]


class TargetValidator:
    """Checks whether a target (IP or domain) may be interacted with.

    Global block-lists take precedence, then domain matching, then the
    per-target allow-list.
    """

    def __init__(self, allowed: list[str] | None = None) -> None:
        self.allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.allowed_domains: set[str] = set()
        if allowed:
            for entry in allowed:
                try:
                    self.allowed_networks.append(ipaddress.ip_network(entry))
                except ValueError:
                    self.allowed_domains.add(entry)

    def is_allowed(self, target: str) -> bool:
        
        # Global block-lists take precedence.
        for suffix in BLOCKED_DOMAINS:
            if target.endswith(suffix):
                return False

        try:
            addr = ipaddress.ip_address(target)
            for net in BLOCKED_NETWORKS:
                if addr in net:
                    return False
            if self.allowed_networks:
                for net in self.allowed_networks:
                    if addr in net:
                        return True
                return False
            return True
        except ValueError:
            pass

        if self.allowed_domains:
            for d in self.allowed_domains:
                if target == d or target.endswith("." + d):
                    return True
            return False
        return True

