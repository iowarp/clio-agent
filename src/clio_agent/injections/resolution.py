"""Resolving and rendering injections: core's, the session's plugins', a person's edits.

An injection (:mod:`clio_agent.injections.declarations`) is resolved for a session:

* its declared text and settings (core's ship in the core plugin; a plugin's in its own
  ``ai.iowarp.clio/injections``);
* overridden by a person's copy, saved through the prompt registry under
  ``clio.reminder.<name>`` (the Reminders tab: per session, workspace or user). A copy
  rewords it (a text edit) or only switches it off (``x_active: false``) or changes a
  setting (``x_param_<name>``) as a settings overlay that keeps the shipped text; a core
  injection that carries data the agent needs (``x_required``) can be reworded but not
  switched off. A plugin's ``x_required`` is ignored.

:func:`render` fires a core event: the core injection's text (unless switched off) and
every active plugin injection attached to the same event, rendered against the event's
variables and the session state they read. A core or person's template that cannot
render raises :class:`~clio_agent.injections.template.InjectionTemplateError` (a bug to
fix, never empty text); a plugin's is skipped with the typed reason
``injection_plugin_render_failed`` on the trace and stream audit. :func:`fire` fires a
general event (``turn_start``, ``tool_result``) and returns each active plugin
injection's text with its source. A switched-off injection is traced with
:data:`INJECTION_INACTIVE`.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.injections.declarations import PREFIX, Injection, core_injections
from clio_agent.injections.events import STATE
from clio_agent.injections.template import InjectionTemplateError, names_used, render_text
from clio_agent.runtime import trace

__all__ = [
    "INJECTION_INACTIVE",
    "PLUGIN_RENDER_FAILED",
    "Resolved",
    "active",
    "fire",
    "injection",
    "param",
    "render",
    "update",
]

#: Typed trace reason: an injection was dropped because it is switched off.
INJECTION_INACTIVE = "injection_inactive"
#: Typed reason: a plugin's injection could not render and was left out.
PLUGIN_RENDER_FAILED = "injection_plugin_render_failed"
_OFF = frozenset({"false", "0", "no", "off"})
#: Resolved injections per registry state (its roots and the override files' times),
#: session and plugin set (the session's pinned resolution digest).
_CACHE: dict[tuple[Any, ...], dict[str, "Resolved"]] = {}
_CACHE_LIMIT = 64


@dataclass(frozen=True)
class Resolved:
    """One injection as a session sees it."""

    name: str
    event: str
    text: str
    plugin: str = "core"
    tool: str = ""
    active: bool = True
    trigger: str = ""
    variables: tuple[str, ...] = ()
    params: dict[str, str] = field(default_factory=dict)
    scope: str = "builtin"
    required: str = ""


def _registry(app: Any, session_id: str, write_scope: str = "global") -> Any:
    for_request = getattr(getattr(app, "state", None), "prompt_registry_for_request", None)
    if callable(for_request):
        return for_request(session_id=session_id, write_scope=write_scope)
    from clio_agent.prompts import PromptRegistry  # noqa: PLC0415

    return PromptRegistry()  # the user's and the working folder's prompts (CLI, settings)


def _context(app: Any, session_id: str) -> tuple[Any, str]:
    if app is None:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        app, session_id = _ctx.active_app(), session_id or _ctx.active_session_id() or ""
    return app, session_id


def _signature(registry: Any) -> tuple[Any, ...]:
    from clio_agent.prompts import _prompt_files  # noqa: PLC0415

    files: list[tuple[str, int]] = []
    for source in registry.sources:
        for path in _prompt_files(source.root):
            try:
                files.append((str(path), path.stat().st_mtime_ns))
            except OSError:  # removed between the listing and the stat: not a prompt now
                continue
    return (id(registry.builtins), tuple(files))


def _effective(row: Injection, registry: Any) -> Resolved:
    """``row`` with a person's saved copy applied (none: as declared).

    A copy is a text edit (its own body) or a settings overlay (the switch and settings
    only, :mod:`clio_agent.prompts`): an overlay keeps the declared text. ``x_required``
    counts only for core: a plugin cannot make its own text impossible to switch off.
    """
    resolved = registry.resolve(row.id)
    meta: Mapping[str, Any] = (resolved.metadata or {}) if resolved is not None else {}
    own_text = resolved is not None and resolved.scope != "builtin" and bool(resolved.text.strip())
    if own_text and resolved is not None:
        scope = resolved.scope
    elif meta.get("settings_scope") or meta.get("overlay"):
        scope = str(meta.get("settings_scope") or getattr(resolved, "scope", "") or "builtin")
    else:
        scope = "builtin"
    params = dict(row.params)
    params.update(
        {k.removeprefix("param_"): str(v) for k, v in meta.items() if k.startswith("param_")}
    )
    switch = str(meta.get("active", "true" if row.active else "false")).strip().lower()
    required = row.required if row.plugin == "core" else ""
    return Resolved(
        name=row.name,
        event=row.event,
        text=resolved.text if own_text and resolved is not None else row.text,
        plugin=row.plugin,
        tool=row.tool,
        active=bool(required) or switch not in _OFF,
        trigger=row.trigger,
        variables=row.variables,
        params=params,
        scope=scope,
        required=required,
    )


def _rows(app: Any, session_id: str) -> dict[str, Injection]:
    from clio_agent.injections.plugin_set import plugin_injections  # noqa: PLC0415

    return {**core_injections(), **{r.name: r for r in plugin_injections(app, session_id)}}


def _resolved_all(app: Any, session_id: str) -> dict[str, Resolved]:
    """Every injection the session sees, rebuilt whenever an override file or the
    session's plugin set changes (a removed plugin's injections are gone, an updated
    plugin's are read again)."""
    from clio_agent.injections.plugin_set import plugin_set_key  # noqa: PLC0415

    registry = _registry(app, session_id)
    key = (_signature(registry), session_id, plugin_set_key(app, session_id))
    rows = _CACHE.get(key)
    if rows is None:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        rows = {name: _effective(row, registry) for name, row in _rows(app, session_id).items()}
        _CACHE[key] = rows
    return rows


