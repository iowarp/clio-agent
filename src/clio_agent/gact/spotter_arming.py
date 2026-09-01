"""Arm-time executability validation for the ``spotter-ai`` standing watcher.

Owner module for the question :func:`clio_agent.gact.spotter_watcher.sync_watcher_for_mode`
must answer BEFORE it arms: *can the watcher this session is about to depend on
actually execute?*

The live defect this closes (qualification sessions ``sess_71d5473bda17`` /
``sess_a35cd5416d46``): the ``spotter-ai`` Agent Blueprint declares its MCP
server as ``uv run --project ${SPOTTER_IMPL_DIR} ... --clio-config
${SPOTTER_CLIO_CONFIG}``. With those deployment variables unset the declaration
never resolves, so the watcher child mounts ZERO ``spotter_*`` tools and errors
``custom_agent_tools_unavailable`` on every wake — while arming itself reported
success and ``GET /v1/agent-blueprints/spotter-ai`` still showed
``enabled: true, validation_errors: []``. The fail-closed clearance barrier
(:mod:`clio_agent.gact.spotter_clearance`) then auto-denied EVERY destructive
tool call with ``spotter_watcher_check_failed``: an armed session was a total
write lockout whose first operator signal was a denial storm instead of an
arm-time diagnosis.

The SECOND live defect this closes (``sess_086cf23a960b``): checking the
launcher's ``--project`` DIRECTORY is not the same as checking that its ENTRY
POINT can spawn. ``SPOTTER_IMPL_DIR`` was set and the directory existed, but the
impl venv had been created and never synced (a bare ``python.exe``, no
``site-packages``); the gate passed, ``uv run --project <dir> --no-sync
spotter-mcp`` died "program not found", and the watcher errored
``custom_agent_tools_unavailable`` on every wake — the same denial storm, one
layer deeper. So a recognized ``uv run`` launcher now also has its console
script resolved against the project's virtual environment.

**Static, never a probe.** Nothing is launched here: no subprocess is spawned,
no handshake runs, no tool is listed. The watcher blueprint's declared servers
are normalized through the SAME parser the runtime mounts them with
(:func:`clio_agent.tools.mcp_config.spec_from_declaration`, which performs the
``${VAR}`` / ``${VAR:-default}`` expansion); a resolved stdio launcher's
``--project`` / ``--directory`` argument is stat'd, and — for the ``uv run``
shape :mod:`clio_agent.tools.uv_launcher` recognizes — so is the entry point
that argument's ``.venv`` must provide. That is the whole check —
deterministic, sub-millisecond, and it fails for exactly the deployment reason
the operator must fix.

**The entry-point half never guesses.** It applies only where its answer is
binding, and records a typed skip (never a silent pass) everywhere else:

* a launcher that is not the modeled ``uv run <console-script>`` shape — a
  ``node`` / ``npx`` / ``python -m`` server, or a ``uv`` argv carrying a flag
  the parser does not model — keeps the directory-only behavior, because
  mistaking a flag's value for an entry point would manufacture a FALSE
  refusal of a working deployment;
* a ``uv run`` WITHOUT ``--no-sync`` provisions its own environment before
  exec, so an absent or half-populated venv is not a static precondition
  failure and is not refused;
* a launcher that redirects the environment via ``UV_PROJECT_ENVIRONMENT``
  does not use ``<project>/.venv``, so there is nothing this module can stat.

**Fail closed AT ARMING.** A refusal REFUSES THE TRANSITION into ``spotter-ai``
with a typed HTTP 422 (:data:`SPOTTER_ARMING_REASONS`) carrying the stable
reason plus the unresolvable spec detail (which environment variable, which
path), rather than arming a watcher that cannot run. Because the route seam is
reached after the session store has already persisted the transition (the two
call sites in ``gact/routes/sessions.py`` are ``create``-then-sync and
``update``-then-sync), the refusal also UNDOES that persisted transition before
raising, so the caller's 422 never leaves a half-armed session behind:

* a refused CREATE deletes the just-minted session row — the caller never
  received its id, and nothing must be able to open a locked-out session later
  (the store's ``session.created`` / ``session.deleted`` lifecycle pair is
  emitted honestly; the refusal's own trace line names the rollback);
* a refused PATCH restores ``approval_mode`` to the session's prior value
  BEFORE the route publishes its ``session.updated`` event, so no subscriber
  ever observes the refused mode. Other fields in the same PATCH body stay
  applied — only the mode transition is refused, and the 422 says so.

Every non-refusal path that could hide a problem is typed and logged too (a
watcher blueprint that is not installed, or declares no servers, has nothing to
resolve and is NOT refused — arming for such a deployment is unchanged).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Optional

from clio_agent.runtime import trace
from clio_agent.tools.uv_launcher import (
    VENV_STATE_ENTRYPOINT_ABSENT,
    VENV_STATE_MISSING,
    VENV_STATE_UNSYNCED,
    entrypoint_venv_state,
    parse_uv_run_launcher,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Session
    from clio_agent.tools.mcp_config import MCPServerSpec

logger = logging.getLogger(__name__)

#: A declared MCP server for the watcher blueprint does not RESOLVE: an
#: environment variable its declaration references is unset, or the declaration
#: itself is invalid. The watcher would mount none of that server's tools.
REFUSAL_WATCHER_UNMOUNTABLE = "spotter_watcher_unmountable"

#: The declaration resolves, but the stdio launcher's ``--project`` /
#: ``--directory`` argument names a directory that does not exist, so the
#: subprocess dies on chdir/ENOENT (the same precondition
#: :class:`clio_agent.tools.mcp_config.MCPSpawnError` catches at mount time).
REFUSAL_WATCHER_PROJECT_MISSING = "spotter_watcher_project_missing"

#: The declaration resolves and the project directory exists, but that project's
#: virtual environment cannot provide the launcher's ENTRY POINT, so ``uv run
#: --no-sync <entrypoint>`` exits "program not found" before any MCP handshake
#: (live defect ``sess_086cf23a960b``). Carries a ``venv_state`` naming which of
#: the three distinct operator fixes applies.
REFUSAL_WATCHER_ENTRYPOINT_MISSING = "spotter_watcher_entrypoint_missing"

#: Closed set of typed arm-time refusals -> the operator-facing explanation.
#: Every refusal :func:`validate_watcher_arming` returns is a key here, so a
#: caller can neither invent a reason nor lose the distinction between them.
SPOTTER_ARMING_REASONS: dict[str, str] = {
    REFUSAL_WATCHER_UNMOUNTABLE: (
        "SPOTTER surveillance was not armed: a declared MCP server for the watcher "
        "Agent Blueprint does not resolve, so the watcher would start with none of "
        "its tools and every protected tool call would be denied."
    ),
    REFUSAL_WATCHER_PROJECT_MISSING: (
        "SPOTTER surveillance was not armed: the watcher's declared MCP launcher "
        "points at a project directory that does not exist, so its server cannot start."
    ),
    REFUSAL_WATCHER_ENTRYPOINT_MISSING: (
        "SPOTTER surveillance was not armed: the watcher's declared MCP launcher names "
        "an entry point its project environment does not provide, so the server exits "
        "before it can offer a single tool and every protected tool call would be denied."
    ),
}

#: The typed reason emitted (never raised) when there is simply nothing to check.
_SKIP_BLUEPRINT_NOT_INSTALLED = "watcher_blueprint_not_installed"
_SKIP_NO_DECLARED_SERVERS = "watcher_blueprint_declares_no_servers"
_SKIP_DISCOVERY_FAILED = "watcher_blueprint_discovery_failed"

#: The typed reasons the ENTRY-POINT half declines to have an opinion (see the
#: module docstring): an unmodeled launcher shape, a launcher that provisions
#: its own environment, and one that redirects the environment elsewhere.
_SKIP_LAUNCHER_SHAPE_UNMODELED = "watcher_launcher_shape_unmodeled"
_SKIP_LAUNCHER_SYNCS_ON_START = "watcher_launcher_syncs_on_start"
_SKIP_LAUNCHER_ENVIRONMENT_REDIRECTED = "watcher_launcher_environment_redirected"

#: ``uv`` honours this variable INSTEAD of ``<project>/.venv``; when a launcher
#: sets it (or the arming environment carries it) there is no default venv to
#: stat, so the entry-point check has nothing to say.
_UV_PROJECT_ENVIRONMENT = "UV_PROJECT_ENVIRONMENT"

#: ``venv_state`` -> the operator fix that state calls for. Each is a DIFFERENT
#: action, which is why the state is reported rather than collapsed.
_VENV_STATE_DETAIL: dict[str, str] = {
    VENV_STATE_MISSING: "the project has no .venv at all",
    VENV_STATE_UNSYNCED: (
        "the project's .venv exists but was never synced (no populated site-packages)"
    ),
    VENV_STATE_ENTRYPOINT_ABSENT: (
        "the project's .venv provides no such console script and no matching distribution"
    ),
}

#: ``expand_env`` raises exactly this text for an unset required variable; the
#: variable name is the actionable half of the refusal, so it is lifted back out
#: rather than re-derived by re-scanning the declaration.
_UNSET_ENV_VAR = re.compile(
    r"required environment variable \$\{([A-Za-z_][A-Za-z0-9_]*)\} is unset"
)

#: Launcher flags whose value is a DIRECTORY that must already exist for the
#: stdio subprocess to start (``uv run --project <dir>`` / ``--directory <dir>``).
#: Only these shapes are checked — the static check never guesses at the meaning
#: of an argument it does not know.
_PROJECT_DIR_FLAGS = ("--project", "--directory")


@dataclass(frozen=True)
class WatcherArmingRefusal:
    """One typed, fully-attributed reason the watcher cannot be armed.

    Attributes:
        reason: A :data:`SPOTTER_ARMING_REASONS` key (the stable vocabulary the
            API caller and the trace both key on).
        detail: The underlying declaration-level error text.
        blueprint_id: The watcher Agent Blueprint whose declaration failed.
        server: The declared MCP server name inside that blueprint.
        variable: The unset environment variable, when the failure names one.
        path: The missing directory, when the failure names one. For an
            entry-point refusal this is the launcher's project directory, which
            ``details()`` also republishes under the typed ``project_dir`` key.
        entrypoint: The console script that does not resolve, when the failure
            names one.
        venv_state: Which :mod:`clio_agent.tools.uv_launcher` state the project
            environment is in, when the failure names one — the half that tells
            the operator WHICH fix applies.
    """

    reason: str
    detail: str
    blueprint_id: str
    server: str
    variable: str = ""
    path: str = ""
    entrypoint: str = ""
    venv_state: str = ""

    @property
    def message(self) -> str:
        """The operator-facing message: the closed-set explanation + the specific fault."""

        return f"{SPOTTER_ARMING_REASONS[self.reason]} ({self.detail})"

    def details(self, session_id: str = "") -> dict[str, str]:
        """The structured ``ErrorInfo.details`` payload for the typed 422."""

        payload = {
            "reason": self.reason,
            "agent_blueprint_id": self.blueprint_id,
            "mcp_server": self.server,
            "detail": self.detail,
        }
        if session_id:
            payload["session_id"] = session_id
        if self.variable:
            payload["environment_variable"] = self.variable
        if self.path:
            payload["path"] = self.path
        if self.entrypoint:
            payload["entrypoint"] = self.entrypoint
        if self.venv_state:
            payload["venv_state"] = self.venv_state
            payload["project_dir"] = self.path
        return payload


def _record_skip(reason: str, blueprint_id: str, *, server: str = "", error: str = "") -> None:
    """Log + trace one typed "nothing to validate" outcome (no silent pass)."""

    logger.info(
        "spotter_watcher_arm_check_skip reason=%s blueprint=%s server=%s error=%s",
        reason,
        blueprint_id,
        server,
        error,
    )
    trace.event(
        "SPOTTER",
        "spotter_watcher_arm_check_skip reason=%s blueprint=%s server=%s error=%s",
        reason,
        blueprint_id,
        server,
        error,
    )


def _declared_watcher_servers(
    app: "FastAPI", blueprint_id: str, *, session_id: str = "", workspace_id: str = ""
) -> Mapping[str, Any]:
    """Return the watcher blueprint's declared ``mcp_servers`` map (raw declarations).

    Resolved by INSTALLED id, deterministically: the watcher child is bound to
    its blueprint by id (``turn_spawn.TaskSpec.session_scope_metadata`` carries
    ``active_agent_blueprint_id``), and arm time is a route context with no
    active turn/tool contextvar to resolve a path-activated pack from. The
    workspace scan root matches the one the runtime tool gateway uses
    (``<workspace>/.clio/agent-blueprints`` alongside the global root), so a
    workspace-local watcher pack is seen here exactly as it will be mounted.

    Returns an empty mapping — typed-logged, never silent — when the blueprint
    is not installed, declares no servers, or discovery itself fails.
    """

    from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import _runtime_workspace_catalog_cwd  # noqa: PLC0415
    from clio_agent.gact.blueprint_activation import blueprint_server_map  # noqa: PLC0415

    cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id, session_id=session_id)
    try:
        blueprints = discover_agent_blueprints(cwd=cwd)
    except Exception as exc:  # noqa: BLE001 - discovery failure must not fail the route
        _record_skip(_SKIP_DISCOVERY_FAILED, blueprint_id, error=repr(exc))
        return {}
    blueprint = next((row for row in blueprints if row.id == blueprint_id), None)
    if blueprint is None:
        _record_skip(_SKIP_BLUEPRINT_NOT_INSTALLED, blueprint_id)
        return {}
    servers = blueprint_server_map(blueprint)
    if not servers:
        _record_skip(_SKIP_NO_DECLARED_SERVERS, blueprint_id)
    return servers


def _unset_variable(errors: Sequence[str]) -> str:
    """Return the first unset environment variable named by a spec's errors."""

    for error in errors:
        match = _UNSET_ENV_VAR.search(error)
        if match:
            return match.group(1)
    return ""


