"""Reminders: the text CLIO adds to what an agent reads, as Markdown templates.

Every injection has a source (``plan``, ``explore_nudge``, ``task_result``…). Its text
is the prompt ``clio.reminder.<source>`` in the prompt registry (built-ins under
``prompt_packs/builtin/reminders/``), so a person can switch it off, edit it or override
it per workspace or session from the Prompts page (docs/design/sdk/reminders.md).

Frontmatter (flat, so the page's save round-trips it): ``x_kind: reminder``,
``x_active`` (``false`` switches it off), ``x_trigger`` (when it fires, for people),
``x_variables`` (comma-separated: the only ``{{placeholders}}`` the body may use) and
``x_param_<name>`` (a setting the code reads, such as a threshold) and ``x_required``
(a reason, for a source that carries data the agent needs, such as a finished child's
result: it cannot be switched off, only reworded).

The code decides *when* a reminder fires and supplies its variables; the template is
*what* it says. :func:`render` fills the variables (text only; nothing runs).
:func:`active` gates every injection at the two places they are recorded, so even a
source whose text is still built in code can be switched off; a switched-off reminder is
traced with :data:`REMINDER_INACTIVE`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.runtime import trace

__all__ = [
    "REMINDER_INACTIVE",
    "REMINDER_PREFIX",
    "Reminder",
    "active",
    "param",
    "reminder",
    "render",
    "update",
]

REMINDER_PREFIX = "clio.reminder."
#: Typed trace reason: an injection was dropped because its reminder is switched off.
REMINDER_INACTIVE = "reminder_inactive"
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
_OFF = frozenset({"false", "0", "no", "off"})
#: Resolved reminders per registry state (its roots, built-ins and the override files'
#: modification times): injections are checked on every recorded step.
_CACHE: dict[tuple[Any, ...], dict[str, Reminder | None]] = {}
_CACHE_LIMIT = 64


@dataclass(frozen=True)
class Reminder:
    """One resolved reminder template."""

    source: str
    text: str
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


def _resolve(registry: Any, source: str) -> Reminder | None:
    resolved = registry.resolve(f"{REMINDER_PREFIX}{source}")
    if resolved is None:
        return None
    meta: Mapping[str, Any] = resolved.metadata or {}
    variables = tuple(
        name.strip() for name in str(meta.get("variables") or "").split(",") if name.strip()
    )
    params = {
        key.removeprefix("param_"): str(value)
        for key, value in meta.items()
        if key.startswith("param_")
    }
    required = str(meta.get("required") or "").strip()
    return Reminder(
        source=source,
        text=resolved.text,
        active=bool(required) or str(meta.get("active", "true")).strip().lower() not in _OFF,
        trigger=str(meta.get("trigger") or ""),
        variables=variables,
        params=params,
        scope=resolved.scope,
        required=required,
    )


def reminder(source: str, *, app: Any = None, session_id: str = "") -> Reminder | None:
    """The reminder for ``source`` as this session sees it, or ``None`` when none exists."""
    app, session_id = _context(app, session_id)
    registry = _registry(app, session_id)
    key = _signature(registry)
    rows = _CACHE.get(key)
    if rows is None:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        rows = _CACHE[key] = {}
    if source not in rows:
        rows[source] = _resolve(registry, source)
    return rows[source]


def update(
    source: str,
    *,
    active: bool | None = None,
    params: Mapping[str, Any] | None = None,
    app: Any = None,
) -> Reminder:
    """Save the person's switch or settings for ``source`` as their own (global) copy.

    The text and the other settings are kept, so a reminder edited on the Prompts page
    keeps its wording when the Fleet page turns it off.
    """
    registry = _registry(app, "", "global")
    resolved = registry.resolve(f"{REMINDER_PREFIX}{source}")
    if resolved is None:
        raise LookupError(f"no reminder template for {source!r} ({REMINDER_PREFIX}{source})")
    metadata = {
        key: value
        for key, value in (resolved.metadata or {}).items()
        if isinstance(value, (str, int, float, bool))
    }
    if active is not None:
        metadata["active"] = "true" if active else "false"
    for name, value in (params or {}).items():
        metadata[f"param_{name}"] = str(value)
    registry.save(
        resolved.id,
        text=resolved.text,
        profile=resolved.profile or "default",
        title=resolved.title,
        description=resolved.description,
        metadata=metadata,
    )
    found = reminder(source, app=app)
    if found is None:  # pragma: no cover - a save is read straight back
        raise LookupError(f"reminder {source!r} was not saved")
    return found


def active(source: str, *, app: Any = None, session_id: str = "") -> bool:
    """Whether an injection from ``source`` may be recorded (no template: yes)."""
    found = reminder(source, app=app, session_id=session_id)
    if found is None or found.active:
        return True
    trace.event("REMINDER", "%s source=%s scope=%s", REMINDER_INACTIVE, source, found.scope)
    return False


def param(source: str, name: str, default: Any, cast: Any = str, **where: Any) -> Any:
    """A reminder's own setting (``x_param_<name>``), else ``default``."""
    found = reminder(source, **where)
    raw = found.params.get(name) if found is not None else None
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        trace.event("REMINDER", "reminder_param_invalid source=%s %s=%r", source, name, raw)
        return default


def render(
    source: str, variables: Mapping[str, Any], *, app: Any = None, session_id: str = ""
) -> str | None:
    """The reminder's text with its variables filled in; ``None`` when it is switched off.

    A source must ship a built-in template; without one this raises, so a reminder can
    never silently lose its text. A placeholder the code did not supply stays as written.
    """
    found = reminder(source, app=app, session_id=session_id)
    if found is None:
        raise LookupError(f"no reminder template for {source!r} ({REMINDER_PREFIX}{source})")
    if not found.active:
        trace.event("REMINDER", "%s source=%s scope=%s", REMINDER_INACTIVE, source, found.scope)
        return None

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(variables[name]) if name in variables else match.group(0)

    return _PLACEHOLDER.sub(fill, found.text).strip()
