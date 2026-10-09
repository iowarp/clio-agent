"""Remove terminal display sequences from structured host command replies."""

from __future__ import annotations

import re

_DISPLAY_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def structured_stdout(value: str) -> str:
    """Discard CSI/OSC decoration while preserving actual paths and JSON strings."""
    return _DISPLAY_SEQUENCE.sub("", value)