def _project_directories(args: Sequence[str]) -> list[str]:
    """Return every ``--project`` / ``--directory`` VALUE in a launcher argv.

    Handles both spellings a declaration may use (``--project <dir>`` and
    ``--project=<dir>``). Any other argument is left alone: this check never
    guesses which arguments are paths. A flag left without a value (trailing, or
    expanded to an empty string) yields ``""`` — equally unspawnable, and
    reported as such rather than dropped.
    """

    found: list[str] = []
    expecting = False
    for raw in args:
        arg = str(raw)
        if expecting:
            found.append(arg)
            expecting = False
            continue
        for flag in _PROJECT_DIR_FLAGS:
            if arg == flag:
                expecting = True
                break
            if arg.startswith(f"{flag}="):
                found.append(arg[len(flag) + 1 :])
                break
    if expecting:
        found.append("")
    return found


def _environment_is_redirected(spec: "MCPServerSpec", *, env: Optional[Mapping[str, str]]) -> bool:
    """True when ``UV_PROJECT_ENVIRONMENT`` moves the venv off ``<project>/.venv``.

    Both the declaration's own ``env`` block (which the subprocess inherits) and
    the arming environment are consulted, because either one reaches ``uv``.
    """

    if str(spec.env.get(_UV_PROJECT_ENVIRONMENT, "")).strip():
        return True
    source = os.environ if env is None else env
    return bool(str(source.get(_UV_PROJECT_ENVIRONMENT, "")).strip())


