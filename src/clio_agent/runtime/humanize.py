"""Small, dependency-free humanizers shared across runtime doctor checks."""

from __future__ import annotations


def format_bytes(size: int) -> str:
    """Render a byte count as a human-readable binary-unit string (e.g. ``2.0 GiB``)."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
