"""Confine every agent-driven child spawn behind one typed backend ladder (#974).

Sibling + stylistic twin of :mod:`clio_agent.runtime.process_tree`: that owns "the child tree
dies with the server", THIS owns "the child writes only inside its territory". The single
composition point every agent-driven ``Popen`` / ``StdioTransport`` routes through —
:func:`wrap_confined` — takes a resolved ``(command, args)`` and returns a :class:`ConfinedSpawn`.

BACKENDS (owner decision #974.1): **srt** (default; Seatbelt/bwrap+proxy/Windows ACL-WFP —
:mod:`~clio_agent.runtime.sandbox_srt`), **Codex** (flag-gated ``CLIO_SANDBOX_BACKEND=codex``, all
platforms — :mod:`~clio_agent.runtime.sandbox_codex`), **native Landlock** (the bwrap-broken rung),
and **none** (the honest floor: no OS fence; :mod:`clio_agent.tools.file_policy` is the ADVISORY
twin). COMPOSITION (#974.5): fence prefix INNER, ``pdeathsig`` OUTERMOST. DENIAL: an active fence
refuses an out-of-territory write as ``EROFS`` / ``EACCES`` / ``WinError 5``; the floor lets it
happen and #966's ``gap`` node records ``sandbox: none/<reason>`` (fenced tiers upgrade it).
"""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

#: The npm package that provides the srt runtime.
SRT_PACKAGE_NAME = "@anthropic-ai/sandbox-runtime"
#: The CLI binary the package installs (resolved on PATH unless overridden by config).
SRT_BINARY_NAME = "srt"
#: Latest srt release verified against this ladder (#974); the doctor row cites it, not a floor.
SRT_LATEST_KNOWN_VERSION = "0.0.66"
#: srt requires node >= 20.11 (owner note #974). A ``(major, minor)`` floor.
SRT_MIN_NODE_VERSION = (20, 11)

MECHANISM_SRT_SEATBELT = "srt_seatbelt"  # srt on macOS
MECHANISM_SRT_BWRAP = "srt_bwrap"  # srt on Linux (bubblewrap + proxy)
MECHANISM_SRT_WINDOWS = "srt_windows"  # srt on Windows (ACL/WFP)
MECHANISM_LANDLOCK = "landlock"  # native Linux Landlock fs-fence (B2)
MECHANISM_CODEX = "codex"  # OpenAI Codex sandbox (restricted-token/AppContainer + Seatbelt/bwrap)
MECHANISM_NONE = "none"  # the honest floor: no OS fence

#: All mechanism labels this ladder can ever report (the doctor validates against it).
KNOWN_MECHANISMS: frozenset[str] = frozenset(
    {
        MECHANISM_SRT_SEATBELT,
        MECHANISM_SRT_BWRAP,
        MECHANISM_SRT_WINDOWS,
        MECHANISM_LANDLOCK,
        MECHANISM_CODEX,
        MECHANISM_NONE,
    }
)

REASON_SRT_NOT_INSTALLED = "srt_not_installed"
REASON_SRT_NODE_MISSING = "srt_node_missing"
REASON_SRT_NODE_TOO_OLD = "srt_node_too_old"
REASON_SRT_NODE_VERSION_UNREADABLE = "srt_node_version_unreadable"
REASON_SRT_SOCAT_MISSING = "srt_socat_missing"
REASON_SRT_DETECTED_DEFERRED = "srt_detected_activation_deferred"
REASON_BWRAP_USERNS_RESTRICTED = "bwrap_userns_restricted"  # B2 rung (Ubuntu 24.04+)
REASON_BWRAP_UNAVAILABLE = "bwrap_unavailable"  # B2 rung (bwrap binary absent → Landlock)
REASON_KERNEL_TOO_OLD = "kernel_too_old"  # B2 rung (no Landlock)
REASON_LANDLOCK_UNAVAILABLE = "landlock_unavailable"  # B2 rung (Landlock off / not compiled)
REASON_LANDLOCK_DEFERRED = "landlock_activation_deferred"  # unused post-B2 (kept for compat)
REASON_SRT_VERSION_UNSUPPORTED = "srt_version_unsupported"  # B2: srt below the validated floor
REASON_CHOKEPOINT_START_FAILED = "chokepoint_start_failed"  # B2: proxy down → drop the srt rung
REASON_WINDOWS_UNPROVISIONED = "windows_unprovisioned"  # B3 rung (needs `clio sandbox setup`)
REASON_DISABLED = "disabled_by_config"
REASON_NOT_INSTALLED = "sandbox_not_installed"  # wrap_confined ran before install_sandbox()
#: Positive reason token stamped on an ACTIVE fence (reason is never blank — house rule).
REASON_FENCE_ACTIVE = "fence_active"

