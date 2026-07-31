"""Identity guarantees for the clio-schemas compatibility shims."""

from __future__ import annotations

import clio_schemas

from clio_agent.gact.artifacts import records, transform_types, transforms


def test_clio_schemas_models_are_reexported_by_identity() -> None:
    """Legacy import paths expose the canonical classes, not subclasses or copies."""

    assert clio_schemas.ArtifactVersion is records.ArtifactVersion
    assert clio_schemas.ArtifactRecord is records.ArtifactRecord
    assert clio_schemas.ProvEdge is transform_types.ProvEdge
    assert clio_schemas.TransformRecord is transforms.TransformRecord
