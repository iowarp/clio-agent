"""A saved connector connection follows its catalog entry when the entry changes.

Enabling a connector writes its entry into the person's ``mcp.yaml`` as a template
(:func:`clio_agent.connectors.materialize.declaration`). A conversation's server is
built from the entry as it is now (``fill_declaration(..., from_entry=True)``), but the
sign-in address (``materialize.in_use``), the person's ``mcp.yaml`` and CLIO's server
settings read the saved copy, so after a release that repins a server or moves its
address they disagreed with what runs. :func:`refresh` rewrites the copy when it no
longer matches, with the typed reason :data:`CONNECTION_REFRESHED` and the fields that
changed in the trace. The Extensions listing calls it for every enabled connector. The person's values (connector state) and secrets (credential store) are not in
the template and are untouched; a sign-in's own block is kept.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from clio_agent.connectors import materialize
from clio_agent.runtime import trace

#: Typed reason in the trace when a saved connection is rewritten from its entry.
CONNECTION_REFRESHED = "connector_connection_refreshed"
#: The parts of a saved connection that come from the entry.
TEMPLATE_FIELDS = ("transport", "command", "args", "env", "url", "headers")


def drift(saved: Mapping[str, Any], entry: Any) -> tuple[str, ...]:
    """The template fields where ``saved`` differs from ``entry``'s declaration now."""

    current = materialize.declaration(entry)
    return tuple(field for field in TEMPLATE_FIELDS if saved.get(field) != current.get(field))


def refresh(
    entry: Any, saved: Mapping[str, Any] | None, save: Callable[[], Any]
) -> tuple[str, ...]:
    """Rewrite ``entry``'s saved connection with ``save`` when it has drifted.

    Returns the fields that changed (empty when the copy matches or there is none).
    """

    if not saved or saved.get(materialize.CONNECTOR_KEY) != entry.id:
        return ()
    changed = drift(saved, entry)
    if changed:
        save()
        trace.event(
            "TOOLS",
            "%s connector=%s fields=%s",
            CONNECTION_REFRESHED,
            entry.id,
            ",".join(changed),
        )
    return changed


__all__ = ["CONNECTION_REFRESHED", "TEMPLATE_FIELDS", "drift", "refresh"]
