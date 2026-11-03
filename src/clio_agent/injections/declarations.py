"""Injection declarations: Markdown files with flat frontmatter, from core and plugins.

A plugin declares injections in ``ai.iowarp.clio/injections/*.md``; core's are the core
plugin's (``plugins/builtin/core/ai.iowarp.clio/injections/``). One file is one
injection::

    ---
    name: plan-pointer            # core files use ``id: clio.reminder.<name>``
    event: tool_result            # an event of :data:`clio_agent.injections.events.EVENTS`
    tool: run_code                # tool_result only: an fnmatch pattern on the tool name
    title: Point at the plan
    description: What it tells the agent and why.
    active: true                  # or x_active; a person can switch it off
    ---
    {% if plan.status == "approved" %}The approved plan is at {{ plan.location }}.{% endif %}

The body is a sandboxed Jinja template (:mod:`clio_agent.injections.template`). Keys may
carry the ``x_`` prefix the Prompts page writes (``x_active``, ``x_variables``,
``x_param_<name>``, ``x_required``, ``x_trigger``). A core injection's event is its own
name unless it says otherwise: the code that fires it is the event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["PREFIX", "Injection", "core_dir", "core_injections", "read_dir", "read_file"]

#: The prompt-registry id prefix a person's copy of an injection is saved under.
PREFIX = "clio.reminder."
_OFF = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class Injection:
    """One declared injection."""

    name: str
    event: str
    text: str
    plugin: str = "core"
    tool: str = ""
    title: str = ""
    description: str = ""
    trigger: str = ""
    variables: tuple[str, ...] = ()
    active: bool = True
    required: str = ""
    params: dict[str, str] = field(default_factory=dict)
    path: str = ""

    @property
    def id(self) -> str:
        """The prompt-registry id a person's edits are saved under."""
        return f"{PREFIX}{self.name}"


def _get(meta: dict[str, Any], key: str) -> Any:
    return meta.get(key, meta.get(f"x_{key}"))


def read_file(path: Path, *, plugin: str = "core") -> Injection:
    """Parse one declaration. A plugin injection's name is ``<plugin>.<name>``."""
    from clio_agent.prompts import _parse_frontmatter  # noqa: PLC0415 - one flat parser

    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    declared = str(meta.get("id") or "").removeprefix(PREFIX)
    own = str(meta.get("name") or declared or path.stem).strip()
    name = own if plugin == "core" else f"{plugin}.{own}"
    raw_vars = _get(meta, "variables") or ""
    items = raw_vars if isinstance(raw_vars, list) else str(raw_vars).split(",")
    return Injection(
        name=name,
        event=str(_get(meta, "event") or own).strip(),
        text=body.strip(),
        plugin=plugin,
        tool=str(_get(meta, "tool") or "").strip(),
        title=str(meta.get("title") or ""),
        description=str(meta.get("description") or ""),
        trigger=str(_get(meta, "trigger") or ""),
        variables=tuple(str(v).strip() for v in items if str(v).strip()),
        active=str(_get(meta, "active") if _get(meta, "active") is not None else "true")
        .strip()
        .lower()
        not in _OFF,
        required=str(_get(meta, "required") or "").strip(),
        params={
            key.removeprefix("x_").removeprefix("param_"): str(value)
            for key, value in meta.items()
            if key.removeprefix("x_").startswith("param_")
        },
        path=str(path),
    )


def read_dir(folder: Path, *, plugin: str) -> list[Injection]:
    """Every ``*.md`` declaration in ``folder`` (none when it does not exist)."""
    if not folder.is_dir():
        return []
    return [read_file(path, plugin=plugin) for path in sorted(folder.glob("*.md"))]


def core_dir() -> Path:
    """The core plugin's injection folder."""
    from clio_agent import plugins  # noqa: PLC0415

    return Path(plugins.__file__).parent / "builtin" / "core" / "ai.iowarp.clio" / "injections"


@lru_cache(maxsize=1)
def core_injections() -> dict[str, Injection]:
    """Core's injections by name (read once per process)."""
    return {row.name: row for row in read_dir(core_dir(), plugin="core")}
