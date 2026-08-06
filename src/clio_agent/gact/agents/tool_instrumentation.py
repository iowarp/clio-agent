"""Default-on native tool instrumentation (owner ruling 2026-08-05).

"All tools by default need to be instrumented as a matter of definition."
Per-tool manual observer shims (the former ``_observed_collector`` wrap in
``spawn_runtime`` that made ``wait_agent_tasks`` / ``check_agent_tasks``
visible) do not scale: every other native ``dspy.Tool`` — and every future
one — stayed invisible unless someone remembered to hand-wrap it. This owner
module replaces that per-tool opt-in with ONE assembly seam:

* :func:`instrument_tools` — called where the final tool list is handed to the
  react runtime (``builders._build_blueprint_dspy_module`` /
  ``builders._build_tool_user_agent_module``). EVERY tool whose callable is not
  already marked observed gets wrapped with the observer notification
  (``started`` with bound args, ``completed`` with the verbatim result, the
  error surfaced on raise — never re-authored).
* :func:`native_tool` — the sanctioned constructor for native tools. It stamps
  the tool's DECLARED presentation on the callable: an optional curated human
  ``title`` (sanitized here — untrusted-input discipline even for our own
  strings) and a ``representation``:

  - ``"row"`` (default): the live observer appends real ``tool_call`` /
    ``tool_result`` parts (with ``tool_title`` when curated);
  - ``"handoff"``: the action's wire representation IS its ``expert_handoff``
    part (spawn_agent_task / spawn_agents_parallel / run_workflow) — the
    observer records telemetry (semantic events + ledger) but appends no tool
    parts;
  - ``"chip"``: the action's wire representation is its ``resource_link`` chip
    (create_artifact) — telemetry only, no tool parts.

  One representation per action on the wire — the existing principle, now
  EXPLICIT at definition instead of implicit by unshimmed omission.
* :func:`boundary_observed_tool` / the bridge marker in
  ``clio_agent.tools.execution._make_dspy_tool`` — MCP-bridged tools already
  notify the observer through the execution boundary, so they carry
  ``TOOL_OBSERVED_ATTR`` from construction and the seam never double-wraps
  them (exactly-once notification).
* :func:`rebuilt_tool` — for wrappers that re-construct a tool around a new
  callable (``builders._recording_blueprint_tool``): propagates the
  instrumentation markers so a re-wrapped boundary tool stays exactly-once.

The observer (``gact.tool_observer._make_tool_observer``) reads the declared
presentation via :func:`declared_tool_representation` /
:func:`declared_tool_title` — a registry populated at the assembly seam, never
a name-matching heuristic.

CI guard: ``scripts/check_tool_instrumentation.py`` (baseline 0) fails any new
bare ``dspy.Tool(`` construction outside this factory and the execution-boundary
bridge, so a future tool cannot be born invisible.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Any

from clio_agent.tools.execution import TOOL_OBSERVED_ATTR

logger = logging.getLogger(__name__)

# Declared-presentation marker attributes stamped on a tool's CALLABLE (they
# survive ``functools.wraps``, which copies ``__dict__``).
REPRESENTATION_ATTR = "_clio_tool_representation"
TITLE_ATTR = "_clio_tool_title"

DEFAULT_REPRESENTATION = "row"
TOOL_REPRESENTATIONS = frozenset({"row", "handoff", "chip"})

TITLE_MAX_CHARS = 80

# name -> (representation, sanitized title). Populated at the assembly seam
# (:func:`instrument_tools`); read by the live tool observer. Tool names are
# stable process-wide, so a plain module dict is the registry.
_TOOL_PRESENTATIONS: dict[str, tuple[str, str]] = {}


def sanitize_tool_title(title: object) -> str:
    """Sanitize a curated tool title for the wire.

    Strips control characters / newlines (any non-printable becomes a space),
    collapses whitespace runs, and clamps to :data:`TITLE_MAX_CHARS` —
    untrusted-input discipline applied even to our own curated strings.
    """

    text = str(title or "")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(cleaned.split())[:TITLE_MAX_CHARS].strip()


def stamp_mcp_tool_title(func: Callable[..., Any], mcp_tool: object) -> None:
    """Stamp an MCP-bridged callable with its upstream tool's declared title.

    #1188 MCP half: an ``mcp.types.Tool`` carries an optional ``title`` distinct
    from its programmatic ``name``. When the upstream server declares one, this
    stamps it (sanitized through the same :func:`sanitize_tool_title` curated
    native titles use) so the assembly seam (:func:`instrument_tools`) registers
    it for ``Part.tool_title`` exactly like a curated native title. Absent when
    the server declares none — never invented. No ``server_title`` yet (needs
    the serverInfo surface, tracked on the issue).
    """

    title = getattr(mcp_tool, "title", None)
    if title:
        setattr(func, TITLE_ATTR, sanitize_tool_title(title))


def _validated_representation(representation: object, *, tool_name: str) -> str:
    """Return a valid representation or raise a typed error (never coerce silently)."""

    value = str(representation or DEFAULT_REPRESENTATION)
    if value not in TOOL_REPRESENTATIONS:
        raise ValueError(
            f"tool {tool_name!r} declared unknown representation {value!r}; "
            f"expected one of {sorted(TOOL_REPRESENTATIONS)}"
        )
    return value


def native_tool(
    func: Callable[..., Any],
    *,
    name: str,
    desc: str | None,
    args: dict[str, Any],
    title: str = "",
    representation: str = DEFAULT_REPRESENTATION,
) -> Any:
    """Construct a native ``dspy.Tool`` with its DECLARED presentation.

    The declaration (curated ``title`` + ``representation``) is stamped on the
    callable; the assembly seam (:func:`instrument_tools`) registers it for the
    observer and wraps the callable with the observer notification. This is the
    ONE sanctioned native construction path (CI guard baseline 0).
    """

    import dspy  # noqa: PLC0415

    setattr(func, REPRESENTATION_ATTR, _validated_representation(representation, tool_name=name))
    setattr(func, TITLE_ATTR, sanitize_tool_title(title))
    return dspy.Tool(func=func, name=name, desc=desc, args=args)


def boundary_observed_tool(
    func: Callable[..., Any],
    *,
    name: str,
    desc: str | None,
    args: dict[str, Any],
    title: str = "",
) -> Any:
    """Construct a ``dspy.Tool`` whose callable ALREADY notifies the observer.

    For callables whose execution path reaches ``notify_tool_observer`` itself
    (the external-MCP dynamic-agent tools in ``builders``). Marks the callable
    so the assembly seam never adds a second notification (exactly-once).

    ``title`` carries an upstream MCP tool's declared ``title`` (#1188 MCP
    half), sanitized through the same :func:`sanitize_tool_title` curated
    native titles use, so it registers into the presentation registry at the
    assembly seam exactly like a curated native title and rides onto
    ``Part.tool_title`` — no separate wire path.
    """

    import dspy  # noqa: PLC0415

    setattr(func, TOOL_OBSERVED_ATTR, True)
    setattr(func, TITLE_ATTR, sanitize_tool_title(title))
    return dspy.Tool(func=func, name=name, desc=desc, args=args)


def rebuilt_tool(
    inner_tool: Any,
    func: Callable[..., Any],
    *,
    name: str,
    desc: str | None,
    args: dict[str, Any],
) -> Any:
    """Re-construct a tool around a replacement callable, keeping its markers.

    A wrapper that re-creates a ``dspy.Tool`` with a new callable (the
    blueprint recording wrapper) would otherwise DROP the inner callable's
    instrumentation markers — a re-wrapped boundary tool would get
    double-wrapped at the seam and notify twice. Propagates the observed /
    representation / title markers from ``inner_tool``'s callable onto ``func``.
    """

    import dspy  # noqa: PLC0415

    inner_func = getattr(inner_tool, "func", None)
    for attr in (TOOL_OBSERVED_ATTR, REPRESENTATION_ATTR, TITLE_ATTR):
        value = getattr(inner_func, attr, None)
        if value is not None:
            setattr(func, attr, value)
    return dspy.Tool(func=func, name=name, desc=desc, args=args)


def observed_tool_callable(func: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """Route a native tool callable through the live tool observer.

    Generalizes the former spawn-runtime ``_observed_collector`` (owner,
    2026-08-05): a native tool call is a REAL tool call the model makes; it
    must reach the observer — a ``started`` with the call's bound args, a
    ``completed`` with the verbatim result and the true duration, the error
    surfaced on failure — never invisible mechanism the narration references,
    and never re-authored.
    """

    @functools.wraps(func)
    def wrapper(*call_args: Any, **call_kwargs: Any) -> Any:
        # Call-time module-attribute lookup so per-app observer resolution (and
        # test monkeypatching) always sees the live seam.
        from clio_agent.tools import execution as _execution  # noqa: PLC0415

        bound = inspect.signature(func).bind(*call_args, **call_kwargs)
        bound.apply_defaults()
        args = dict(bound.arguments)
        _execution.notify_global_tool_observer(tool_name, args, "started")
        try:
            result = func(*call_args, **call_kwargs)
        except Exception as exc:
            _execution.notify_global_tool_observer(tool_name, args, "completed", error=str(exc))
            raise
        _execution.notify_global_tool_observer(tool_name, args, "completed", result=result)
        return result

    setattr(wrapper, TOOL_OBSERVED_ATTR, True)
    return wrapper


def instrument_tools(tools: Iterable[Any]) -> list[Any]:
    """THE assembly seam: instrument every tool handed to a react runtime.

    For each tool: registers its declared presentation (default ``"row"`` with
    no title) for the observer, then wraps its callable with the observer
    notification unless the callable is already marked observed (a
    seam-wrapped native, or an MCP-bridged tool that notifies through the
    execution boundary). Idempotent via :data:`TOOL_OBSERVED_ATTR`.
    """

    return [_instrument_tool(tool) for tool in tools]


def _instrument_tool(tool: Any) -> Any:
    func = getattr(tool, "func", None)
    name = str(getattr(tool, "name", "") or "").strip()
    if not callable(func) or not name:
        # Nothing to wrap — surfaced loudly, never a silent pass (a tool that
        # dodges the seam is exactly the invisibility this module deletes).
        logger.warning(
            "tool instrumentation skipped reason=uninstrumentable_tool name=%r callable=%s",
            name,
            callable(func),
        )
        return tool
    representation = _validated_representation(
        getattr(func, REPRESENTATION_ATTR, "") or DEFAULT_REPRESENTATION, tool_name=name
    )
    title = sanitize_tool_title(getattr(func, TITLE_ATTR, ""))
    _TOOL_PRESENTATIONS[name] = (representation, title)
    if getattr(func, TOOL_OBSERVED_ATTR, False):
        return tool
    tool.func = observed_tool_callable(func, name)
    return tool


def declared_tool_representation(name: str) -> str:
    """The declared representation for ``name`` (``"row"`` when undeclared)."""

    return _TOOL_PRESENTATIONS.get(str(name or ""), (DEFAULT_REPRESENTATION, ""))[0]


def declared_tool_title(name: str) -> str:
    """The curated, sanitized title for ``name`` (empty when uncurated)."""

    return _TOOL_PRESENTATIONS.get(str(name or ""), (DEFAULT_REPRESENTATION, ""))[1]