def _entrypoint_refusal(
    blueprint_id: str,
    name: str,
    spec: "MCPServerSpec",
    *,
    env: Optional[Mapping[str, str]],
) -> Optional[WatcherArmingRefusal]:
    """Resolve a recognized ``uv run`` launcher's console script, or decline.

    ``None`` means "this server's entry point is not a static precondition this
    module can answer" — every such decline is typed-logged with the reason it
    declined, so a deployment that silently loses the check is still visible in
    the trace.
    """

    launcher = parse_uv_run_launcher(spec.command, spec.args)
    if launcher is None:
        _record_skip(_SKIP_LAUNCHER_SHAPE_UNMODELED, blueprint_id, server=name)
        return None
    if not launcher.sync_disabled:
        _record_skip(_SKIP_LAUNCHER_SYNCS_ON_START, blueprint_id, server=name)
        return None
    if _environment_is_redirected(spec, env=env):
        _record_skip(_SKIP_LAUNCHER_ENVIRONMENT_REDIRECTED, blueprint_id, server=name)
        return None
    state = entrypoint_venv_state(launcher.project_dir, launcher.entrypoint)
    if not state:
        return None
    return WatcherArmingRefusal(
        reason=REFUSAL_WATCHER_ENTRYPOINT_MISSING,
        detail=(
            f"MCP server {name!r}: 'uv run --project {launcher.project_dir} --no-sync "
            f"{launcher.entrypoint}' cannot start because "
            f"{_VENV_STATE_DETAIL[state]} (run 'uv sync --project {launcher.project_dir}')"
        ),
        blueprint_id=blueprint_id,
        server=name,
        path=launcher.project_dir,
        entrypoint=launcher.entrypoint,
        venv_state=state,
    )


