"""Files a viewer reaches through a binding are exported like literal ones (#1823).

A viewer's file property (``meshUri``, ``materialUri``, ``rasterUri``, ``geojsonUri``,
``dataUri``, ``url``, ``uri``) may be bound (``{"path": "/..."}``) instead of literal,
so one viewer can follow a list. Its value then comes from one of two places, and each
is exported at the same boundary as a literal path (:mod:`._export`): a workspace file
path becomes an ``artifact://`` reference and is reported in ``exported_artifacts``.

* **A selection** (``/selection/<key>/values/...``): the values are cells of the
  ``selectionField`` column of the components whose ``selection`` is bound to
  ``/selection/<key>``. Their inline ``rows`` are exported. A table read by
  ``dataUri`` keeps its column as written, so that column holds artifact URIs.
* **The data model** (any other path): the value at that pointer, when the data model
  is written (``create_a2ui_surface``'s ``data_model``, ``update_a2ui_data_model``).
  After export a bound viewer file must be an ``artifact://`` reference (a bound
  ``url``/``uri``/``dataUri`` any URL the catalog allows); anything else is refused
  (``a2ui_bound_file_invalid``), the same rule a literal value meets.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_catalogs.validation import is_allowed_a2ui_url
from clio_agent.gact.a2ui_producer._export import _Exporter, _path_candidate
from clio_agent.gact.a2ui_producer._refusal import refusal

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Properties naming a file a viewer loads; only an ``artifact://`` value is loadable.
VIEWER_FILE_KEYS = frozenset({"meshUri", "materialUri", "rasterUri", "geojsonUri"})
#: Every property naming a file or URL.
FILE_KEYS = VIEWER_FILE_KEYS | {"dataUri", "url", "uri"}
_SELECTION = "/selection/"


def _tokens(pointer: str) -> list[str]:
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]


def file_bindings(components: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """``(component id, property, pointer)`` for every bound file property."""

    bound = []
    for component in components:
        if not isinstance(component, dict):
            continue
        for key in FILE_KEYS:
            value = component.get(key)
            if isinstance(value, dict) and str(value.get("path") or "").startswith("/"):
                bound.append((str(component.get("id") or ""), key, value["path"]))
    return bound


def _export(exporter: _Exporter, value: Any, *, component_id: str, prop: str) -> Any:
    """``value`` exported when it is a workspace path; a refusal when it can't be shown."""

    if value is None:
        return None
    if not isinstance(value, str):
        return refusal(
            "a2ui_bound_file_invalid",
            detail=f"{component_id}.{prop} is bound to {value!r}, which is not a file reference",
        )
    path = _path_candidate(value)
    if path is not None:
        resolved = exporter.resolve(value, path)
        if isinstance(resolved, dict):
            return resolved
        return exporter.mint(resolved, raw=value, component_id=component_id, prop=prop)
    loadable = value.startswith("artifact://") if prop in VIEWER_FILE_KEYS else None
    if loadable is False or (loadable is None and not is_allowed_a2ui_url(value)):
        return refusal(
            "a2ui_bound_file_invalid",
            detail=(
                f"{component_id}.{prop} is bound to {value!r}: a viewer loads a registered "
                "artifact (artifact://...) or a file inside the workspace, given by its path"
            ),
        )
    return value


def export_selection_rows(
    app: "FastAPI", session_id: str, components: list[dict[str, Any]]
) -> list[dict[str, Any]] | dict[str, Any]:
    """Export the selection-field cells of inline rows that bound viewers follow.

    Rewrites ``components`` in place; returns the exported entries or a refusal.
    """

    keys = {
        _tokens(pointer)[1]
        for _, _, pointer in file_bindings(components)
        if pointer.startswith(_SELECTION) and len(_tokens(pointer)) > 1
    }
    exporter = _Exporter(app, session_id)
    for component in components:
        selection = component.get("selection") if isinstance(component, dict) else None
        path = selection.get("path") if isinstance(selection, dict) else None
        field = component.get("selectionField") if isinstance(component, dict) else None
        rows = component.get("rows") if isinstance(component, dict) else None
        if not isinstance(path, str) or not path.startswith(_SELECTION) or path[11:] not in keys:
            continue
        if not isinstance(field, str) or not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get(field), str):
                if _path_candidate(row[field]) is None:
                    continue
                exported = _export(
                    exporter, row[field], component_id=str(component.get("id")), prop=field
                )
                if isinstance(exported, dict):
                    return exported
                row[field] = exported
    return exporter.exported


def _export_selection_values(
    exporter: _Exporter,
    value: Any,
    base: list[str],
    values_path: list[str],
    component_id: str,
    prop: str,
) -> dict[str, Any] | None:
    """Export the path strings of a seeded ``/selection/<key>/values`` list in place."""

    if values_path[: len(base)] != base and base[: len(values_path)] != values_path:
        return None
    if len(base) > len(values_path):
        return None  # a write inside one value: the producer passed a single cell
    current: Any = value
    for token in values_path[len(base) :]:
        current = current.get(token) if isinstance(current, dict) else None
    if not isinstance(current, list):
        return None
    for index, item in enumerate(current):
        if isinstance(item, str) and _path_candidate(item) is not None:
            exported = _export(exporter, item, component_id=component_id, prop=prop)
            if isinstance(exported, dict):
                return exported
            current[index] = exported
    return None


def export_bound_values(
    app: "FastAPI",
    session_id: str,
    components: list[dict[str, Any]],
    path: str,
    value: Any,
) -> tuple[Any, list[dict[str, Any]]] | dict[str, Any]:
    """``value`` (written at ``path``) with each bound file in it exported and checked.

    Returns the rewritten value and the exported entries, or a refusal.
    """

    value = copy.deepcopy(value)
    base = [] if path in ("", "/") else _tokens(path)
    exporter = _Exporter(app, session_id)
    for component_id, prop, pointer in file_bindings(components):
        tokens = _tokens(pointer)
        if pointer.startswith(_SELECTION):
            # A seeded selection names files the way the rows do, so it is exported
            # the same way and keeps matching the exported cells.
            if len(tokens) < 3 or tokens[2] != "values":
                continue
            stop = _export_selection_values(exporter, value, base, tokens[:3], component_id, prop)
            if stop is not None:
                return stop
            continue
        if tokens[: len(base)] != base:
            continue
        rest = tokens[len(base) :]
        holder: Any = None
        current: Any = value
        for token in rest:
            holder = current
            if isinstance(current, dict):
                current = current.get(token)
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                current = current[int(token)]
            else:
                current = None
                break
        exported = _export(exporter, current, component_id=component_id, prop=prop)
        if isinstance(exported, dict):
            return exported
        if exported is current:
            continue
        if not rest:
            value = exported
        elif isinstance(holder, dict):
            holder[rest[-1]] = exported
        elif isinstance(holder, list):
            holder[int(rest[-1])] = exported
    return value, exporter.exported


__all__ = [
    "FILE_KEYS",
    "VIEWER_FILE_KEYS",
    "export_bound_values",
    "export_selection_rows",
    "file_bindings",
]
