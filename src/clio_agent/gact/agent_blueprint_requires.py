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
blueprint's declared PEP 440 ``clio_agent`` specifier. Two call sites consume
it (``.claude/CLAUDE.md`` no-accretion: the real logic lives here, not
duplicated at each call site):

* ``gact/agent_blueprints.py::parse_agent_blueprint_root`` — folds
  :func:`floor_declaration_errors` straight into the blueprint's OWN
  ``validation_errors`` at parse time (the same place the ``workflow_state``
  schema check already lands), so EVERY consumer of a parsed blueprint --
  ``validate_agent_blueprint_path``, ``install_agent_blueprint`` (which gates
  on ``parsed.enabled``), and ``discover_agent_blueprints`` (the
  ``GET /v1/agent-blueprints`` listing) -- disables/refuses/shows the SAME
  typed reason for free, from ONE source of truth. A install-time refusal was
  the S8-review finding: the floor used to be enforced only at session
  activation, so an over-the-floor pack still installed 201 with
  ``validation_errors: []`` and the listing showed ``enabled: true``.
* ``gact/blueprint_activation.py::agent_blueprint_activation_metadata`` — the
  ONE seam both of ``routes/blueprints.py``'s session-activation branches
  call (moved there from ``gact/app.py`` in the same review round, since that
  module already owns the ``blueprint.resolution.degraded`` reason ledger)
  raises :func:`requires_floor_activation_error`'s typed 400 BEFORE
  projecting a blueprint's install provenance into session metadata, so no
  partial activation state is ever written for a blueprint this server
  cannot honour.

Two distinct typed reasons, both surfaced -- never a silent pass:
``blueprint_requires_newer_clio_agent`` (a well-formed specifier the running
version does not satisfy) and ``blueprint_requires_unparseable`` (the
declared specifier -- or, in the unreachable-in-production case, the running
version -- does not even parse as PEP 440; a format-only validation, ⚑ #2:
schema-validate is allowed). Only a genuinely ABSENT ``requires.clio_agent``
key is "no floor to enforce."
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import HTTPException

#: Two distinct typed reason/refusal codes — both the validation-error
#: string (parse time) and the activation-time ``blueprint.resolution.
#: degraded`` ledger row use these exact codes.
BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT = "blueprint_requires_newer_clio_agent"
BLUEPRINT_REQUIRES_UNPARSEABLE = "blueprint_requires_unparseable"


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


def _floor_reason(metadata: dict[str, Any], *, running_version: str = "") -> tuple[str, str] | None:
    """Return ``(reason_code, declared_specifier_text)`` when this blueprint's
    ``requires.clio_agent`` is a problem, else ``None`` (no floor declared, or
    a well-formed floor the running version satisfies).

    Two distinct typed reasons (see module docstring):
    :data:`BLUEPRINT_REQUIRES_UNPARSEABLE` when the declared specifier (or,
    unreachable in production, the running version) fails to parse as PEP
    440 -- a format-only validation, never silently treated as "no floor" --
    and :data:`BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT` when it parses fine but
    the running version does not satisfy it.
    """

    specifier_text = declared_clio_agent_requirement(metadata)
    if not specifier_text:
        return None
    try:
        specifier = SpecifierSet(specifier_text)
    except InvalidSpecifier:
        return BLUEPRINT_REQUIRES_UNPARSEABLE, specifier_text
    version_text = running_version or _running_clio_agent_version()
    try:
        running = Version(version_text)
    except InvalidVersion:
        return BLUEPRINT_REQUIRES_UNPARSEABLE, specifier_text
    if specifier.contains(running, prereleases=True):
        return None
    return BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT, specifier_text


def unsatisfied_clio_agent_floor(metadata: dict[str, Any], *, running_version: str = "") -> str:
    """Return the declared specifier text when the floor is a problem
    (unsatisfied OR unparseable), else ``""``.

    A thin, reason-collapsing convenience over :func:`_floor_reason` for a
    caller that only needs the boolean/text, not which of the two typed
    reasons applies.
    """

    problem = _floor_reason(metadata, running_version=running_version)
    return problem[1] if problem is not None else ""


