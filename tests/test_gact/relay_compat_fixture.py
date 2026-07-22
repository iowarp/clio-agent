"""Vendored MINIMAL mirror of the clio-relay durable-record models (finding [12]).

clio-relay is NOT a clio-agent dependency (federation is future work), so the
against-the-real-repo shape-compat tests in ``test_artifacts_s5.py`` skip when the
sibling checkout is absent — which is the default CI condition. That leaves the
"validates as the REAL relay model" guarantee unenforced by the gate.

This checked-in fixture closes that hole: it mirrors JUST the three relay types the
clio-side projections (``ProvEdge.to_artifact_use`` /
``TransformRecord.to_relay_provenance``) must stay shape-compatible with —
``DurableRecordId``, ``ArtifactUse``, ``ArtifactRef`` — faithfully enough
(``extra="forbid"``, the 64-hex ``sha256`` constraint, the ``DurableRecordId``
pattern + validator) that a clio-side key/shape regression turns the fixture-backed
tests RED in every CI run. The real-repo test stays as an ADDITIONAL skip-if-absent
layer (a live snapshot catches relay-side drift on a dev box); this fixture catches
the clio-side regression the reviewer flagged.

PINNED to clio-relay commit ``772b47cacca64103ef5b14d8eade508c3c848c7e``
(``src/clio_relay/models.py`` + ``src/clio_relay/identifiers.py``). If relay's
durable-record shape changes, re-mirror from that repo and bump this hash; the
real-repo layer surfaces such drift when the sibling checkout is present.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

RELAY_PINNED_COMMIT = "772b47cacca64103ef5b14d8eade508c3c848c7e"

# --- identifiers.py -------------------------------------------------------- #

DURABLE_RECORD_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,127}$"
DURABLE_RECORD_ID_MAX_BYTES = 128
_DURABLE_RECORD_ID = re.compile(DURABLE_RECORD_ID_PATTERN)


def validate_durable_record_id(value: object) -> str:
    """Mirror of ``clio_relay.identifiers.validate_durable_record_id`` (minimal)."""
    if not isinstance(value, str):
        raise TypeError("durable record ID must be a string")
    encoded = value.encode("utf-8")
    if len(encoded) > DURABLE_RECORD_ID_MAX_BYTES:
        raise ValueError(
            f"durable record ID must be at most {DURABLE_RECORD_ID_MAX_BYTES} UTF-8 bytes"
        )
    if _DURABLE_RECORD_ID.fullmatch(value) is None:
        raise ValueError(
            f"durable record ID must match {DURABLE_RECORD_ID_PATTERN}: lowercase portable ASCII"
        )
    return value


DurableRecordId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=DURABLE_RECORD_ID_MAX_BYTES,
        pattern=DURABLE_RECORD_ID_PATTERN,
    ),
    AfterValidator(validate_durable_record_id),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- models.py ------------------------------------------------------------- #


class ArtifactUse(BaseModel):
    """Mirror of ``clio_relay.models.ArtifactUse`` — frozen, ``extra='forbid'``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: DurableRecordId
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("sha256")
    @classmethod
    def sha256_must_be_canonical(cls, value: str) -> str:
        lowered = value.lower()
        if len(lowered) != 64 or any(ch not in "0123456789abcdef" for ch in lowered):
            raise ValueError("sha256 must be a SHA-256 digest")
        return lowered


class ArtifactRef(BaseModel):
    """Mirror of ``clio_relay.models.ArtifactRef`` — ``extra='forbid'`` (metadata is free)."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: DurableRecordId
    job_id: DurableRecordId
    sequence: Optional[int] = Field(default=None, ge=1)
    uri: str
    kind: str
    size_bytes: Optional[int] = Field(default=None, ge=0)
    sha256: Optional[str] = None
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "RELAY_PINNED_COMMIT",
    "ArtifactRef",
    "ArtifactUse",
    "DurableRecordId",
    "validate_durable_record_id",
]
