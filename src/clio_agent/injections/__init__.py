"""Injections: everything CLIO adds to what an agent reads, declared as data.

One system for core and plugins. An injection is a Markdown declaration (text plus
frontmatter) attached to an event; the code decides only *when* an event fired and what
it knows then, never what the agent is told:

* :mod:`~clio_agent.injections.declarations` -- the declaration format; core's ship in
  the core plugin, a plugin's in its ``ai.iowarp.clio/injections`` (listed on the
  Reminders tab by :mod:`~clio_agent.injections.listing`);
* :mod:`~clio_agent.injections.events` -- the event catalogue and the session state a
  template may read;
* :mod:`~clio_agent.injections.template` -- sandboxed Jinja (variables, ternaries,
  ``{% if %}``; an unknown name is a typed error, nothing runs);
* :mod:`~clio_agent.injections.resolution` -- a session's view (core, its plugins, a
  person's text edits and settings overlays from the Reminders tab), :func:`render`,
  :func:`fire` and :func:`update`;
* :mod:`~clio_agent.injections.notes` -- what the harness told the agent about one tool
  call, collected so the user sees it too;
* :mod:`~clio_agent.injections.marks` -- how the recorder treats a text: an event note
  recorded at each firing (:func:`fired`), or state the agent already holds
  (:func:`held_by`).

A leaf for ``tools/``: nothing here imports ``gact`` at import time.
"""

from __future__ import annotations

from clio_agent.injections.declarations import PREFIX, Injection
from clio_agent.injections.marks import fired, held_by
from clio_agent.injections.notes import collect, note
from clio_agent.injections.resolution import (
    INJECTION_INACTIVE,
    PLUGIN_RENDER_FAILED,
    Resolved,
    active,
    fire,
    injection,
    param,
    render,
    update,
)
from clio_agent.injections.template import InjectionTemplateError

__all__ = [
    "INJECTION_INACTIVE",
    "PLUGIN_RENDER_FAILED",
    "PREFIX",
    "Injection",
    "InjectionTemplateError",
    "Resolved",
    "active",
    "collect",
    "fire",
    "fired",
    "held_by",
    "injection",
    "note",
    "param",
    "render",
    "update",
]
