import subprocess
import shlex
import os
from drx_agent.engine.python_sandbox import SandboxResult

BLOCKED_PATTERNS = [
    "rm -rf", "rm -r", "dd ", "mkfs", "fdisk",
    "mount", "umount", "iptables", "shutdown",
    "reboot", "halt", "poweroff", "init 0", "init 6",
    ":(){ :|:& };:", "> /dev/sda",
]


class BashSandbox:
    def __init__(self, command_whitelist=None, timeout=120):
        self.command_whitelist = command_whitelist or []
        self.timeout = timeout

    def run(self, command: str, allow_destructive: bool = False) -> SandboxResult:
        import time
        start = time.time()

        if not allow_destructive:
            cmd_lower = command.lower()
            for pattern in BLOCKED_PATTERNS:
                if pattern.lower() in cmd_lower:
                    return SandboxResult(
                        status="blocked", stdout="",
                        stderr=f"Command blocked: matches destructive pattern '{pattern}'",
                        exit_code=-1, duration_ms=0,
                    )

            try:
                tokens = shlex.split(command)
                if tokens and self.command_whitelist:
                    base_cmd = os.path.basename(tokens[0])
                    if base_cmd not in self.command_whitelist:
                        return SandboxResult(
                            status="blocked", stdout="",
                            stderr=f"Command '{base_cmd}' not in whitelist",
                            exit_code=-1, duration_ms=0,
                        )
            except ValueError:
                pass

        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=self.timeout, executable='/bin/bash',
            )
            status = "success" if proc.returncode == 0 else "error"
            return SandboxResult(
                status=status, stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(), exit_code=proc.returncode,
                duration_ms=(time.time() - start) * 1000,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                status="timeout", stdout="",
                stderr=f"Command timed out after {self.timeout}s",
                exit_code=-1, duration_ms=(time.time() - start) * 1000,
            )