# Network-enforcement labels (owner decision #974.3/#974.7 — honest per tier).
#: On an srt tier the OS fence FORCES children through the clio proxy → real enforcement.
NET_ENFORCEMENT_PROXY = "proxy"
#: On the Codex/Landlock/floor tier egress is proxy-ENV cooperation only (raw sockets bypass) —
#: the record says so, per-edge (never claim proxy enforcement off the srt tier).
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

# Confinement classification of a census child by kind (#975): makes the EXCLUDED seams
# (CTE daemon, provider CLI links, serve.py — never wrapped, #974.5) visible policy.
Confinement = Literal["wrapped", "excluded"]
CONFINEMENT_WRAPPED: Confinement = "wrapped"
CONFINEMENT_EXCLUDED: Confinement = "excluded"

#: Census child kinds descending from a wrapped seam (the MCP fleet + python MCP servers).
_WRAPPED_KINDS: frozenset[str] = frozenset({"mcp_stdio", "mcp_launcher", "python_child"})
#: Kinds EXCLUDED from confinement: the clio-core daemon (breakaway is load-bearing) + the
#: provider LLM CLI links (claude/codex need the network + their own cache territory).
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
    ``active`` is ``True`` only for a live srt / Codex / Landlock backend; ``reason`` is a
    machine-stable token (e.g. :data:`REASON_FENCE_ACTIVE`); ``details`` is structured evidence.
    """

    mechanism: str
    active: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SrtDetection:
    """What the srt probe found (detection only — never activates anything).

    ``version`` is from the npm ``package.json`` (NEVER the lying ``srt --version`` banner);
    ``node_ok`` is ``node_version`` vs :data:`SRT_MIN_NODE_VERSION`; ``reason`` is the typed rung.
    """

    installed: bool
    binary_path: str
    version: str
    node_present: bool
    node_version: str
    node_ok: bool
    socat_present: bool
    reason: str


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


def _sandbox_backend(env: Optional[Mapping[str, str]] = None) -> str:
    """Which OS-fence backend the ladder resolves (config ``sandbox.backend``).

    ``"codex"`` selects the flag-gated Codex fence (opt-in, all platforms); ``"srt"`` (the default)
    keeps the existing srt→Landlock ladder UNCHANGED. Mirrors :func:`_sandbox_enabled`'s env/config
    read; an unrecognized value falls to ``"srt"``.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    if env is not None:
        raw = (env.get("CLIO_SANDBOX_BACKEND", "") or "").strip().lower()
        return raw if raw in ("codex", "srt") else "srt"
    value = (
        conf.resolve("sandbox.backend", env="CLIO_SANDBOX_BACKEND", default="srt", cast=conf.as_str)
        .strip()
        .lower()
    )
    return value if value in ("codex", "srt") else "srt"