def _floor_detail(reason_code: str, specifier_text: str, *, running: str) -> str:
    if reason_code == BLUEPRINT_REQUIRES_UNPARSEABLE:
        return (
            f"requires.clio_agent {specifier_text!r} could not be evaluated as a "
            "PEP 440 specifier against this server's own version"
        )
    return f"requires clio_agent{specifier_text}, running {running}"


def floor_declaration_errors(blueprint_id: str, requirements: "Mapping[str, Any]") -> list[str]:
    """``parse_agent_blueprint_root``'s error-list contribution for one
    blueprint's ``requires.clio_agent`` declaration.

    Folded straight into ``errors`` at PARSE time (not at
    ``validate_agent_blueprint_path``, which only some callers reach) so
    ``AgentBlueprintDefinition.enabled``/``validation_errors`` reflect an
    unsatisfied or unparseable floor for every consumer of a parsed
    blueprint for free: the blueprint listing, install, and validate all
    read the SAME ``parse_agent_blueprint_root`` result.

    Args:
        blueprint_id: The blueprint's own resolved id (for the message).
        requirements: The raw ``meta.get("requires")`` sub-mapping (NOT the
            full blueprint metadata dict — this is called from inside
            ``parse_agent_blueprint_root`` before that dict exists).
    """

    problem = _floor_reason({"requires": dict(requirements)})
    if problem is None:
        return []
    reason_code, specifier_text = problem
    detail = _floor_detail(reason_code, specifier_text, running=_running_clio_agent_version())
    return [f"{blueprint_id}: {reason_code}: {detail}"]


def requires_floor_activation_error(
    metadata: dict[str, Any],
    blueprint_id: str,
    *,
    app: Any | None = None,
    session_id: str | None = None,
) -> "HTTPException | None":
    """The typed 400 to raise at ACTIVATION when this blueprint's floor is a problem.

    Takes ``metadata``/``blueprint_id`` as primitives (not the
    ``AgentBlueprintDefinition`` dataclass) so
    ``blueprint_activation.agent_blueprint_activation_metadata`` (the ONE
    seam both session-activation branches call) can call it identically
    from a ``blueprint_wire`` dict. ``app``/``session_id`` are the calling
    route's own locals — a route handler has no ambient ``gact.context``
    turn (see ``blueprint_activation._record_resolution_reason``).

    Records the SAME reason code through
    ``blueprint_activation.record_requires_floor_reason`` first (the SAME
    ``blueprint.resolution.degraded`` ledger every other Agent Blueprint
    resolution degradation rides — queryable via
    ``blueprint_resolution_reasons(app, sid)``, no new store) so
    activation's refusal is durable and queryable exactly like the
    pre-existing degradation reasons, not merely a one-shot HTTP error.
    Returns ``None`` when the floor is satisfied (or absent) — the caller's
    normal activation path continues unchanged.
    """

    problem = _floor_reason(metadata)
    if problem is None:
        return None
    reason_code, specifier_text = problem
    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.blueprint_activation import (  # noqa: PLC0415
        record_requires_floor_reason,
    )
    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415

    record_requires_floor_reason(blueprint_id, reason=reason_code, app=app, session_id=session_id)
    running = _running_clio_agent_version()
    detail = _floor_detail(reason_code, specifier_text, running=running)
    return HTTPException(
        status_code=400,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=reason_code,
                message=f"agent blueprint {blueprint_id!r}: {detail}",
                details={
                    "agent_blueprint_id": blueprint_id,
                    "requires_clio_agent": specifier_text,
                    "running_clio_agent_version": running,
                },
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


__all__ = [
    "BLUEPRINT_REQUIRES_NEWER_CLIO_AGENT",
    "BLUEPRINT_REQUIRES_UNPARSEABLE",
    "declared_clio_agent_requirement",
    "floor_declaration_errors",
    "requires_floor_activation_error",
    "unsatisfied_clio_agent_floor",
]
