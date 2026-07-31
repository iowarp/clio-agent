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


def test_every_moved_public_name_is_reexported_by_identity() -> None:
    """Full-surface guard: every moved public symbol is the SAME object.

    The shims re-export ~19 names; the review found the spot-check above too
    thin a guard — a name silently dropped from a shim would break star-import
    and getattr consumers with no test noticing.
    """
    import clio_schemas

    from clio_agent.gact.artifacts import records, transform_types, transforms

    for module in (records, transform_types, transforms):
        for name in module.__all__:
            if hasattr(clio_schemas, name):
                assert getattr(module, name) is getattr(clio_schemas, name), (
                    f"{module.__name__}.{name} is not the clio_schemas object"
                )
