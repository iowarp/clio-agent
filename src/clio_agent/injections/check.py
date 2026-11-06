"""``clio-agent plugin check`` for a plugin's injections: event, names and template.

Each ``ai.iowarp.clio/injections/*.md`` must name an event of the catalogue
(:func:`~clio_agent.injections.events.events`), parse as a template, and read only its
event's variables and the session state. Findings are typed diagnostics, so a broken
injection is reported when the plugin is checked, not first at the turn it fires.
"""

from __future__ import annotations

from pathlib import Path

from clio_agent.injections.declarations import read_dir
from clio_agent.injections.events import STATE, events
from clio_agent.injections.template import InjectionTemplateError, names_used
from clio_agent.plugins.records import Diagnostic, PluginRecord

__all__ = ["injection_diagnostics"]


def injection_diagnostics(record: PluginRecord) -> list[Diagnostic]:
    """The typed findings for ``record``'s injection declarations (none: ``[]``)."""
    folder = Path(record.root) / "ai.iowarp.clio" / "injections"
    catalogue = events()
    found: list[Diagnostic] = []
    for row in read_dir(folder, plugin=record.id):
        event = catalogue.get(row.event)
        if event is None:
            message = f"{row.name}: unknown event {row.event!r}"
            found.append(Diagnostic("error", "injection_event_unknown", message, record.id))
            continue
        if row.required:
            message = f"{row.name}: x_required is core's alone; a person can switch it off"
            found.append(Diagnostic("warning", "injection_required_ignored", message, record.id))
        if row.tool and row.event != "tool_result":
            message = f"{row.name}: tool: only narrows the tool_result event"
            found.append(Diagnostic("warning", "injection_tool_ignored", message, record.id))
        try:
            unknown = sorted(names_used(row.text, row.name) - set(event.variables) - STATE)
        except InjectionTemplateError as exc:
            message = f"{row.name}: {exc.problem}"
            found.append(Diagnostic("error", "injection_template_invalid", message, record.id))
            continue
        if unknown:
            message = f"{row.name}: {row.event} does not supply {', '.join(unknown)}"
            found.append(Diagnostic("error", "injection_variable_unknown", message, record.id))
    return found