def _refusal_for_declaration(
    blueprint_id: str,
    name: str,
    declaration: Any,
    *,
    env: Optional[Mapping[str, str]],
) -> Optional[WatcherArmingRefusal]:
    """Validate ONE declared MCP server; ``None`` when it resolves."""

    from clio_agent.tools.mcp_config import spec_from_declaration  # noqa: PLC0415

    spec = spec_from_declaration(
        name, declaration, source=f"agent-blueprint:{blueprint_id}", env=env
    )
    if spec.validation_errors:
        return WatcherArmingRefusal(
            reason=REFUSAL_WATCHER_UNMOUNTABLE,
            detail="; ".join(spec.validation_errors),
            blueprint_id=blueprint_id,
            server=name,
            variable=_unset_variable(spec.validation_errors),
        )
    if spec.transport != "stdio":
        return None
    for candidate in _project_directories(spec.args):
        if not candidate or not Path(candidate).expanduser().is_dir():
            return WatcherArmingRefusal(
                reason=REFUSAL_WATCHER_PROJECT_MISSING,
                detail=(
                    f"MCP server {name!r}: project directory {candidate!r} does not exist, "
                    f"so the stdio subprocess cannot start"
                ),
                blueprint_id=blueprint_id,
                server=name,
                path=candidate,
            )
    return _entrypoint_refusal(blueprint_id, name, spec, env=env)