def injection(name: str, *, app: Any = None, session_id: str = "") -> Resolved | None:
    """The injection ``name`` as this session sees it, or ``None`` when none exists."""
    app, session_id = _context(app, session_id)
    return _resolved_all(app, session_id).get(name)


def update(
    name: str,
    *,
    active: bool | None = None,
    params: Mapping[str, Any] | None = None,
    app: Any = None,
    session_id: str = "",
) -> Resolved:
    """Save the person's switch or settings for ``name`` (a core or plugin injection).

    Only the switch and the settings are stored, as the person's global settings overlay
    (:mod:`clio_agent.prompts`), never the text: a later release's wording still reaches
    them. A text edit the person saved globally on the Prompts page is kept, re-saved with
    the new settings. ``session_id`` finds a plugin's injection (plugins are per session).

    Raises:
        LookupError: no injection ``name`` exists (for this session).
    """
    if injection(name, app=app, session_id=session_id) is None:
        raise LookupError(f"no injection {name!r} ({PREFIX}{name})")
    registry = _registry(app, "", "global")
    resolved = registry.resolve(f"{PREFIX}{name}")
    meta = dict((resolved.metadata or {}) if resolved is not None else {})
    settings: dict[str, Any] = {
        key: value for key, value in meta.items() if key == "active" or key.startswith("param_")
    }
    if active is not None:
        settings["active"] = "true" if active else "false"
    for key, value in (params or {}).items():
        settings[f"param_{key}"] = str(value)
    edited = resolved is not None and resolved.scope == "global" and resolved.text.strip()
    if edited and resolved is not None:
        kept = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
        registry.save(
            resolved.id,
            text=resolved.text,
            profile=resolved.profile or "default",
            title=resolved.title,
            description=resolved.description,
            metadata={**kept, **settings},
        )
    else:
        registry.save(f"{PREFIX}{name}", text="", metadata=settings)
    found = injection(name, app=app, session_id=session_id)
    if found is None:  # pragma: no cover - a save is read straight back
        raise LookupError(f"injection {name!r} was not saved")
    return found