def _srt_path_override(env: Optional[Mapping[str, str]] = None) -> str:
    """An explicit ``srt`` binary path (config ``sandbox.srt_path``), else ``""`` (resolve PATH)."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    if env is not None:
        return (env.get("CLIO_SANDBOX_SRT_PATH", "") or "").strip()
    return conf.resolve(
        "sandbox.srt_path", env="CLIO_SANDBOX_SRT_PATH", default="", cast=conf.as_str
    ).strip()


def _srt_package_version(binary_path: str) -> str:
    """Read the srt package version from its ``package.json`` — NEVER ``srt --version``.

    ``srt --version`` prints a stale ``1.0.0`` banner (owner note #974). The binary is (usually) a
    shim under a node ``bin`` dir into ``.../node_modules/@anthropic-ai/sandbox-runtime/``; walk
    the real path's ancestors for that ``package.json`` (``""`` when none — an honest empty).
    """
    if not binary_path:
        return ""
    import json  # noqa: PLC0415 - only needed on the detection path

    real = Path(binary_path)
    try:
        real = real.resolve()
    except OSError:
        pass
    pkg_leaf = SRT_PACKAGE_NAME.split("/")[-1]
    scope = SRT_PACKAGE_NAME.split("/")[0] if "/" in SRT_PACKAGE_NAME else ""
    # Search the binary's own ancestors and any adjacent node_modules for the package.
    for parent in [real, *real.parents]:
        candidates = [
            parent / "package.json",
            parent / "node_modules" / scope / pkg_leaf / "package.json",
            parent / "node_modules" / SRT_PACKAGE_NAME / "package.json",
        ]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(data.get("name") or "") == SRT_PACKAGE_NAME:
                return str(data.get("version") or "")
    logger.info("srt version unresolved reason=srt_package_json_not_found binary=%s", binary_path)
    return ""


def _node_version_tuple(version: str) -> tuple[int, int]:
    """Parse a ``node --version`` string (``vMAJOR.MINOR.PATCH``) to ``(major, minor)``."""
    cleaned = version.strip().lstrip("vV")
    parts = cleaned.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (IndexError, ValueError):
        return (0, 0)
    return (major, minor)


def _read_node_version() -> str:
    """Return ``node --version`` (truthful, unlike srt) or ``""`` — best-effort, short timeout."""
    node = shutil.which("node")
    if not node:
        return ""
    import subprocess  # noqa: PLC0415 - only on the detection path

    try:
        out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("node version probe failed reason=srt_node_version_unreadable error=%r", exc)
        return ""
    return (out.stdout or "").strip()


def detect_srt(
    *,
    env: Optional[Mapping[str, str]] = None,
    which: Any = shutil.which,
    package_version: Any = _srt_package_version,
    node_version_reader: Any = _read_node_version,
    platform: str = sys.platform,
) -> SrtDetection:
    """Probe for the srt runtime + its preconditions (node/socat). DETECTION ONLY.

    Never spawns srt and never activates a fence. Every dependency is injectable so the ladder is
    unit-testable without a real srt/node install. The returned :attr:`SrtDetection.reason` is the
    typed ladder reason the missing/present fence implies (:func:`_resolve_backend` maps it).
    """
    override = _srt_path_override(env)
    # win32: prefer launchable ``srt.cmd``/``srt.exe`` (an extensionless POSIX shim fails CreateProcess).
    _names = (
        ("srt.cmd", "srt.exe", SRT_BINARY_NAME)
        if platform.startswith("win")
        else (SRT_BINARY_NAME,)
    )
    binary = override or next((p for p in (which(n) for n in _names) if p), "")
    if not binary:
        return SrtDetection(
            installed=False,
            binary_path="",
            version="",
            node_present=bool(which("node")),
            node_version="",
            node_ok=False,
            socat_present=bool(which("socat")),
            reason=REASON_SRT_NOT_INSTALLED,
        )

    version = package_version(binary)
    node_present = bool(which("node"))
    node_version = node_version_reader() if node_present else ""
    node_ok = _node_version_tuple(node_version) >= SRT_MIN_NODE_VERSION if node_version else False
    socat_present = bool(which("socat"))

    if not node_present:
        reason = REASON_SRT_NODE_MISSING
    elif not node_version:
        # node on PATH but its version was unreadable (logged) — NOT the same claim as "too old".
        reason = REASON_SRT_NODE_VERSION_UNREADABLE
    elif not node_ok:
        reason = REASON_SRT_NODE_TOO_OLD
    elif platform.startswith("linux") and not socat_present:
        reason = REASON_SRT_SOCAT_MISSING
    else:
        # srt + all preconditions present. B2/B3 will ACTIVATE it; this slice defers.
        reason = REASON_SRT_DETECTED_DEFERRED
    return SrtDetection(
        installed=True,
        binary_path=binary,
        version=version,
        node_present=node_present,
        node_version=node_version,
        node_ok=node_ok,
        socat_present=socat_present,
        reason=reason,
    )


def _target_mechanism_for_platform(platform: str) -> str:
    """The srt mechanism label that WOULD govern this platform once activated (B2/B3)."""
    if platform == "darwin":
        return MECHANISM_SRT_SEATBELT
    if platform.startswith("linux"):
        return MECHANISM_SRT_BWRAP
    if platform.startswith("win"):
        return MECHANISM_SRT_WINDOWS
    return MECHANISM_NONE


def _probe_bwrap_userns(platform: str) -> tuple[bool, str]:
    """Whether srt's Linux bwrap substrate can run unprivileged (owner decision #974.3).

    Non-Linux is not-applicable. On Linux the two known breakers — a missing ``bwrap`` binary
    (:data:`REASON_BWRAP_UNAVAILABLE`) and the Ubuntu 24.04+ AppArmor userns knob
    (:data:`REASON_BWRAP_USERNS_RESTRICTED`) — each drop the ladder to the Landlock rung.
    """
    if not platform.startswith("linux"):
        return True, ""
    if not shutil.which("bwrap"):
        return False, REASON_BWRAP_UNAVAILABLE
    knob = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    try:
        if knob.is_file() and knob.read_text(encoding="utf-8").strip() == "1":
            return False, REASON_BWRAP_USERNS_RESTRICTED
    except OSError:
        pass
    return True, ""


def _default_start_proxy() -> int:
    """Start (once) the clio network chokepoint and return its loopback port."""
    from clio_agent.runtime.net_chokepoint import install_chokepoint  # noqa: PLC0415

    return install_chokepoint().port


def _srt_viability(det: SrtDetection) -> tuple[bool, str]:
    """Whether srt is activatable + the typed skip reason when not (B2).

    Viable iff every precondition is met AND the installed version is at/above the validated floor;
    else the skip reason is :data:`REASON_SRT_VERSION_UNSUPPORTED` or the detection verdict.
    """
    from clio_agent.runtime.sandbox_srt import is_srt_version_supported  # noqa: PLC0415

    if det.reason != REASON_SRT_DETECTED_DEFERRED:
        return False, det.reason
    if not is_srt_version_supported(det.version):
        return False, REASON_SRT_VERSION_UNSUPPORTED
    return True, ""


def _resolve_backend(
    *,
    env: Optional[Mapping[str, str]] = None,
    platform: str = sys.platform,
    detection: Optional[SrtDetection] = None,
    codex_detection: Any = None,
    codex_provisioned_probe: Any = None,
    bwrap: Optional[tuple[bool, str]] = None,
    landlock: Any = None,
    start_proxy: Any = None,
    win_state: Any = None,
) -> SandboxResult:
    """Resolve + ACTIVATE the confinement backend down the typed ladder (#976, B2).

    When ``CLIO_SANDBOX_BACKEND=codex`` (flag-gated), the Codex rung resolves FIRST on all
    platforms (via injectable ``codex_detection``; win32 additionally gates on the cached
    ``codex_provisioned_probe`` — provisioned + enforcement-verified, #1026), else a typed
    ``codex_*`` floor (``codex_windows_unprovisioned`` / ``codex_enforcement_unverified``). The DEFAULT
    ``srt`` ladder is UNCHANGED: **srt** (Seatbelt / bwrap+proxy) → **Landlock** → **none**. Every
    rung change carries a typed reason; all probes are injectable so the matrix is unit-pinnable.
    ``start_proxy`` runs only for an srt tier, and a typed
    :class:`~clio_agent.runtime.net_chokepoint.ChokepointStartError` drops the srt rung rather than
    losing all network. Windows (B3, #977): a three-way ``win_state`` branch (provisioned→
    srt_windows, srt_absent→typed reason, unprovisioned→floor).
    """
    det = detection if detection is not None else detect_srt(env=env, platform=platform)
    base_details: dict[str, Any] = {
        "platform": platform,
        "target_mechanism": _target_mechanism_for_platform(platform),
        "srt": {
            "package": SRT_PACKAGE_NAME,
            "installed": det.installed,
            "binary_path": det.binary_path,
            "version": det.version,
            "node_present": det.node_present,
            "node_version": det.node_version,
            "node_ok": det.node_ok,
            "socat_present": det.socat_present,
        },
    }

    def floor(reason: str, **extra: Any) -> SandboxResult:
        return SandboxResult(
            mechanism=MECHANISM_NONE, active=False, reason=reason, details={**base_details, **extra}
        )

    if not _sandbox_enabled(env):
        return floor(REASON_DISABLED)

    # Codex backend (flag-gated, all platforms): resolve BEFORE the srt/Landlock ladder — viable
    # detect_codex → active (win32 additionally gates on provisioning + verify #1026), else floor.
    if _sandbox_backend(env) == "codex":
        from clio_agent.runtime import sandbox_codex as scx  # noqa: PLC0415

        cdet = (
            codex_detection if codex_detection is not None else scx.detect_codex(platform=platform)
        )
        if not (cdet.installed and cdet.reason == scx.REASON_CODEX_DETECTED):
            return floor(cdet.reason)  # typed: codex_not_installed / codex_version_unsupported
        if platform.startswith("win"):  # gate on cached provision + verify (#1026 no-false-green)
            ready, creason = (codex_provisioned_probe or scx.codex_windows_gate)()
            if not ready:
                return floor(creason)  # codex_windows_unprovisioned / codex_enforcement_unverified
        return _activate_codex(cdet, base_details)

    # Windows (B3): the provisioning verdict drives the rung (see the docstring's 3-way branch).
    if platform.startswith("win"):
        from clio_agent.runtime import sandbox_provision as swp  # noqa: PLC0415

        win = win_state or swp.windows_sandbox_state(platform=platform, detection=det)
        if win.status == swp.STATUS_PROVISIONED:
            activated = _activate_srt(MECHANISM_SRT_WINDOWS, det, base_details, start_proxy)
            return activated if activated is not None else floor(REASON_CHOKEPOINT_START_FAILED)
        if win.status in (swp.STATUS_SRT_ABSENT, swp.STATUS_ENFORCEMENT_UNVERIFIED):
            return floor(
                win.reason
            )  # typed floor: srt gap OR provisioned-but-cannot-enforce (#1026)
        return floor(REASON_WINDOWS_UNPROVISIONED)

    srt_ok, srt_skip = _srt_viability(det)

    # macOS: srt (Seatbelt) or floor (no Landlock rung on darwin).
    if platform == "darwin":
        if srt_ok:
            activated = _activate_srt(MECHANISM_SRT_SEATBELT, det, base_details, start_proxy)
            if activated is not None:
                return activated
            srt_skip = REASON_CHOKEPOINT_START_FAILED
        return floor(srt_skip)

    # Linux: srt(bwrap) → Landlock → floor.
    bwrap_ok, bwrap_reason = bwrap if bwrap is not None else _probe_bwrap_userns(platform)
    if srt_ok and bwrap_ok:
        activated = _activate_srt(MECHANISM_SRT_BWRAP, det, base_details, start_proxy)
        if activated is not None:
            return activated
        srt_skip = REASON_CHOKEPOINT_START_FAILED  # proxy down → drop to Landlock
    elif srt_ok and not bwrap_ok:
        srt_skip = bwrap_reason  # srt viable but bwrap broken → Landlock rung

    from clio_agent.runtime.sandbox_landlock import probe_landlock  # noqa: PLC0415

    probe = landlock if landlock is not None else probe_landlock(platform=platform)
    base_details["srt_skip_reason"] = srt_skip
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
    return floor(probe.reason or srt_skip)


def _activate_srt(
    mechanism: str,
    det: SrtDetection,
    base_details: dict[str, Any],
    start_proxy: Any,
) -> Optional[SandboxResult]:
    """Activate an srt tier — start the chokepoint, stamp the active result (or ``None``).

    Returns ``None`` (caller drops a rung with :data:`REASON_CHOKEPOINT_START_FAILED`) when the
    chokepoint cannot start: an srt child with no proxy would silently lose all network.
    """
    from clio_agent.runtime.net_chokepoint import ChokepointStartError  # noqa: PLC0415

    fn = start_proxy if start_proxy is not None else _default_start_proxy
    try:
        proxy_port = int(fn())
    except ChokepointStartError as exc:
        logger.warning("srt rung dropped reason=%s error=%r", exc.reason, exc)
        return None
    return SandboxResult(
        mechanism=mechanism,
        active=True,
        reason=REASON_FENCE_ACTIVE,
        details={
            **base_details,
            "srt_binary": det.binary_path,
            "srt_version": det.version,
            "proxy_port": proxy_port,
            "net_enforcement": NET_ENFORCEMENT_PROXY,
        },
    )


def _activate_codex(det: Any, base_details: dict[str, Any]) -> SandboxResult:
    """Stamp an ACTIVE Codex write-fence result (network egress DEFERRED to a later slice).

    Unlike :func:`_activate_srt` this needs NO chokepoint proxy; the write-fence activates now and
    ``net_enforcement`` honestly records ``codex-net-deferred`` (a typed value, never a silent gap).
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
    """Compose the active fence's argv prefix around ``(command, args)`` (B2/B4).

    Per active mechanism: srt writes/validates the settings then prepends ``srt -s <cfg> --``;
    Codex delegates to :func:`sandbox_codex.compose_codex_spawn`; Landlock prepends its shim. Any
    failure raises :class:`SandboxCompositionError` so the spawn fails loud, never unconfined.
    """
    mechanism = state.mechanism
    roots: list[str] = [str(r) for r in write_roots]
    try:
        if mechanism in (MECHANISM_SRT_BWRAP, MECHANISM_SRT_SEATBELT, MECHANISM_SRT_WINDOWS):
            from clio_agent.runtime import sandbox_srt  # noqa: PLC0415

            # B4: the child's ``httpProxyPort`` is its OWN chokepoint channel port, else the shared.
            port = proxy_port if proxy_port is not None else state.details.get("proxy_port")
            config = sandbox_srt.synthesize_srt_config(roots, http_proxy_port=port)
            settings = sandbox_srt.settings_path_for(profile, config=config)
            sandbox_srt.write_settings_file(config, settings)  # validates (typed on reject)
            binary = str(state.details.get("srt_binary") or SRT_BINARY_NAME)
            prefix = sandbox_srt.srt_prefix(binary, settings)
            return prefix[0], [*prefix[1:], command, *args]
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

    # srt Windows fs policy is SESSION-WIDE (#977): shell can't narrow below fleet → REUSES it.
    compose_profile: Profile = profile
    windows_note = ""
    if resolved_state.mechanism == MECHANISM_SRT_WINDOWS and profile == PROFILE_SHELL:
        compose_profile = PROFILE_FLEET
        windows_note = "shell reuses fleet (srt Windows fs policy is session-wide)"
        write_roots = (*effective_write_roots(PROFILE_FLEET), *(Path(r) for r in write_roots))

    # Active backend: fence prefix INNER (pdeathsig outermost below). B4's per-child egress channel
    # is srt/Landlock ONLY; codex net is DEFERRED — skip the proxy (it would hang) and compose only.
    if resolved_state.active and resolved_state.mechanism != MECHANISM_NONE:
        proxy_port: Optional[int] = None
        if resolved_state.mechanism != MECHANISM_CODEX:
            from clio_agent.runtime import sandbox_net  # noqa: PLC0415 - B4 egress-wiring sibling

            net_child_id, proxy_port, net_env = sandbox_net.open_child_egress(
                resolved_state, write_roots
            )
            env_overlay.update(net_env)
        cmd, arg_list = _compose_fence_prefix(
            resolved_state, compose_profile, cmd, arg_list, write_roots, proxy_port=proxy_port
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
            "compose_profile": compose_profile,
            "net_policy": net_policy,
            "pdeathsig": pdeathsig,
            "write_roots": [str(r) for r in write_roots],
            **({"windows_profile_reuse": windows_note} if windows_note else {}),
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
    "SRT_PACKAGE_NAME",
    "SRT_BINARY_NAME",
    "SRT_LATEST_KNOWN_VERSION",
    "SRT_MIN_NODE_VERSION",
    "MECHANISM_SRT_SEATBELT",
    "MECHANISM_SRT_BWRAP",
    "MECHANISM_SRT_WINDOWS",
    "MECHANISM_LANDLOCK",
    "MECHANISM_CODEX",
    "MECHANISM_NONE",
    "KNOWN_MECHANISMS",
    "REASON_SRT_NOT_INSTALLED",
    "REASON_SRT_NODE_MISSING",
    "REASON_SRT_NODE_TOO_OLD",
    "REASON_SRT_NODE_VERSION_UNREADABLE",
    "REASON_SRT_SOCAT_MISSING",
    "REASON_SRT_DETECTED_DEFERRED",
    "REASON_BWRAP_USERNS_RESTRICTED",
    "REASON_BWRAP_UNAVAILABLE",
    "REASON_KERNEL_TOO_OLD",
    "REASON_LANDLOCK_UNAVAILABLE",
    "REASON_LANDLOCK_DEFERRED",
    "REASON_SRT_VERSION_UNSUPPORTED",
    "REASON_CHOKEPOINT_START_FAILED",
    "REASON_WINDOWS_UNPROVISIONED",
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
    "SrtDetection",
    "ConfinedSpawn",
    "SandboxCompositionError",
    "confinement_for_kind",
    "detect_srt",
    "effective_write_roots",
    "wrap_confined",
    "install_sandbox",
    "current_state",
    "emit_boot_state_event",
    "probe_sandbox",
]
