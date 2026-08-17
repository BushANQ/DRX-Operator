import subprocess
import tempfile
import os
from dataclasses import dataclass, field


@dataclass
class SandboxResult:
    status: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float


# Spec promises socket/ssl/urllib/etc. availability; we block code-injection
# vectors (os/subprocess/ctypes) and unsafe deserialization (pickle/marshal).
BLOCKED_MODULES = {
    'os', 'subprocess', 'shutil', 'ctypes',
    'http.server', 'socketserver', 'pickle', 'marshal',
}

# Pre-imported with the guard disabled so their own `import os` etc. succeed and cache transitive deps.
SAFE_PRELOAD = (
    "urllib.request", "urllib.parse", "urllib.error",
    "ssl", "socket", "http.client",
    "json", "re", "base64", "hashlib", "time",
    "email", "html.parser", "html",
    "ipaddress", "struct", "binascii", "string",
    "io", "collections", "itertools", "functools",
    "datetime", "random", "uuid",
)


class PythonSandbox:
    def __init__(self, timeout: int = 60, memory_mb: int = 256):
        self.timeout = timeout
        self.memory_mb = memory_mb

    def run(self, code: str) -> SandboxResult:
        import time
        start = time.time()
        safe_code = self._wrap_code(code)

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False, encoding='utf-8'
        ) as f:
            f.write(safe_code)
            script_path = f.name

        try:
            proc = subprocess.run(
                ['python3', script_path],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
            )
            status = "success" if proc.returncode == 0 else "error"
            if proc.returncode == -9:
                status = "memory_error"
            return SandboxResult(
                status=status,
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
                exit_code=proc.returncode,
                duration_ms=(time.time() - start) * 1000,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                status="timeout", stdout="",
                stderr=f"Execution timed out after {self.timeout}s",
                exit_code=-1, duration_ms=(time.time() - start) * 1000,
            )
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass

    def _wrap_code(self, code: str) -> str:
        # Import-guard protocol: flip guard off, preload SAFE_PRELOAD so their
        # transitive `import os` succeeds, then flip on — every __import__ after that is intercepted.
        preamble = f"""
import sys, builtins, resource
try:
    resource.setrlimit(resource.RLIMIT_AS, ({self.memory_mb} * 1024 * 1024, {self.memory_mb} * 1024 * 1024))
except (ValueError, AttributeError):
    pass

_orig_import = builtins.__import__
_BLOCKED = {BLOCKED_MODULES!r}
_guard_active = [False]

def _safe_import(name, *args, **kwargs):
    if _guard_active[0]:
        top = name.split('.')[0]
        if top in _BLOCKED:
            raise ImportError(f'import {{name}} is blocked in sandbox')
    return _orig_import(name, *args, **kwargs)

builtins.__import__ = _safe_import

for _mod in {SAFE_PRELOAD!r}:
    try:
        _orig_import(_mod)
    except Exception:
        pass

# Make urllib HTTPS fetches work even when the system CA bundle is missing
# (common on macOS / minimal containers). The agent is a red-team tool;
# strict TLS verification is the operator's call.
try:
    import ssl as _ssl
    _ssl._create_default_https_context = _ssl._create_unverified_context
except Exception:
    pass

_guard_active[0] = True
"""
        return f"{preamble}\n{code}"
