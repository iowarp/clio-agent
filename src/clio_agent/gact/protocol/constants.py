"""Single-source protocol identifiers used by negotiation and projections."""

from __future__ import annotations

GACT_V2 = "0.2"
GACT_V3 = "0.3"
A2UI_V091 = "0.9.1"
A2UI_V091_WIRE = f"v{A2UI_V091}"

__all__ = [
    "A2UI_V091",
    "A2UI_V091_WIRE",
    "GACT_V2",
    "GACT_V3",
]
