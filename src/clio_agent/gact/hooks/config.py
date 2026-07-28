"""Hook configuration: the declarative flat-array config + matching (P2.2).

The config is a FLAT array of hook entries, each keyed by a REQUIRED STABLE ``id``
(not positional — the identity bug every surveyed CLI has; hooks-research §2.6).
An entry declares which events it runs ``on``, a ``match`` (anchored tool regex,
capability annotations, args regex), how to ``run`` it, its timeout, and its
per-hook fail-closed posture.

Discovery salvages the old registry's scope-awareness as the cleanest single
mechanism: one declarative JSON file, discovered at USER scope
(``<user_config_dir>/hooks.json``) then PROJECT scope (``<cwd>/.clio/hooks.json``),
merged so a project entry overrides a user entry with the same ``id`` (precedence
project > user). A malformed entry is dropped with a diagnostic naming its ``id``;
the rest of the file still loads (hooks-research M5/M6).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.gact.hooks.events import KNOWN_EVENTS
from clio_agent.gact.hooks.wire import HookEnvelope

logger = logging.getLogger(__name__)


class HookConfigError(ValueError):
    """A single hook entry was malformed. Carries the offending ``id`` when known."""

    def __init__(self, message: str, *, hook_id: str = "") -> None:
        super().__init__(message)
        self.hook_id = hook_id


@dataclass(frozen=True)
class HookMatch:
    """Match predicate for one hook entry.

    * ``tool`` — an ANCHORED regex (wrapped ``^...$``) against the tool name, so
      ``Edit`` does NOT match ``NotebookEdit`` (hooks-research §2.5 / M1).
    * ``annotations`` — capability match against the wire ``tool_annotations``
      block (``readOnly``/``destructive``/``openWorld``), covering MCP tools
      nobody enumerated (M3/M4).
    * ``args_pattern`` — an (unanchored) regex against the JSON-serialized
      ``tool_input``.

    An absent/empty match admits everything (the event alone selects the hook).
    """

    tool: re.Pattern[str] | None = None
    annotations: Mapping[str, bool] = field(default_factory=dict)
    args_pattern: re.Pattern[str] | None = None

    def matches(self, envelope: HookEnvelope) -> bool:
        """Return whether this predicate admits ``envelope``."""

        if self.tool is not None:
            name = envelope.tool_name or ""
            if not self.tool.match(name):
                return False
        if self.annotations:
            declared = envelope.tool_annotations or {}
            for key, want in self.annotations.items():
                if bool(declared.get(key)) != bool(want):
                    return False
        if self.args_pattern is not None:
            try:
                blob = json.dumps(envelope.tool_input or {}, sort_keys=True, default=str)
            except (TypeError, ValueError):
                blob = str(envelope.tool_input)
            if not self.args_pattern.search(blob):
                return False
        return True


@dataclass(frozen=True)
class HookRun:
    """How to invoke one hook (the adapter selector + its parameters)."""

    type: str  # "command" | "http" | "prompt"
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    prompt: str = ""


@dataclass(frozen=True)
class HookEntry:
    """One declarative hook, keyed by a stable ``id``."""

    id: str
    on: frozenset[str]
    match: HookMatch
    run: HookRun
    timeout_ms: int = 30000
    fail_closed: bool = False
    enabled: bool = True
    source: str = ""
    #: P2.5 bounded self-loops — the per-hook ``Stop`` re-entry cap (``loopLimit``).
    #: How many times THIS hook's ``Stop`` block may re-drive the turn within one
    #: stop-sequence before it stops being honored (R5). ``0``/negative means "no
    #: per-hook limit beyond the global cap" (the global ceiling still binds). Only
    #: consulted for ``Stop`` hooks; inert for every other event.
    loop_limit: int = 0

    @property
    def timeout_s(self) -> float:
        return max(self.timeout_ms, 0) / 1000.0

    def runs_for(self, event: str, envelope: HookEnvelope) -> bool:
        """Return whether this hook runs for ``event`` given ``envelope``."""

        return self.enabled and event in self.on and self.match.matches(envelope)


def _compile_anchored(pattern: str, *, hook_id: str) -> re.Pattern[str]:
    """Compile ``pattern`` anchored (``^...$``), raising a typed error on failure."""

    body = pattern
    if not body.startswith("^"):
        body = "^" + body
    if not body.endswith("$"):
        body = body + "$"
    try:
        return re.compile(body)
    except re.error as exc:
        raise HookConfigError(
            f"hook {hook_id!r}: invalid tool regex {pattern!r}: {exc}", hook_id=hook_id
        ) from exc


def _parse_entry(raw: Mapping[str, Any], *, source: str) -> HookEntry:
    """Parse + validate one raw entry into a :class:`HookEntry` (typed errors)."""

    hook_id = str(raw.get("id") or "").strip()
    if not hook_id:
        raise HookConfigError("hook entry is missing a required stable 'id'")
    on_raw = raw.get("on")
    if not isinstance(on_raw, Sequence) or isinstance(on_raw, (str, bytes)):
        raise HookConfigError(f"hook {hook_id!r}: 'on' must be a list of event names", hook_id=hook_id)
    events = {str(name) for name in on_raw}
    unknown = events - KNOWN_EVENTS
    if unknown:
        logger.warning(
            "[clio-hooks] hook %r references unknown event(s) %s; they will never fire",
            hook_id,
            sorted(unknown),
        )
    events &= KNOWN_EVENTS
    if not events:
        raise HookConfigError(
            f"hook {hook_id!r}: no known event in 'on'", hook_id=hook_id
        )

    match_raw = raw.get("match") or {}
    if not isinstance(match_raw, Mapping):
        raise HookConfigError(f"hook {hook_id!r}: 'match' must be an object", hook_id=hook_id)
    tool_pat = None
    if match_raw.get("tool"):
        tool_pat = _compile_anchored(str(match_raw["tool"]), hook_id=hook_id)
    annotations = {
        str(key): bool(value)
        for key, value in (match_raw.get("annotations") or {}).items()
    }
    args_pat = None
    if match_raw.get("argsPattern"):
        try:
            args_pat = re.compile(str(match_raw["argsPattern"]))
        except re.error as exc:
            raise HookConfigError(
                f"hook {hook_id!r}: invalid argsPattern: {exc}", hook_id=hook_id
            ) from exc

    run_raw = raw.get("run") or {}
    if not isinstance(run_raw, Mapping):
        raise HookConfigError(f"hook {hook_id!r}: 'run' must be an object", hook_id=hook_id)
    run_type = str(run_raw.get("type") or "command").lower()
    if run_type not in {"command", "http", "prompt"}:
        raise HookConfigError(
            f"hook {hook_id!r}: unsupported run.type {run_type!r}", hook_id=hook_id
        )
    if run_type == "command" and not str(run_raw.get("command") or "").strip():
        raise HookConfigError(
            f"hook {hook_id!r}: run.type 'command' requires a 'command'", hook_id=hook_id
        )
    run = HookRun(
        type=run_type,
        command=str(run_raw.get("command") or ""),
        args=tuple(str(a) for a in (run_raw.get("args") or ())),
        url=str(run_raw.get("url") or ""),
        prompt=str(run_raw.get("prompt") or ""),
    )

    return HookEntry(
        id=hook_id,
        on=frozenset(events),
        match=HookMatch(tool=tool_pat, annotations=annotations, args_pattern=args_pat),
        run=run,
        timeout_ms=int(raw.get("timeoutMs") or 30000),
        fail_closed=bool(raw.get("failClosed", False)),
        enabled=bool(raw.get("enabled", True)),
        source=source,
        loop_limit=int(raw.get("loopLimit") or 0),
    )


def parse_hook_entries(rows: Any, *, source: str) -> list[HookEntry]:
    """Parse a list of raw hook rows, skipping (with a diagnostic) any malformed one."""

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        logger.warning("[clio-hooks] %s: 'hooks' must be a list; ignoring", source)
        return []
    entries: list[HookEntry] = []
    for row in rows:
        if not isinstance(row, Mapping):
            logger.warning("[clio-hooks] %s: skipping non-object hook entry", source)
            continue
        try:
            entries.append(_parse_entry(row, source=source))
        except HookConfigError as exc:
            logger.warning("[clio-hooks] %s: %s", source, exc)
    return entries


def _load_file(path: Path) -> list[HookEntry]:
    """Load hook entries from one JSON config file, or ``[]`` if absent/malformed."""

    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[clio-hooks] failed to read hook config %s: %r", path, exc)
        return []
    if not isinstance(data, Mapping):
        logger.warning("[clio-hooks] hook config %s is not an object; ignoring", path)
        return []
    return parse_hook_entries(data.get("hooks"), source=str(path))


def discover_hook_entries(
    *,
    cwd: Path | None = None,
    user_config_path: Path | None = None,
    project_config_path: Path | None = None,
) -> list[HookEntry]:
    """Discover hook entries from the user + project config files.

    Precedence: project overrides user by ``id`` (an admin/managed tier is P2.7).
    An explicit ``*_config_path`` overrides the default location (used by tests
    and the ``CLIO_HOOKS_CONFIG`` env in :func:`build_hook_dispatcher`).
    """

    if user_config_path is None:
        from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

        user_config_path = paths.user_config_dir() / "hooks.json"
    if project_config_path is None:
        base = cwd if cwd is not None else Path.cwd()
        project_config_path = base / ".clio" / "hooks.json"

    merged: dict[str, HookEntry] = {}
    for entry in _load_file(user_config_path):
        merged[entry.id] = entry
    for entry in _load_file(project_config_path):
        merged[entry.id] = entry  # project wins on id collision
    return list(merged.values())
