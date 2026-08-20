"""Compatibility surface for artifact records now owned by clio-schemas.

The canonical model and enum objects are imported directly so legacy paths
preserve object identity. Alias validation remains clio-agent behavior rather
than a cross-service wire schema concern.
"""

from __future__ import annotations

import re

from clio_schemas import (
    RESERVED_KINDS,
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
    new_artifact_id,
)

_VERSION_ALIAS_RE = re.compile(r"^v\d+$")


class InvalidAliasError(ValueError):
    """A rejected alias name with a typed reason."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def alias_rejection_reason(alias: str) -> str:
    """Return a typed rejection reason for an illegal user alias, else ``""``."""

    name = alias.strip()
    if name == "latest":
        return "reserved_alias"
    if _VERSION_ALIAS_RE.match(name):
        return "invalid_alias"
    return ""


__all__ = [
    "RESERVED_KINDS",
    "ArtifactKind",
    "ArtifactRecord",
    "ArtifactVersion",
    "Custody",
    "EvidenceClass",
    "IdentityEvidence",
    "InvalidAliasError",
    "Mechanism",
    "alias_rejection_reason",
    "new_artifact_id",
]
