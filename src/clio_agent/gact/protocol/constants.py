"""Single-source protocol identifiers used by negotiation and projections."""

from __future__ import annotations

GACT_V2 = "0.2"
GACT_V3 = "0.3"
A2UI_V091 = "0.9.1"
A2UI_V091_WIRE = f"v{A2UI_V091}"
#: The official ``server_to_client.json``/``client_to_server.json`` envelope
#: ``version`` enum is ``["v0.9", "v0.9.1"]`` (adversarial review, S2): a
#: producer may spell either. This is a WIRE-envelope concern only -- the
#: ``x-a2ui-version`` negotiation header and ``A2UISurfaceRecord.protocol_version``
#: keep naming ``A2UI_V091_WIRE`` ("0.9.1"); an accepted "v0.9" message is
#: persisted with its own spelling verbatim, never rewritten.
A2UI_WIRE_VERSIONS = frozenset({"v0.9", A2UI_V091_WIRE})

__all__ = [
    "A2UI_V091",
    "A2UI_V091_WIRE",
    "A2UI_WIRE_VERSIONS",
    "GACT_V2",
    "GACT_V3",
]
