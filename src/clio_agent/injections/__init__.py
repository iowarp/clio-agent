"""Injections: everything CLIO adds to what an agent reads, declared as data.

One system for core and plugins. An injection is a Markdown declaration (text plus
frontmatter) attached to an event; the code decides only *when* an event fired and what
it knows then, never what the agent is told:

* :mod:`~clio_agent.injections.declarations` -- the declaration format; core's ship in
  the core plugin, a plugin's in its ``ai.iowarp.clio/injections``;
* :mod:`~clio_agent.injections.events` -- the event catalogue and the session state a
  template may read;
* :mod:`~clio_agent.injections.template` -- sandboxed Jinja (variables, ternaries,
  ``{% if %}``; an unknown name is a typed error, nothing runs);
* :mod:`~clio_agent.injections.resolution` -- a session's view (core, its plugins, a
  person's edits from the Prompts page), :func:`render` and :func:`fire`;
* :mod:`~clio_agent.injections.notes` -- what the harness told the agent about one tool
  call, collected so the user sees it too.

A leaf for ``tools/``: nothing here imports ``gact`` at import time.
"""

from __future__ import annotations

from clio_agent.injections.declarations import PREFIX, Injection
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
    "injection",
    "note",
    "param",
    "render",
    "update",
]