def active(name: str, *, app: Any = None, session_id: str = "") -> bool:
    """Whether an injection from ``name`` may be recorded (none declared: yes)."""
    found = injection(name, app=app, session_id=session_id)
    if found is None or found.active:
        return True
    trace.event("INJECTION", "%s name=%s scope=%s", INJECTION_INACTIVE, name, found.scope)
    return False


def param(name: str, key: str, default: Any, cast: Any = str, **where: Any) -> Any:
    """An injection's own setting (``x_param_<key>``), else ``default``."""
    found = injection(name, **where)
    raw = found.params.get(key) if found is not None else None
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        trace.event("INJECTION", "injection_param_invalid name=%s %s=%r", name, key, raw)
        return default


def _variables(text: str, given: Mapping[str, Any], app: Any, sid: str, name: str) -> dict:
    from clio_agent.injections.state import state_variables  # noqa: PLC0415

    wanted = (names_used(text, name) & STATE) - set(given)
    return {**state_variables(wanted, app, sid), **given} if wanted else dict(given)


def _plugin_texts(
    rows: list[Resolved], variables: Mapping[str, Any], app: Any, sid: str
) -> list[tuple[str, str]]:
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    out: list[tuple[str, str]] = []
    for row in rows:
        if not row.active:
            trace.event("INJECTION", "%s name=%s scope=%s", INJECTION_INACTIVE, row.name, row.scope)
            continue
        try:
            text = render_text(
                row.text, _variables(row.text, variables, app, sid, row.name), row.name
            )
        except InjectionTemplateError as exc:
            trace.event(
                "INJECTION", "%s name=%s problem=%s", PLUGIN_RENDER_FAILED, row.name, exc.problem
            )
            stream_audit(PLUGIN_RENDER_FAILED, injection=row.name, problem=exc.problem)
            continue
        if text:
            out.append((row.name, text))
    return out


def render(
    name: str, variables: Mapping[str, Any], *, app: Any = None, session_id: str = ""
) -> str | None:
    """Fire core event ``name``: its text and its plugin additions, ``None`` when all off.

    Raises:
        LookupError: ``name`` is not a core injection (an injection never silently
            loses its text).
        InjectionTemplateError: the core (or a person's) template cannot render.
    """
    app, session_id = _context(app, session_id)
    rows = _resolved_all(app, session_id)
    found = rows.get(name)
    if found is None or found.plugin != "core":
        raise LookupError(f"no core injection {name!r} ({PREFIX}{name})")
    parts: list[str] = []
    if found.active:
        given = _variables(found.text, variables, app, session_id, name)
        parts.append(render_text(found.text, given, name))
    else:
        trace.event("INJECTION", "%s name=%s scope=%s", INJECTION_INACTIVE, name, found.scope)
    extra = [r for r in rows.values() if r.plugin != "core" and r.event == name]
    parts.extend(text for _src, text in _plugin_texts(extra, variables, app, session_id))
    joined = "\n\n".join(part for part in parts if part)
    return joined if (found.active or joined) else None


def fire(
    event: str,
    variables: Mapping[str, Any],
    *,
    app: Any = None,
    session_id: str = "",
    tool: str = "",
) -> list[tuple[str, str]]:
    """Fire general event ``event``: ``(source, text)`` of each active injection on it.

    ``tool`` narrows a ``tool_result`` event to the declarations whose ``tool`` pattern
    matches it (no pattern: every tool).
    """
    app, session_id = _context(app, session_id)
    rows = [
        row
        for row in _resolved_all(app, session_id).values()
        if row.event == event and (not row.tool or fnmatch.fnmatchcase(tool, row.tool))
    ]
    return _plugin_texts(rows, variables, app, session_id)
