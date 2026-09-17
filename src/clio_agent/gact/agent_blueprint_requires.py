"""Agent Blueprint ``requires: {clio_agent: ">=X"}`` server-floor enforcement.

Added deliverable, issue #1374 (S7-review comment): ``AGENT.md`` frontmatter
``requires:`` is already parsed into ``AgentBlueprintDefinition.metadata["requires"]``
(``gact/agent_blueprints.py``'s ``parse_agent_blueprint_root``) but nothing
consumed it, so a pack that needs newer server capabilities (S7's
``earthscope-single-agent`` needing S2's ``a2ui_catalogs``, S4's producer
tools, S5's ``ask_user(surface_id=)``) could not protect an older server —
the default registry bootstraps packs from marketplace ``main`` regardless of
what version cut this server is.

Owns the ONE decision: does the running server's version satisfy this
blueprint's declared PEP 440 ``clio_agent`` specifier. Two thin call sites
consume it (``.claude/CLAUDE.md`` no-accretion: the real logic lives here,
not duplicated at each call site):

* ``gact/agent_blueprints.py::validate_agent_blueprint_path`` — contributes a
  validation-error string, so an unsatisfied floor disables the blueprint
  (``enabled=False``) and is visible in the blueprint listing UI exactly like
  every other validation error, never a silent install.
* ``gact/routes/blueprints.py``'s session-activation route (``POST /v1/
  sessions/{sid}/agent-blueprint``, both the installed-id and the explicit-
  path branches) — refuses activation outright (400,
  ``blueprint_requires_newer_clio_agent``) and records the reason through
  ``gact/blueprint_activation.py``'s existing ``blueprint.resolution.degraded``
  ledger, the SAME mechanism every other Agent Blueprint resolution
  degradation already rides (no new store).

A missing ``requires.clio_agent`` key, an unparseable PEP 440 specifier, or
an unparseable running version are all treated as "no floor to enforce" —
this module never fabricates a refusal for a case it cannot actually
evaluate (⚑ #1/#2: clio surfaces reality, format-only, it does not invent a
decision the pack author's declaration and the running version do not
actually resolve).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from fastapi import HTTPException

    from clio_agent.gact.agent_blueprints import AgentBlueprintDefinition

#: The typed reason/refusal code — both the validation-error string
#: (``validate_agent_blueprint_path``) and the activation-time
#: ``blueprint.resolution.degraded`` ledger row use this exact code.
BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT = "blueprint_requires_newer_clio_agent"


def _running_clio_agent_version() -> str:
    from clio_agent import __version__  # noqa: PLC0415 - avoid import cycle at module load

    return __version__


def declared_clio_agent_requirement(metadata: dict[str, Any]) -> str:
    """Return the raw ``requires.clio_agent`` PEP 440 specifier string, or ``""``."""

    requires = metadata.get("requires")
    if not isinstance(requires, dict):
        return ""
    value = requires.get("clio_agent")
    return str(value).strip() if value else ""


def unsatisfied_clio_agent_floor(metadata: dict[str, Any], *, running_version: str = "") -> str:
    """Return the declared specifier text when it is NOT met, else ``""``.

    ``running_version`` overrides the live ``clio_agent.__version__`` (unit
    tests pin an explicit "server version" without monkeypatching the
    package); empty (the default) reads the real running version.
    """

    specifier_text = declared_clio_agent_requirement(metadata)
    if not specifier_text:
        return ""
    try:
        specifier = SpecifierSet(specifier_text)
    except InvalidSpecifier:
        return ""
    try:
        running = Version(running_version or _running_clio_agent_version())
    except InvalidVersion:
        return ""
    return "" if specifier.contains(running, prereleases=True) else specifier_text


def requires_floor_errors(blueprint: "AgentBlueprintDefinition") -> list[str]:
    """``validate_agent_blueprint_path``'s error-list contribution for this blueprint.

    A single-item list naming the typed reason when the floor is unmet;
    ``[]`` otherwise. Folded straight into that function's ``errors`` beside
    every other contributor, so an unsatisfied floor disables the blueprint
    (``enabled = blueprint.enabled and not errors``) exactly like a missing
    MCP tool reference or an invalid a2ui catalog declaration.
    """

    unsatisfied = unsatisfied_clio_agent_floor(blueprint.metadata)
    if not unsatisfied:
        return []
    return [
        f"{blueprint.id}: {BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT}: "
        f"requires clio_agent{unsatisfied}, running {_running_clio_agent_version()}"
    ]


def requires_floor_activation_error(
    metadata: dict[str, Any],
    blueprint_id: str,
    *,
    app: Any | None = None,
    session_id: str | None = None,
) -> "HTTPException | None":
    """The typed 400 to raise at ACTIVATION when this blueprint's floor is unmet.

    Takes ``metadata``/``blueprint_id`` as primitives (not the
    ``AgentBlueprintDefinition`` dataclass) so BOTH of ``routes/blueprints.py``'s
    session-activation branches can call it identically: the installed-id
    branch has a real ``AgentBlueprintDefinition`` (pass ``.metadata``/``.id``);
    the explicit-path branch only has the ``validate_agent_blueprint_path``
    wire dict (pass its ``["metadata"]``/``["id"]`` keys). ``app``/
    ``session_id`` are the calling route's own locals — a route handler has
    no ambient ``gact.context`` turn (see ``_record_resolution_reason``).

    Records ``blueprint_requires_newer_clio_agent`` through
    ``blueprint_activation.record_requires_floor_reason`` first (the SAME
    ``blueprint.resolution.degraded`` ledger every other Agent Blueprint
    resolution degradation rides — queryable via
    ``blueprint_resolution_reasons(app, sid)``, no new store) so activation's
    refusal is durable and queryable exactly like the pre-existing
    degradation reasons, not merely a one-shot HTTP error. Returns ``None``
    when the floor is satisfied (or absent/unparseable) — the caller's
    normal activation path continues unchanged.
    """

    unsatisfied = unsatisfied_clio_agent_floor(metadata)
    if not unsatisfied:
        return None
    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.blueprint_activation import (  # noqa: PLC0415
        record_requires_floor_reason,
    )
    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415

    record_requires_floor_reason(blueprint_id, app=app, session_id=session_id)
    running = _running_clio_agent_version()
    return HTTPException(
        status_code=400,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT,
                message=(
                    f"agent blueprint {blueprint_id!r} requires clio_agent{unsatisfied}, "
                    f"this server is running {running}"
                ),
                details={
                    "agent_blueprint_id": blueprint_id,
                    "requires_clio_agent": unsatisfied,
                    "running_clio_agent_version": running,
                },
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


__all__ = [
    "BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT",
    "declared_clio_agent_requirement",
    "requires_floor_activation_error",
    "requires_floor_errors",
    "unsatisfied_clio_agent_floor",
]
