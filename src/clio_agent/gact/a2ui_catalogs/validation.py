"""Catalog-aware component/value validation: JSON Schema plus the CLIO safety walk.

Two layers, both catalog-aware:

1. **Schema** (:func:`validate_components`) — the official per-component
   ``jsonschema`` validators compiled by ``clio_schemas.a2ui.validation`` from
   the SURFACE's own catalog file. This replaces the old closed
   ``A2UIComponent`` pydantic union: an unknown component name, a wrong
   type, an out-of-range bound, an unregistered ``functionCall`` target
   inside a schema-reachable slot (e.g. a ``TextField`` check) are all
   rejected here, with a message naming the component, its id, and the
   JSON Pointer to the failing property.
2. **Safety walk** (:func:`validate_value`) — CLIO's own trust policy,
   moved verbatim from ``gact/a2ui.py`` (docs/design/a2ui-compat-campaign-
   2026-09.md S2): forbidden presentation/executable keys, a literal-only
   URL scheme allowlist, string/nesting bounds, and a Mermaid directive
   scan. It runs over the WHOLE server-message payload (not just
   ``updateComponents``), including zones no per-component schema reaches
   (action ``context``, ``updateDataModel`` values) — schema validation
   alone cannot see those. Its one catalog-aware change: a ``functionCall``
   (any mapping carrying a ``call`` key) is allowed iff the name is
   declared in the resolved catalog's ``functions`` map; previously every
   function call was unconditionally rejected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from jsonschema.exceptions import ValidationError, best_match

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry


class A2UIValidationError(ValueError):
    """Raised when an A2UI message crosses the trusted catalog boundary.

    Re-declared here (rather than imported from ``gact/a2ui.py``) to avoid a
    circular import — ``gact/a2ui.py`` imports THIS module. ``gact/a2ui.py``
    re-exports this exact class as its own ``A2UIValidationError`` so callers
    outside this package never see the split.
    """


_FORBIDDEN_KEYS = frozenset(
    {
        "css",
        "style",
        "styles",
        "html",
        "rawHtml",
        "dangerouslySetInnerHTML",
        "srcdoc",
        "script",
        "imports",
        "command",
        "commands",
        "eventHandlers",
        "onClick",
        "onChange",
    }
)
_URL_KEYS = frozenset({"url", "uri", "datauri"})
_MERMAID_EXECUTABLE_PATTERN = re.compile(
    r"<|%%\{|\bclick\b|\bhref\b|javascript:|data:text/html|url\s*\(",
    re.IGNORECASE,
)


def _most_specific_error(errors: list[ValidationError]) -> ValidationError:
    """Return the most useful top-level error to report.

    ``jsonschema.exceptions.best_match``'s relevance heuristic does not
    prefer a deeper failing path over a shallow one -- for an ``allOf`` +
    ``unevaluatedProperties`` composition (every hand-authored/canonicalised
    component in the catalog), a nested bound violation (e.g. a map point's
    ``latitude`` out of range) produces BOTH the specific nested error and a
    shallow, confusing "unevaluated properties" error (the failed branch's
    properties never got counted as evaluated); ``best_match`` alone can pick
    the latter. Preferring the error with the longest ``absolute_path`` picks
    the specific one; ``best_match`` still breaks ties and descends into a
    single error's own ``context`` (e.g. a ``oneOf`` cross-field check),
    where a deeper path is not available at all.
    """

    if len(errors) == 1:
        return best_match(errors)
    deepest = max(len(error.absolute_path) for error in errors)
    candidates = [error for error in errors if len(error.absolute_path) == deepest]
    return candidates[0] if len(candidates) == 1 else best_match(candidates)


def validate_components(entry: "CatalogEntry", components: Any, *, max_components: int) -> None:
    """Validate a full ``updateComponents`` list against one catalog's schemas.

    Args:
        entry: The surface's resolved catalog entry.
        components: The raw ``components`` array from the message.
        max_components: Bound on the list length (caller resolves config).

    Raises:
        A2UIValidationError: If the list shape is invalid, a component names
            an unimplemented type, or a component fails its schema — the
            message names ``component=<name> id=<id> pointer=<json-pointer>``
            (from ``jsonschema.exceptions.best_match``) plus the schema
            failure text.
    """

    if not isinstance(components, list) or not 1 <= len(components) <= max_components:
        raise A2UIValidationError("A2UI components must be a non-empty bounded list")
    for component in components:
        if not isinstance(component, Mapping):
            raise A2UIValidationError("A2UI component must be an object")
        name = str(component.get("component") or "")
        validator = entry.validators.get(name)
        if validator is None:
            raise A2UIValidationError(
                f"A2UI component is not in catalog {entry.catalog_id}: {name}"
            )
        errors = list(validator.iter_errors(component))
        if errors:
            failure = _most_specific_error(errors)
            pointer = "/" + "/".join(str(part) for part in failure.absolute_path)
            comp_id = str(component.get("id") or "")
            raise A2UIValidationError(
                f"component={name} id={comp_id} pointer={pointer}: {failure.message}"
            )


def _validate_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"https", "artifact", "resource"}:
        raise A2UIValidationError("A2UI URLs must use an allowed non-executable scheme")


def _validate_function_call(value: Mapping[str, Any], *, entry: "CatalogEntry | None") -> None:
    """A ``{"call": name, ...}`` object is allowed iff its catalog declares it."""

    name = str(value.get("call") or "")
    functions = entry.file.get("functions", {}) if entry is not None else {}
    if name not in functions:
        catalog_id = entry.catalog_id if entry is not None else "<unresolved>"
        raise A2UIValidationError(f"A2UI function is not in catalog {catalog_id}: {name}")


def validate_value(
    value: Any,
    *,
    entry: "CatalogEntry | None",
    key: str = "",
    depth: int = 0,
    max_depth: int,
    max_string: int,
    free_form: bool = False,
) -> None:
    """Walk one A2UI value, enforcing CLIO's catalog-aware safety policy.

    ``free_form`` marks a subtree that is an action/event ``context`` —
    arbitrary producer/client DATA, not renderable structure. The SAFETY
    rules (forbidden keys, function calls, URL literals, string/nesting
    bounds) still apply there; only structural Action-envelope shape rules
    (none remain in this walk — see ``gact/a2ui.py``) would be lifted.

    Args:
        value: The (sub)value to walk.
        entry: The message's resolved catalog entry, if known (``None`` for
            zones validated before a catalog can be resolved, e.g. a
            ``createSurface`` whose own catalogId is still being checked).
        key: The property name ``value`` was reached under (for URL-key and
            binding-path checks).
        depth: Current recursion depth.
        max_depth: Nesting bound (caller resolves config once).
        max_string: Per-string character bound (caller resolves config once).
        free_form: Whether this subtree is action/event context data.

    Raises:
        A2UIValidationError: On any safety-policy violation.
    """

    if depth > max_depth:
        raise A2UIValidationError("A2UI value exceeds the nesting limit")
    if isinstance(value, str):
        if len(value) > max_string:
            raise A2UIValidationError("A2UI string exceeds the size limit")
        if key.lower() in _URL_KEYS:
            _validate_url(value)
        return
    if isinstance(value, list):
        for item in value:
            validate_value(
                item,
                entry=entry,
                key=key,
                depth=depth + 1,
                max_depth=max_depth,
                max_string=max_string,
                free_form=free_form,
            )
        return
    if not isinstance(value, Mapping):
        return
    if set(value) == {"path"}:
        # The official spec defines RELATIVE binding paths (no leading "/")
        # in collection/template scope -- e.g. a List template's "name"
        # resolves against the current item, not the surface root. Only
        # updateDataModel.path (gact/a2ui.py, a different rule) must be an
        # absolute JSON Pointer; a binding just needs a non-empty string.
        binding_path = value.get("path")
        if not isinstance(binding_path, str) or not binding_path:
            raise A2UIValidationError("A2UI data bindings must be a non-empty string path")
    if "call" in value and isinstance(value.get("call"), str):
        _validate_function_call(value, entry=entry)
    # Both envelope shapes that carry a free-form payload -- the ``event`` inside a
    # component's action and the client action message itself -- pair ``name`` with
    # ``context``. Everything under that ``context`` is producer/client data.
    carries_action_context = "name" in value and "context" in value
    for child_key, child_value in value.items():
        if not isinstance(child_key, str):
            raise A2UIValidationError("A2UI object keys must be strings")
        if child_key in _FORBIDDEN_KEYS:
            raise A2UIValidationError(f"A2UI property is prohibited: {child_key}")
        # The scheme allowlist (_validate_url, below) applies to LITERAL URL
        # strings only. A property like Image.url is a DynamicString, so a
        # bound value ({"path": "/productImage"}) or a declared functionCall
        # is legal here -- the renderer's kernel media/artifact components
        # enforce the same allowlist on the resolved value at render time and
        # report VALIDATION_FAILED (owner decision 11, S6 deliverable).
        validate_value(
            child_value,
            entry=entry,
            key=child_key,
            depth=depth + 1,
            max_depth=max_depth,
            max_string=max_string,
            free_form=free_form or (carries_action_context and child_key == "context"),
        )
    if str(value.get("component") or "") == "clio.mermaid.v1":
        source = value.get("source")
        if isinstance(source, str) and _MERMAID_EXECUTABLE_PATTERN.search(source):
            raise A2UIValidationError(
                "A2UI Mermaid source contains an executable or HTML directive"
            )


__all__ = [
    "A2UIValidationError",
    "validate_components",
    "validate_value",
]
