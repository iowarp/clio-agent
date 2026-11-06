"""A view's data source, live or pinned (taskable run_code plan, Part 3).

A chart, table or map reads one fixed version (``artifact://artifact_<id>``) or follows
its source: a study source, or a named artifact at its latest version
(``artifact://<workspace>/<name>@latest``). The producer checks a named source against
the version that is latest now, and reports each binding in the tool result
(``data_bindings``) so the agent knows which views will follow their data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_producer._refusal import refusal

if TYPE_CHECKING:
    from fastapi import FastAPI


def latest_artifact_id(
    app: "FastAPI", data_uri: str, component_id: str
) -> tuple[str, int] | dict[str, Any] | None:
    """``(artifact_id, version)`` of a named reference at its latest version, a refusal
    when it names nothing, or ``None`` when ``data_uri`` is not a named ``@latest``."""

    from clio_agent.gact.artifacts.references import (  # noqa: PLC0415
        ReferenceResolutionError,
        _resolve_named,
        parse_reference,
    )
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    if not data_uri.startswith("artifact://") or not data_uri.endswith("@latest"):
        return None
    try:
        parsed = parse_reference(data_uri)
        if parsed.kind != "artifact_name":
            return None
        resolved = _resolve_named(get_registry(app), parsed, data_uri)
    except ReferenceResolutionError as exc:
        return refusal(
            "a2ui_data_reference_not_found",
            detail=f"component {component_id!r} dataUri {data_uri!r}: {exc.message}",
        )
    return str(resolved["artifact_id"]), int(resolved["version"])


def record_binding(
    bindings: list[dict[str, Any]] | None,
    component_id: str,
    data_uri: str,
    binding: str,
    **fields: Any,
) -> None:
    """Add one ``{component, dataUri, binding, ...}`` row when ``bindings`` is given."""

    if bindings is not None:
        bindings.append(
            {"component": component_id, "dataUri": data_uri, "binding": binding, **fields}
        )


__all__ = ["latest_artifact_id", "record_binding"]
