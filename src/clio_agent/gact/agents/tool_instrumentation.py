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
    part (spawn_agent_task / spawn_agents_parallel / run_workflow) — that part
    already IS call evidence, so the observer records telemetry (semantic
    events + ledger) but skips the redundant ``tool_call``/``tool_result`` row;
  - ``"chip"``: the action gets its normal ``tool_call``/``tool_result`` row
    PLUS a ``resource_link`` chip appended separately at turn finalize
    (create_artifact) — the chip is adornment, never a replacement for the
    call row.

  Every EXECUTED tool call emits its ``tool_call``/``tool_result`` parts
  unconditionally (owner ruling, P5 wire semantics); a declared representation
  may only ADD adornment on top, never remove the call row. ``"handoff"`` is
  the one exception, because its own runtime already emits equivalent call
  evidence (no double-emission).
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
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from clio_agent.gact.evidence import _bounded_tool_call_result
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

# name -> resolver(app, args) -> extra STARTED-phase ``tool_call`` metadata.
# Declared HERE, next to the representation/title registries, instead of a
# hardcoded ``if name == ...`` inside ``tool_observer._make_tool_observer`` —
# the observer only ever consults the registry (:func:`tool_call_metadata_resolver`).
TOOL_CALL_METADATA_RESOLVERS: dict[str, Callable[[Any, Mapping[str, Any]], dict[str, Any]]] = {}


def register_tool_call_metadata_resolver(
    name: str, resolver: Callable[[Any, Mapping[str, Any]], dict[str, Any]]
) -> None:
    """Register a per-tool STARTED-phase ``tool_call`` metadata resolver.

    ``resolver(app, args)`` returns extra keys merged into the observer's
    ``call_metadata`` for THIS tool's ``started`` phase only.
    """

    TOOL_CALL_METADATA_RESOLVERS[str(name)] = resolver


def tool_call_metadata_resolver(
    name: str,
) -> Callable[[Any, Mapping[str, Any]], dict[str, Any]] | None:
    """Return the registered call-metadata resolver for ``name``, if any."""

    return TOOL_CALL_METADATA_RESOLVERS.get(str(name or ""))


