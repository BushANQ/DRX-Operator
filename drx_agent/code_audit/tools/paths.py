"""Workspace path normalization and sandbox checks."""

from __future__ import annotations

import os


def safe_path(workspace: str, relative: str) -> str:
    value = relative or "."
    if not os.path.isabs(value):
        value = os.path.join(workspace, value)
    absolute = os.path.abspath(value)
    root = os.path.normpath(workspace)
    clean = os.path.normpath(absolute)
    if clean != root and not clean.startswith(root + os.sep):
        raise ValueError("path escapes workspace: %s" % relative)
    return clean


def normalize_inventory_path(path: str) -> str:
    value = (path or "").strip().replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    return value.strip("/")