def validate_watcher_arming(
    app: "FastAPI",
    *,
    session_id: str = "",
    workspace_id: str = "",
    blueprint_id: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> Optional[WatcherArmingRefusal]:
    """Statically check that the spotter watcher could actually execute.

    Args:
        app: The GACT app (workspace store, for the blueprint scan root).
        session_id: The session being armed, used to resolve its workspace's
            blueprint scan root. Empty falls back to ``workspace_id``.
        workspace_id: Explicit workspace scope when no session id is available.
        blueprint_id: The watcher Agent Blueprint to validate; empty resolves
            the configured one (``spotter.watcher_blueprint_id``).
        env: Environment mapping for ``${VAR}`` expansion. ``None`` (the
            runtime default) reads the real process environment; tests inject a
            mapping so they never mutate ambient env.

    Returns:
        The FIRST :class:`WatcherArmingRefusal` in declaration order, or
        ``None`` when every declared server resolves (including the "nothing
        declared" case, which is typed-logged rather than refused). Fail-fast is
        inherent: ``${VAR}`` expansion stops at the first unset variable, so a
        deployment missing two variables is fixed one named variable at a time.
    """

    from clio_agent.gact.spotter_watcher import _watcher_blueprint_id  # noqa: PLC0415

    resolved_id = blueprint_id or _watcher_blueprint_id()
    if not resolved_id:
        return None
    servers = _declared_watcher_servers(
        app, resolved_id, session_id=session_id, workspace_id=workspace_id
    )
    for name, declaration in servers.items():
        refusal = _refusal_for_declaration(resolved_id, str(name), declaration, env=env)
        if refusal is not None:
            return refusal
    return None


def refuse_watcher_arming(
    app: "FastAPI",
    session: "Session",
    refusal: WatcherArmingRefusal,
    *,
    prior_approval_mode: str = "",
) -> NoReturn:
    """Undo the just-persisted spotter-ai transition and raise the typed 422.

    Args:
        app: The GACT app (session store, for the rollback).
        session: The session whose transition into ``spotter-ai`` is refused.
        refusal: The typed reason from :func:`validate_watcher_arming`.
        prior_approval_mode: The mode the session held BEFORE this request.
            Empty means a fresh CREATE (there is no prior mode), whose rollback
            is deleting the just-minted row; otherwise the mode is restored.

    Raises:
        fastapi.HTTPException: Always — HTTP 422 whose ``ErrorInfo.error`` IS
            the stable reason, with the unresolvable spec detail in ``details``.
    """

    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415

    session_id = session.id
    if prior_approval_mode:
        app.state.sessions.update(session_id, approval_mode=prior_approval_mode)
        rollback = f"approval_mode_restored:{prior_approval_mode}"
    else:
        app.state.sessions.delete(session_id)
        rollback = "session_deleted"
    logger.warning(
        "spotter_watcher_arm_refused reason=%s session=%s blueprint=%s server=%s "
        "variable=%s path=%s entrypoint=%s venv_state=%s rollback=%s detail=%s",
        refusal.reason,
        session_id,
        refusal.blueprint_id,
        refusal.server,
        refusal.variable,
        refusal.path,
        refusal.entrypoint,
        refusal.venv_state,
        rollback,
        refusal.detail,
    )
    trace.event(
        "SPOTTER",
        "spotter_watcher_arm_refused reason=%s session=%s blueprint=%s server=%s "
        "variable=%s path=%s entrypoint=%s venv_state=%s rollback=%s detail=%s",
        refusal.reason,
        session_id,
        refusal.blueprint_id,
        refusal.server,
        refusal.variable,
        refusal.path,
        refusal.entrypoint,
        refusal.venv_state,
        rollback,
        refusal.detail,
    )
    raise HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=refusal.reason,
                message=refusal.message,
                details=refusal.details(session_id),
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )
