"""Plugin injections as prompt rows, so the Reminders tab lists and switches them too.

Core's injections are the prompt registry's built-in prompts
(:func:`clio_agent.prompts.builtin_prompt_definitions`). A plugin's are declared in its
own ``ai.iowarp.clio/injections`` and apply to the sessions that use the plugin; this
module turns every installed plugin's declarations into the same kind of row
(``clio.reminder.<plugin>.<name>``, ``x_kind: reminder``, ``plugin: <id>``), so a
person sees them beside core's and can switch one off or change a setting with a
settings overlay, as for core's. A plugin cannot mark its injection required.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace

__all__ = ["PLUGIN_REMINDERS_UNAVAILABLE", "plugin_reminders", "with_plugin_reminders"]

#: Typed reason: the installed plugins could not be read, so their reminders are not listed.
PLUGIN_REMINDERS_UNAVAILABLE = "plugin_reminders_unavailable"


def plugin_reminders(app: Any) -> dict[str, Any]:
    """Every installed plugin's injections as prompt definitions, by id."""
    from clio_agent.injections.events import events  # noqa: PLC0415
    from clio_agent.injections.plugin_set import _read  # noqa: PLC0415
    from clio_agent.prompts import PromptDefinition, PromptProfile, _checksum  # noqa: PLC0415

    service = getattr(getattr(app, "state", None), "plugin_service", None)
    if service is None:
        return {}
    try:
        records = service.store.records()
    except (OSError, ValueError) as exc:
        trace.event("INJECTION", "%s: %s", PLUGIN_REMINDERS_UNAVAILABLE, exc)
        return {}
    catalogue = events()
    rows: dict[str, Any] = {}
    for record in records:
        if record.id == "core" or not record.root:
            continue
        for row in _read(str(record.root), record.id, str(record.version)):
            event = catalogue.get(row.event)
            meta = {
                "kind": "reminder",
                "plugin": record.id,
                "event": row.event,
                "trigger": row.trigger or (event.when if event is not None else row.event),
                "variables": ", ".join(row.variables),
                "active": "true" if row.active else "false",
                **{f"param_{key}": value for key, value in row.params.items()},
            }
            profile = PromptProfile(
                name="default",
                text=row.text,
                scope="builtin",
                source_path=row.path,
                checksum=_checksum(row.text),
                metadata=dict(meta),
            )
            rows[row.id] = PromptDefinition(
                id=row.id,
                title=row.title or row.name,
                description=row.description,
                profiles={"default": profile},
                scope="builtin",
                source_path=row.path,
                metadata=meta,
            )
    return rows


def with_plugin_reminders(registry: Any, app: Any) -> Any:
    """``registry`` (a per-request one) with the plugins' injections among its built-ins."""
    registry.builtins = {**registry._builtins(), **plugin_reminders(app)}
    return registry