def _wait_agent_tasks_call_metadata(app: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve ``wait_agent_tasks``'s STARTED-phase ``waited_tasks`` display rows.

    Clean-wire rule (P5): resolved from the agent-task registry AT CALL TIME
    (static spawn-time facts) so the UI never renders a raw task-id array.
    ``task_ids`` is model-authored: a bare STRING used to reach
    ``list(args.get("task_ids") or [])``, which iterates its CHARACTERS —
    fabricating one bogus row per character. Anything that isn't absent or a
    genuine list of string ids gets a typed marker instead of invented rows;
    the tool's own argument validation surfaces the real error.
    """

    from clio_agent.gact.agent_tasks import resolve_waited_task_rows  # noqa: PLC0415

    task_ids = args.get("task_ids")
    if task_ids is None:
        task_ids = []
    if isinstance(task_ids, list) and all(isinstance(item, str) for item in task_ids):
        return {"waited_tasks": resolve_waited_task_rows(app, task_ids)}
    return {"waited_tasks": [{"invalid": "task_ids_not_a_list"}]}


register_tool_call_metadata_resolver("wait_agent_tasks", _wait_agent_tasks_call_metadata)

# create_artifact's own ``content`` (+ batch ``artifacts[].content``) is a
# model-authored DELIVERABLE, not call evidence — the minted artifact file +
# its resource_link chip is already the durable copy. Scoped strictly to the
# tool(s) that carry it, never a generic content-sniffing heuristic.
_ARTIFACT_CONTENT_TOOL_NAMES = frozenset({"create_artifact"})


def _elided_artifact_content(value: Any) -> Any:
    """Typed elision marker for one artifact ``content`` value (never silent)."""

    if not isinstance(value, str) or not value:
        return value
    return {"elided": "artifact_content", "bytes": len(value.encode("utf-8"))}


def bounded_tool_call_input(name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """Wire-safe projection of a tool call's input args for the observer.

    create_artifact's deliverable content is elided (above) before the
    generic bound (:func:`clio_agent.gact.evidence._bounded_tool_call_result`,
    the same 12000-char limit tool RESULTS use) covers any other oversized
    argument on any tool.
    """

    projected: dict[str, Any] = dict(args)
    if name in _ARTIFACT_CONTENT_TOOL_NAMES:
        if "content" in projected:
            projected["content"] = _elided_artifact_content(projected["content"])
        artifacts = projected.get("artifacts")
        if isinstance(artifacts, list):
            projected["artifacts"] = [
                (
                    {**item, "content": _elided_artifact_content(item["content"])}
                    if isinstance(item, Mapping) and "content" in item
                    else item
                )
                for item in artifacts
            ]
    return _bounded_tool_call_result(projected)


def sanitize_tool_title(title: object) -> str:
    """Sanitize a curated tool title for the wire.

    Strips control characters / newlines (any non-printable becomes a space),
    collapses whitespace runs, and clamps to :data:`TITLE_MAX_CHARS` —
    untrusted-input discipline applied even to our own curated strings.
    """

    text = str(title or "")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(cleaned.split())[:TITLE_MAX_CHARS].strip()


def _annotations_title(annotations: object) -> str:
    """Read ``ToolAnnotations.title`` from either shape annotations arrive in.

    FastMCP/mcp exposes ``annotations`` as a ``ToolAnnotations`` model on a live
    listed tool; persisted/descriptor rows carry it as a plain mapping (e.g.
    :func:`clio_agent.tools.catalog.normalize_mcp_annotations`'s output). Both
    normalize to the same ``title`` read.
    """

    if annotations is None:
        return ""
    if isinstance(annotations, Mapping):
        return str(annotations.get("title") or "")
    return str(getattr(annotations, "title", None) or "")


def mcp_tool_title(mcp_tool: object) -> str:
    """Resolve an upstream MCP tool's display title (#1188 MCP half).

    ``Tool.title`` (the first-class display-name field) WINS when present;
    ``ToolAnnotations.title`` (the ``annotations={"title": ...}`` field
    clio-kit's servers actually populate today — hdf5/arxiv/plot/web) is the
    fallback when ``Tool.title`` is absent. Both absent -> ``""`` — never
    invented. Accepts either an ``mcp.types.Tool``-shaped object (the
    execution-boundary bridge) or a plain mapping row (the external-MCP
    registry's ``tool_row``), so this is the ONE precedence rule for every
    bridge seam.
    """

    if isinstance(mcp_tool, Mapping):
        title = mcp_tool.get("title") or ""
        annotations = mcp_tool.get("annotations")
    else:
        title = getattr(mcp_tool, "title", None) or ""
        annotations = getattr(mcp_tool, "annotations", None)
    return str(title) if title else _annotations_title(annotations)


def stamp_mcp_tool_title(func: Callable[..., Any], mcp_tool: object) -> None:
    """Stamp an MCP-bridged callable with its upstream tool's declared title.

    #1188 MCP half: resolves via :func:`mcp_tool_title` (``Tool.title`` then
    ``ToolAnnotations.title``), sanitized through the same
    :func:`sanitize_tool_title` curated native titles use, so it rides the
    assembly seam (:func:`instrument_tools`) onto ``Part.tool_title`` exactly
    like a curated native title. Absent when the server declares neither —
    never invented. No ``server_title`` yet (needs the serverInfo surface,
    tracked on the issue).
    """

    title = mcp_tool_title(mcp_tool)
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

    class ClioNativeTool(dspy.Tool):
        """DSPy tool whose JSON schema honors declared argument defaults."""

        def format_as_litellm_function_call(self) -> dict[str, Any]:
            formatted = super().format_as_litellm_function_call()
            function_schema = formatted["function"]
            properties = function_schema["parameters"]["properties"]
            function_schema["parameters"]["required"] = [
                arg_name
                for arg_name, arg_schema in properties.items()
                if "default" not in arg_schema
            ]
            return formatted

    setattr(func, REPRESENTATION_ATTR, _validated_representation(representation, tool_name=name))
    setattr(func, TITLE_ATTR, sanitize_tool_title(title))
    return ClioNativeTool(func=func, name=name, desc=desc, args=args)


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


# Per-thread one-shot declaration (owner ruling, wire semantics): a native tool
# call and its instrumentation wrapper always run SYNCHRONOUSLY on the SAME
# thread (call -> func() -> notify completed, no interleaving with another
# tool's call on that thread), so a thread-local is a safe, simple channel —
# the same pattern ``tool_observer``'s ``_OBSERVER_CALL_IDS``/``_OBSERVER_CALL_T0``
# already use for the same reason.
_DECLARED_STRUCTURED_CONTENT = threading.local()


def declare_structured_content(value: Mapping[str, Any]) -> None:
    """Attach a typed structured payload to THIS call's wire ``structured_content``
    WITHOUT changing what the caller (the model) receives as the tool's actual
    return value (owner ruling, P5 wire semantics: a tool DECLARES its
    presentation the way an MCP tool's ``outputSchema``/``structuredContent``
    does — the UI's existing result ladder then renders it top-down with zero
    tool-specific client code, instead of the presentation riding on
    incidental dict-key ORDER in the model-facing return).

    Call this from inside a native tool's own function body, any time before
    it returns. The instrumentation wrapper reads + clears it the moment the
    call completes (:func:`pop_declared_structured_content`) — one-shot, so a
    stale value can never leak onto a later, unrelated call on the same
    thread. A tool that never calls this gets the unchanged default
    (``structured_content`` derived from an MCP-shaped ``result``, or absent).
    """

    _DECLARED_STRUCTURED_CONTENT.value = dict(value)


def pop_declared_structured_content() -> dict[str, Any] | None:
    """Read + clear the current thread's declared structured payload, if any.

    Idempotent (a second pop with nothing declared just returns ``None``), so
    it is safe to call from more than one place in the same call's lifecycle.
    Two call sites consume it (finding A, proven leak fix):

    * the tool observer (``tool_observer._make_tool_observer``) reads it once
      per completed call to attach ``structured_content`` to the wire
      ``tool_result`` part -- the happy path;
    * :func:`observed_tool_callable`'s wrapper ALSO pops it, in a
      ``finally``, immediately after notifying "completed" -- regardless of
      whether the observer ever reached its own read (an unresolved session
      id, an observer exception ``execution.notify_tool_observer`` swallows,
      or no observer installed at all all used to leave a stale value for the
      NEXT unrelated call on this thread to inherit). This is the channel's
      REAL one-shot guarantee: consumption is tied to the call boundary, not
      to how far the observer got.
    """

    value = getattr(_DECLARED_STRUCTURED_CONTENT, "value", None)
    _DECLARED_STRUCTURED_CONTENT.value = None
    return value


def observed_tool_callable(func: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """Route a native tool callable through the live tool observer.

    Generalizes the former spawn-runtime ``_observed_collector`` (owner,
    2026-08-05): a native tool call is a REAL tool call the model makes; it
    must reach the observer — a ``started`` with the call's bound args, a
    ``completed`` with the verbatim result and the true duration, the error
    surfaced on failure — never invisible mechanism the narration references,
    and never re-authored.

    Finding A (proven leak, three independent paths): a declared structured
    payload (:func:`declare_structured_content`) is meant to be one-shot, but
    the observer's own read (``tool_observer.py``'s completed-phase pop) sits
    behind an unresolved session id, an observer exception
    ``execution.notify_tool_observer`` swallows, and the observer being
    ``None`` entirely -- any of which used to skip the pop and leave the
    declaration for the NEXT, unrelated call on this thread to inherit. This
    wrapper now consumes the channel itself, at the call boundary, in a
    ``finally`` -- regardless of what the observer did or didn't do -- and
    clears it BEFORE ``func`` runs too (belt-and-braces: a stale value from a
    call whose cleanup was somehow skipped anyway must never survive to be
    read as if it belonged to THIS call).
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
        # Belt-and-braces: THIS call hasn't run ``func`` yet, so nothing it
        # declares exists -- any value still sitting here belongs to an
        # earlier, unrelated call whose own cleanup was skipped. Discard it
        # now so it can never be misread as THIS call's declaration.
        pop_declared_structured_content()
        try:
            result = func(*call_args, **call_kwargs)
        except Exception as exc:
            try:
                _execution.notify_global_tool_observer(tool_name, args, "completed", error=str(exc))
            finally:
                # The real one-shot guarantee: consumed HERE, tied to the call
                # boundary, whether or not the observer ever reached its own
                # pop (idempotent -- a no-op when it already did).
                pop_declared_structured_content()
            raise
        try:
            _execution.notify_global_tool_observer(tool_name, args, "completed", result=result)
        finally:
            pop_declared_structured_content()
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
