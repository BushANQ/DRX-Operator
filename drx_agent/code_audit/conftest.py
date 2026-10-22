"""Pytest support for the audit test suite.

Provides a ``tmp_path`` that also works in sandboxes which deny writes inside
newly created private temp directories.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from drx_agent.code_audit._test_utils import writable_temp_dir


@pytest.fixture
def tmp_path():
    path = Path(writable_temp_dir("pytest-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
