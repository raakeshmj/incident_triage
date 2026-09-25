"""Sanitizing untrusted text from telemetry.

Log lines, commit messages and span names are untrusted input
(docs/architecture/13-security-boundaries.md, "Prompt-injection defense",
point 3): strip terminal escape sequences and control characters and bound
lengths before anything reaches a normalized payload. NUL is also stripped
from raw responses because Postgres JSONB cannot store it.
"""

from __future__ import annotations

import re
from typing import Any

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: str, max_chars: int) -> str:
    cleaned = _CONTROL.sub("", _ANSI_ESCAPE.sub("", value))
    if len(cleaned) > max_chars:
        return cleaned[: max_chars - 1] + "…"
    return cleaned


def strip_nul(value: Any) -> Any:
    """Recursively remove NUL from strings (JSONB can't hold it)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [strip_nul(v) for v in value]
    return value
