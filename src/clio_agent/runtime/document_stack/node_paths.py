"""Translate Windows filesystem namespaces at the Node.js process boundary."""

from __future__ import annotations


def node_path(value: str) -> str:
    """Keep drive and UNC identity while removing Node-incompatible verbatim prefixes."""
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\") and len(value) > 6 and value[5:7] == ":\\":
        return value[4:]
    return value
