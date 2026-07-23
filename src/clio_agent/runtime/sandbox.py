"""Confine every agent-driven child spawn behind one typed backend ladder (#974).

Sibling + stylistic twin of :mod:`clio_agent.runtime.process_tree`: that owns "the child tree
dies with the server", THIS owns "the child writes only inside its territory". The single
composition point every agent-driven ``Popen`` / ``StdioTransport`` routes through —
:func:`wrap_confined` — takes a resolved ``(command, args)`` and returns a :class:`ConfinedSpawn`.

BACKENDS (B-codex-5): **Codex** is the SOLE OS-fence backend on every platform — restricted-token /
dedicated sandbox user + ACLs on native Windows, Seatbelt/bubblewrap on mac/Linux
(:mod:`~clio_agent.runtime.sandbox_codex`). When Codex is not viable the ladder falls, on Linux
only, to the native **Landlock** rung (:mod:`~clio_agent.runtime.sandbox_landlock`), then to
**none** (the honest floor: no OS fence; :mod:`clio_agent.tools.file_policy` is the ADVISORY twin).
The srt backend was deleted here: its native-Windows fence never enforced (proven broken on a clean
box) and Codex is now live-validated on Windows and Linux, making srt redundant. COMPOSITION
(#974.5): fence prefix INNER, ``pdeathsig`` OUTERMOST. DENIAL: an active fence refuses an
out-of-territory write as ``EROFS`` / ``EACCES`` / ``WinError 5``; the floor lets it happen and
#966's ``gap`` node records ``sandbox: none/<reason>`` (fenced tiers upgrade it).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

MECHANISM_LANDLOCK = "landlock"  # native Linux Landlock fs-fence (B2)
MECHANISM_CODEX = "codex"  # OpenAI Codex sandbox (restricted-token/AppContainer + Seatbelt/bwrap)
MECHANISM_NONE = "none"  # the honest floor: no OS fence

#: All mechanism labels this ladder can ever report (the doctor validates against it).
KNOWN_MECHANISMS: frozenset[str] = frozenset(
    {
        MECHANISM_LANDLOCK,
        MECHANISM_CODEX,
        MECHANISM_NONE,
    }
)

REASON_KERNEL_TOO_OLD = "kernel_too_old"  # Landlock rung (no Landlock)
REASON_LANDLOCK_UNAVAILABLE = "landlock_unavailable"  # Landlock rung (Landlock off / not compiled)
REASON_LANDLOCK_DEFERRED = "landlock_activation_deferred"  # kept for compat
REASON_DISABLED = "disabled_by_config"
REASON_NOT_INSTALLED = "sandbox_not_installed"  # wrap_confined ran before install_sandbox()
#: Positive reason token stamped on an ACTIVE fence (reason is never blank — house rule).
REASON_FENCE_ACTIVE = "fence_active"

# Network-enforcement labels (owner decision #974.3/#974.7 — honest per tier).
#: On a proxy-forcing tier the OS fence FORCES children through the clio proxy → real enforcement.
#: (No current backend forces this; kept as the honest positive label for :mod:`sandbox_net`.)
NET_ENFORCEMENT_PROXY = "proxy"
#: On the Codex/Landlock/floor tier egress is proxy-ENV cooperation only (raw sockets bypass) —
#: the record says so, per-edge (never claim proxy enforcement off a forcing tier).
NET_ENFORCEMENT_ENV_COOPERATIVE = "env-cooperative"

# Profiles live in the sandbox_roots sibling (the shared-boundary owner); re-exported here.
from clio_agent.runtime.sandbox_roots import (  # noqa: E402
    PROFILE_FLEET,
    PROFILE_SHELL,
    Profile,
    effective_write_roots,
)

#: Network default is ALLOW + RECORD (owner decision #974.3); deny-by-default is an
#: opt-in per-workspace mode wired in B4. Recorded here, enforced later.
NetPolicy = Literal["allow_record", "deny"]
NET_ALLOW_RECORD: NetPolicy = "allow_record"
NET_DENY: NetPolicy = "deny"

# Confinement classification by census kind (#975): makes EXCLUDED seams visible policy (#974.5).
Confinement = Literal["wrapped", "excluded"]
CONFINEMENT_WRAPPED: Confinement = "wrapped"
CONFINEMENT_EXCLUDED: Confinement = "excluded"

#: Census child kinds descending from a wrapped seam (the MCP fleet + python MCP servers).
_WRAPPED_KINDS: frozenset[str] = frozenset({"mcp_stdio", "mcp_launcher", "python_child"})
#: Kinds EXCLUDED from confinement: the clio-core daemon + provider LLM CLI links (own net/cache).
_EXCLUDED_KINDS: frozenset[str] = frozenset({"clio_core_daemon", "sdk_cli", "codex_cli"})


def confinement_for_kind(kind: str) -> Confinement:
    """Classify a census child ``kind`` as ``wrapped`` (via a :func:`wrap_confined` seam: MCP
    fleet / python MCP servers) or ``excluded`` (CTE daemon, provider CLI links, unknown) (#974.5).
    """
    if kind in _WRAPPED_KINDS:
        return CONFINEMENT_WRAPPED
    return CONFINEMENT_EXCLUDED


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of resolving the confinement backend (a typed, loggable reason).

    ``mechanism`` is one of :data:`KNOWN_MECHANISMS` (:data:`MECHANISM_NONE` on the floor);
    ``active`` is ``True`` only for a live Codex / Landlock backend; ``reason`` is a
    machine-stable token (e.g. :data:`REASON_FENCE_ACTIVE`); ``details`` is structured evidence.
    """

    mechanism: str
    active: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfinedSpawn:
    """A resolved spawn plan under confinement — what to actually launch (#975).

    ``command``/``args`` are the (possibly wrapped) argv; ``env_overlay`` are env keys the fence
    adds; ``popen_kwargs`` are extra ``subprocess`` kwargs; ``result`` is the :class:`SandboxResult`.
    """

    command: str
    args: list[str]
    env_overlay: dict[str, str]
    popen_kwargs: dict[str, Any]
    result: SandboxResult


def _sandbox_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether confinement resolution is enabled (config ``sandbox.enabled``).

    ``false`` stamps :data:`REASON_DISABLED` on the floor result (no fence resolves).
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    if env is not None:
        raw = env.get("CLIO_SANDBOX_ENABLED", "")
        if raw.strip():
            try:
                return conf.as_bool(raw)
            except ValueError:
                return True
        return True
    return conf.resolve(
        "sandbox.enabled", env="CLIO_SANDBOX_ENABLED", default=True, cast=conf.as_bool
    )


def _target_mechanism_for_platform(platform: str) -> str:
    """The mechanism that WOULD govern this platform when the primary (Codex) backend activates.

    Codex is the SOLE OS-fence backend on every platform (Seatbelt/bubblewrap on mac/Linux, a
    restricted-token/ACL fence on native Windows), so the target is always :data:`MECHANISM_CODEX`.
    """
    return MECHANISM_CODEX


def _resolve_backend(
    *,
    env: Optional[Mapping[str, str]] = None,
    platform: str = sys.platform,
    codex_detection: Any = None,
    codex_provisioned_probe: Any = None,
    landlock: Any = None,
) -> SandboxResult:
    """Resolve + ACTIVATE the confinement backend down the typed ladder (codex-primary, B-codex-5).

    Codex is the SOLE OS-fence backend and resolves FIRST on every platform (via injectable
    ``codex_detection``; win32 additionally gates on the cached ``codex_provisioned_probe`` —
    provisioned + enforcement-verified, #1026 no-false-green). When Codex is not viable the ladder
    falls, on **Linux** only, to the native **Landlock** rung → the honest **floor**; on
    win32/darwin it floors directly with the typed ``codex_*`` reason
    (``codex_not_installed`` / ``codex_version_unsupported`` / ``codex_windows_unprovisioned`` /
    ``codex_enforcement_unverified``). Every rung carries a typed reason; all probes are injectable
    so the whole selection matrix is unit-pinnable without a real fence.
    """
    from clio_agent.runtime import sandbox_codex as scx  # noqa: PLC0415

    base_details: dict[str, Any] = {
        "platform": platform,
        "target_mechanism": _target_mechanism_for_platform(platform),
    }

    def floor(reason: str, **extra: Any) -> SandboxResult:
        return SandboxResult(
            mechanism=MECHANISM_NONE, active=False, reason=reason, details={**base_details, **extra}
        )

    if not _sandbox_enabled(env):
        return floor(REASON_DISABLED)

    cdet = codex_detection if codex_detection is not None else scx.detect_codex(platform=platform)
    base_details["codex"] = {
        "installed": cdet.installed,
        "binary_path": cdet.binary_path,
        "version": cdet.version,
    }
    codex_viable = cdet.installed and cdet.reason == scx.REASON_CODEX_DETECTED
    if codex_viable:
        if platform.startswith("win"):  # gate on cached provision + verify (#1026 no-false-green)
            ready, creason = (codex_provisioned_probe or scx.codex_windows_gate)()
            if ready:
                return _activate_codex(cdet, base_details)
            return floor(creason)  # codex_windows_unprovisioned / codex_enforcement_unverified
        return _activate_codex(cdet, base_details)

    # Codex not viable → the typed skip reason (codex_not_installed / codex_version_unsupported).
    codex_skip = cdet.reason
    base_details["codex_skip_reason"] = codex_skip

    # Linux fallback: the native Landlock rung → floor. (win32/darwin have no fallback rung.)
    if platform.startswith("linux"):
        from clio_agent.runtime.sandbox_landlock import probe_landlock  # noqa: PLC0415

        probe = landlock if landlock is not None else probe_landlock(platform=platform)
        base_details["landlock"] = {
            "available": probe.available,
            "abi": probe.abi,
            "reason": probe.reason,
        }
        if probe.available:
            return SandboxResult(
                mechanism=MECHANISM_LANDLOCK,
                active=True,
                reason=REASON_FENCE_ACTIVE,
                details={
                    **base_details,
                    "net_enforcement": NET_ENFORCEMENT_ENV_COOPERATIVE,
                    "landlock_abi": probe.abi,
                },
            )
        return floor(probe.reason or codex_skip)

    return floor(codex_skip)


def _activate_codex(det: Any, base_details: dict[str, Any]) -> SandboxResult:
    """Stamp an ACTIVE Codex write-fence result (network egress DEFERRED to a later slice).

    The Codex write-fence needs NO chokepoint proxy; it activates now and ``net_enforcement``
    honestly records ``codex-net-deferred`` (a typed value, never a silent gap).
    """
    return SandboxResult(
        mechanism=MECHANISM_CODEX,
        active=True,
        reason=REASON_FENCE_ACTIVE,
        details={
            **base_details,
            "codex_binary": det.binary_path,
            "codex_version": det.version,
            "net_enforcement": "codex-net-deferred",
        },
    )


class SandboxCompositionError(RuntimeError):
    """A fence prefix could not be composed at spawn time — typed, loud (no silent hole)."""


def _compose_fence_prefix(
    state: SandboxResult,
    profile: Profile,
    command: str,
    args: list[str],
    write_roots: Sequence[Path] | Sequence[str],
    *,
    proxy_port: Optional[int] = None,
) -> tuple[str, list[str]]:
    """Compose the active fence's argv prefix around ``(command, args)`` (B-codex / Landlock).

    Per active mechanism: Codex delegates to :func:`sandbox_codex.compose_codex_spawn`; Landlock
    prepends its ``landlock_exec`` shim. Any failure raises :class:`SandboxCompositionError` so the
    spawn fails loud, never unconfined. ``profile``/``proxy_port`` are accepted for seam parity but
    are not consumed by the surviving backends (Codex/Landlock derive everything from the roots).
    """
    mechanism = state.mechanism
    roots: list[str] = [str(r) for r in write_roots]
    try:
        if mechanism == MECHANISM_CODEX:
            from clio_agent.runtime import sandbox_codex  # noqa: PLC0415

            binary = str(state.details.get("codex_binary") or sandbox_codex.CODEX_BINARY_NAME)
            return sandbox_codex.compose_codex_spawn(roots, command, args, binary=binary)
        if mechanism == MECHANISM_LANDLOCK:
            from clio_agent.runtime import sandbox_landlock  # noqa: PLC0415

            prefix = sandbox_landlock.landlock_shim_prefix(roots)
            return prefix[0], [*prefix[1:], command, *args]
    except Exception as exc:  # noqa: BLE001 — re-raised typed; a fence hole must never be silent
        raise SandboxCompositionError(
            f"fence composition failed mechanism={mechanism} reason={type(exc).__name__}: {exc}"
        ) from exc
    raise SandboxCompositionError(f"unknown active mechanism for composition: {mechanism}")


# The single spawn-composition point.
def wrap_confined(
    command: str,
    args: Sequence[str],
    *,
    write_roots: Sequence[Path] | Sequence[str] = (),
    net_policy: NetPolicy = NET_ALLOW_RECORD,
    profile: Profile,
    pdeathsig: bool = False,
    state: Optional[SandboxResult] = None,
) -> ConfinedSpawn:
    """Compose the confined spawn plan for a resolved ``(command, args)`` (#975/#976).

    THE single argv-composition owner (#974.5). On the floor the argv is byte-identical to the
    input (only ``pdeathsig`` where requested); on an ACTIVE backend the fence prefix composes
    INNER and ``pdeathsig`` stays OUTERMOST — ``pdeathsig( fence( argv ) )``. ``command``/``args``
    MUST be the FINAL resolved argv (wrap AFTER spawn-diet); ``write_roots`` is the writable
    territory. ``state`` defaults to :func:`current_state`, else a :data:`REASON_NOT_INSTALLED`
    floor; a fence that cannot compose RAISES (typed), never spawning unconfined.
    """
    resolved_state = state or current_state()
    if resolved_state is None:
        resolved_state = SandboxResult(
            mechanism=MECHANISM_NONE,
            active=False,
            reason=REASON_NOT_INSTALLED,
            details={"platform": sys.platform},
        )

    cmd: str = command
    arg_list: list[str] = list(args)
    env_overlay: dict[str, str] = {}
    popen_kwargs: dict[str, Any] = {}
    net_child_id = ""

    # Active backend: fence prefix INNER (pdeathsig outermost below). B4's per-child egress channel
    # is the Landlock/floor tier ONLY; codex net is DEFERRED — skip the proxy (it would hang) and
    # compose only.
    if resolved_state.active and resolved_state.mechanism != MECHANISM_NONE:
        proxy_port: Optional[int] = None
        if resolved_state.mechanism != MECHANISM_CODEX:
            from clio_agent.runtime import sandbox_net  # noqa: PLC0415 - B4 egress-wiring sibling

            net_child_id, proxy_port, net_env = sandbox_net.open_child_egress(
                resolved_state, write_roots
            )
            env_overlay.update(net_env)
        cmd, arg_list = _compose_fence_prefix(
            resolved_state, profile, cmd, arg_list, write_roots, proxy_port=proxy_port
        )

    # pdeathsig OUTERMOST (#974.5): the argv-prefix helper folds in last (passthrough sans setpriv).
    if pdeathsig:
        from clio_agent.tools.mcp_config import pdeathsig_wrapped_command  # noqa: PLC0415 - cycle

        cmd, arg_list = pdeathsig_wrapped_command(cmd, arg_list)

    per_call = replace(
        resolved_state,
        details={
            **resolved_state.details,
            "profile": profile,
            "net_policy": net_policy,
            "pdeathsig": pdeathsig,
            "write_roots": [str(r) for r in write_roots],
            # B4: the per-child egress channel id (empty on the floor) — so a caller can close it.
            "net_child_id": net_child_id,
        },
    )
    return ConfinedSpawn(
        command=cmd,
        args=arg_list,
        env_overlay=env_overlay,
        popen_kwargs=popen_kwargs,
        result=per_call,
    )


# The resolved backend for THIS process, cached at install (cf. process_tree.child_reaper_status).
_STATE: SandboxResult | None = None


def install_sandbox(*, env: Optional[Mapping[str, str]] = None) -> SandboxResult:
    """Resolve + cache the confinement backend for this process (call at server boot).

    Mirrors :func:`clio_agent.runtime.process_tree.install_child_reaper`. Recomputes the ladder
    each call (cheap probes) and caches it for :func:`current_state`. Missing/broken host tooling
    never raises (a locked-down host still boots), but a *malformed config value* DOES raise, like
    every config knob (a typo fails boot loud).
    """
    global _STATE
    result = _resolve_backend(env=env)
    _STATE = result
    logger.info(
        "sandbox resolved mechanism=%s active=%s reason=%s target=%s",
        result.mechanism,
        result.active,
        result.reason,
        result.details.get("target_mechanism"),
    )
    return result


def current_state() -> SandboxResult | None:
    """Return the confinement state resolved in THIS process, or ``None`` if never installed."""
    return _STATE


# Doctor probe: the ``sandbox`` row lives in the sandbox_doctor sibling (ratchet); re-exported.
from clio_agent.runtime.sandbox_doctor import emit_boot_state_event, probe_sandbox  # noqa: E402

__all__ = [
    "MECHANISM_LANDLOCK",
    "MECHANISM_CODEX",
    "MECHANISM_NONE",
    "KNOWN_MECHANISMS",
    "REASON_KERNEL_TOO_OLD",
    "REASON_LANDLOCK_UNAVAILABLE",
    "REASON_LANDLOCK_DEFERRED",
    "REASON_DISABLED",
    "REASON_NOT_INSTALLED",
    "REASON_FENCE_ACTIVE",
    "NET_ENFORCEMENT_PROXY",
    "NET_ENFORCEMENT_ENV_COOPERATIVE",
    "PROFILE_FLEET",
    "PROFILE_SHELL",
    "NET_ALLOW_RECORD",
    "NET_DENY",
    "CONFINEMENT_WRAPPED",
    "CONFINEMENT_EXCLUDED",
    "SandboxResult",
    "ConfinedSpawn",
    "SandboxCompositionError",
    "confinement_for_kind",
    "effective_write_roots",
    "wrap_confined",
    "install_sandbox",
    "current_state",
    "emit_boot_state_event",
    "probe_sandbox",
]
