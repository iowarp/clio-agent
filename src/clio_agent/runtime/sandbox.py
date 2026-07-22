"""Confine every agent-driven child spawn behind one typed backend ladder (#974/#975).

Sibling + stylistic twin of :mod:`clio_agent.runtime.process_tree`: that module owns "the
child tree dies with the server", THIS one owns "the child writes only inside its
territory". It is the single composition point every agent-driven ``Popen`` /
``StdioTransport`` routes through — :func:`wrap_confined` takes a resolved
``(command, args)`` and returns a :class:`ConfinedSpawn`.

FLOOR-FIRST (#975, this slice): all the plumbing lands with **zero behavioral change**.
:func:`wrap_confined` always resolves to the passthrough :data:`MECHANISM_NONE` backend on
every platform, so mechanism labels, the doctor row and the provenance ``environment``
field populate *before* any fence exists. The ladder is DETECTION-ONLY here — it probes
for srt + records a typed reason for the missing fence, never activating one. B2/B3
(#976/#977) turn the detected rungs into real fences; because #966 labels every
degradation permanently, nothing is silently wrong on the way there.

BACKEND LADDER (owner decision #974.1), strongest first — **srt → Landlock → none**:

* **srt** — ``@anthropic-ai/sandbox-runtime`` (Apache-2.0, npm; CLI ``srt``, latest
  ``v0.0.66``). Library mode = macOS Seatbelt (:data:`MECHANISM_SRT_SEATBELT`), Linux
  bwrap+proxy (:data:`MECHANISM_SRT_BWRAP`), Windows ACL/WFP (:data:`MECHANISM_SRT_WINDOWS`
  — srt IS the Windows path; no native restricted-token impl is built). Needs
  **node >= 20.11** and, on Linux, **socat**; each missing precondition is its own reason.
* **native Landlock** (:data:`MECHANISM_LANDLOCK`) — Linux-only fs-fence, the answer to
  bwrap broken by the Ubuntu 24.04+ AppArmor userns restriction. Deferred to B2.
* **none** (:data:`MECHANISM_NONE`) — the honest floor: no OS fence + a typed reason;
  :mod:`clio_agent.tools.file_policy` survives as the ADVISORY twin at the tool boundary.

srt VERSION PROBE (owner note #974): ``srt --version`` LIES (a stale ``1.0.0`` banner), so
:func:`_srt_package_version` reads the npm package ``package.json`` version, NEVER the CLI
banner — a probe that trusted ``--version`` would be a defect (pinned by a unit test).

WRAP COMPOSITION (owner decision #974.5): ``pdeathsig``
(:func:`clio_agent.tools.mcp_config.pdeathsig_wrapped_command`) folds INTO this pipeline as
the **outermost** composer step, so :func:`wrap_confined` is the single argv-prefix owner
(no second prefix site). This slice adds no fence prefix, so the composed argv is
byte-identical to today wherever ``pdeathsig`` was (and was not) applied.

DENIAL SEMANTICS (B2/B3 forward note): an active fence refuses an out-of-territory write at
the OS as ``EROFS`` (Linux bwrap read-only bind), ``EACCES`` (ACL) or
``WinError 5 / ERROR_ACCESS_DENIED`` (Windows) — never a bare ``PermissionError`` with no
mechanism. This slice observes none of it (the floor lets the write happen); #966's ``gap``
node carries ``sandbox: none/<reason>`` as the honest record.
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

# srt package identity (owner note #974) — pinned, never guessed.             #
#: The npm package that provides the srt runtime.
SRT_PACKAGE_NAME = "@anthropic-ai/sandbox-runtime"
#: The CLI binary the package installs (resolved on PATH unless overridden by config).
SRT_BINARY_NAME = "srt"
#: The latest srt release verified against this ladder (#974 live probe). Detection is
#: version-tolerant; this is the reference the doctor row cites, not a hard floor.
SRT_LATEST_KNOWN_VERSION = "0.0.66"
#: srt requires node >= 20.11 (owner note #974). A ``(major, minor)`` floor.
SRT_MIN_NODE_VERSION = (20, 11)

# Mechanism labels (typed, so the doctor / trace / provenance never guess).   #
MECHANISM_SRT_SEATBELT = "srt_seatbelt"  # srt on macOS
MECHANISM_SRT_BWRAP = "srt_bwrap"  # srt on Linux (bubblewrap + proxy)
MECHANISM_SRT_WINDOWS = "srt_windows"  # srt on Windows (ACL/WFP)
MECHANISM_LANDLOCK = "landlock"  # native Linux Landlock fs-fence (B2)
MECHANISM_NONE = "none"  # the honest floor: no OS fence

#: All mechanism labels this ladder can ever report (the doctor validates against it).
KNOWN_MECHANISMS: frozenset[str] = frozenset(
    {
        MECHANISM_SRT_SEATBELT,
        MECHANISM_SRT_BWRAP,
        MECHANISM_SRT_WINDOWS,
        MECHANISM_LANDLOCK,
        MECHANISM_NONE,
    }
)

# Typed ladder reasons (no silent fallback — every rung explains itself).      #
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

# Network-enforcement labels (owner decision #974.3/#974.7 — honest per tier).           #
#: On an srt tier the OS fence FORCES children through the clio proxy → real enforcement.
NET_ENFORCEMENT_PROXY = "proxy"
#: On the Landlock/floor tier egress is proxy-ENV cooperation only (raw sockets bypass) —
#: the record says so, per-edge (never claim enforcement off the srt tier).
NET_ENFORCEMENT_ENV_COOPERATIVE = "env-cooperative"

# Profiles live in the sandbox_roots sibling (the shared-boundary owner); re-exported
# here so the seams keep reaching them as ``sandbox.PROFILE_FLEET`` etc.
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

# Confinement classification of a census child by its coarse kind (#975).      #
# Makes the EXCLUDED seams visible policy in the process census, not an         #
# invisible omission: the CTE daemon, provider CLI links and serve.py are       #
# deliberately never wrapped (owner decision #974.5).                          #
Confinement = Literal["wrapped", "excluded"]
CONFINEMENT_WRAPPED: Confinement = "wrapped"
CONFINEMENT_EXCLUDED: Confinement = "excluded"

#: Child kinds (:func:`clio_agent.runtime.process_tree._classify_child`) that descend
#: from a wrapped seam (the MCP fleet + python MCP servers).
_WRAPPED_KINDS: frozenset[str] = frozenset({"mcp_stdio", "mcp_launcher", "python_child"})
#: Child kinds deliberately EXCLUDED from confinement: the shared clio-core daemon
#: (breakaway is load-bearing), and the provider LLM CLI links (claude/codex need the
#: network + their own auth/cache territory).
_EXCLUDED_KINDS: frozenset[str] = frozenset({"clio_core_daemon", "sdk_cli", "codex_cli"})


def confinement_for_kind(kind: str) -> Confinement:
    """Classify a census child ``kind`` as ``wrapped`` or ``excluded`` (#974.5).

    ``wrapped`` — spawned through a :func:`wrap_confined` seam (MCP fleet / python MCP
    servers). ``excluded`` — the CTE daemon, provider CLI links (and anything unknown),
    which are verifiably never wrapped so the census makes the exclusion visible policy.
    """
    if kind in _WRAPPED_KINDS:
        return CONFINEMENT_WRAPPED
    return CONFINEMENT_EXCLUDED


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of resolving the confinement backend (a typed, loggable reason).

    Copies the :class:`clio_agent.runtime.process_tree.ChildReaperResult` shape verbatim
    in spirit so the doctor / provenance render both the same way.

    Attributes:
        mechanism: Which fence governs child writes — one of :data:`KNOWN_MECHANISMS`.
            :data:`MECHANISM_NONE` on the floor (this slice always).
        active: Whether an OS-level write fence is actually enforcing (``True`` only for
            a live srt/Landlock backend). Always ``False`` this slice — detection never
            activates.
        reason: A short machine-stable reason token (e.g. :data:`REASON_SRT_NOT_INSTALLED`,
            :data:`REASON_SRT_DETECTED_DEFERRED`, :data:`REASON_WINDOWS_UNPROVISIONED`).
        details: Structured evidence (platform, srt detection, write_roots, net_policy...).
    """

    mechanism: str
    active: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SrtDetection:
    """What the srt probe found (detection only — never activates anything).

    ``version`` is read from the npm ``package.json`` (NEVER the lying ``srt --version``
    banner). ``node_ok`` is ``node_version`` vs :data:`SRT_MIN_NODE_VERSION`; ``reason`` is
    the typed ladder rung the verdict implies (:data:`REASON_SRT_NOT_INSTALLED`,
    :data:`REASON_SRT_NODE_TOO_OLD`, :data:`REASON_SRT_SOCAT_MISSING`, ...).
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

    ``command``/``args`` are the (possibly wrapped) argv — byte-identical to the input this
    slice unless ``pdeathsig`` applied (then the ``setpriv`` launcher, exactly as today).
    ``env_overlay`` are env keys the fence adds on top of the caller's env (empty on the
    floor); ``popen_kwargs`` are extra ``subprocess`` kwargs (empty on the floor).
    ``result`` is the per-spawn :class:`SandboxResult` (mechanism + reason + the per-call
    profile / write_roots / net_policy).
    """

    command: str
    args: list[str]
    env_overlay: dict[str, str]
    popen_kwargs: dict[str, Any]
    result: SandboxResult


# Config knobs (config → env → default; the env reference is generated).        #
def _sandbox_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether confinement resolution is enabled (config ``sandbox.enabled``).

    Detection-only this slice, so ``false`` simply stamps :data:`REASON_DISABLED` on the
    floor result — it changes no spawn behavior yet. B2+ gate activation on it.
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


def _srt_path_override(env: Optional[Mapping[str, str]] = None) -> str:
    """An explicit ``srt`` binary path (config ``sandbox.srt_path``), else ``""``.

    For hosts where ``srt`` is installed off PATH; ``""`` means "resolve on PATH".
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    if env is not None:
        return (env.get("CLIO_SANDBOX_SRT_PATH", "") or "").strip()
    return conf.resolve(
        "sandbox.srt_path", env="CLIO_SANDBOX_SRT_PATH", default="", cast=conf.as_str
    ).strip()


# srt detection (detection ONLY — never activates a fence this slice).          #
def _srt_package_version(binary_path: str) -> str:
    """Read the srt package version from its ``package.json`` — NEVER ``srt --version``.

    ``srt --version`` prints a stale ``1.0.0`` banner (owner note #974), so trusting it
    would be a defect. The installed binary is (usually) a shim/symlink under a node
    ``bin`` dir pointing into ``.../node_modules/@anthropic-ai/sandbox-runtime/``; walk
    the real path's ancestors for that package's ``package.json`` and read its ``version``.
    Returns ``""`` when no matching ``package.json`` is found (an honest empty, logged).
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

    Never spawns srt and never activates a fence. Every dependency is injectable so the
    ladder is unit-testable without a real srt/node install. The returned
    :attr:`SrtDetection.reason` is the typed ladder reason the missing/present fence
    implies; :func:`_resolve_backend` maps it onto the final (always ``none``) result.
    """
    override = _srt_path_override(env)
    binary = override if override else (which(SRT_BINARY_NAME) or "")
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
        # node is on PATH but its version could not be read (probe failure, logged by
        # the reader) — an unreadable version is NOT the same claim as "too old".
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

    Non-Linux is not-applicable (Seatbelt/Windows use no bwrap). On Linux the two known
    breakers are a missing ``bwrap`` binary (:data:`REASON_BWRAP_UNAVAILABLE`) and the Ubuntu
    24.04+ AppArmor knob ``kernel.apparmor_restrict_unprivileged_userns == 1``
    (:data:`REASON_BWRAP_USERNS_RESTRICTED`) — either drops the ladder to the Landlock rung.
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

    Viable iff every precondition is met (``srt_detected_activation_deferred``) AND the
    installed version is at/above the schema-validated floor. Otherwise the typed skip
    reason is the version gate (:data:`REASON_SRT_VERSION_UNSUPPORTED`) or the detection
    verdict itself.
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
    bwrap: Optional[tuple[bool, str]] = None,
    landlock: Any = None,
    start_proxy: Any = None,
) -> SandboxResult:
    """Resolve + ACTIVATE the confinement backend down the typed ladder (#976, B2).

    Ladder (strongest first): **srt** (macOS Seatbelt / Linux bwrap+proxy) → **Landlock**
    (Linux fs-fence, the bwrap-broken rung) → **none** (floor). Every rung change carries a
    typed reason (no silent fallback). All probes are injectable so the (platform, srt,
    bwrap, landlock, enabled) matrix is unit-pinnable without a real host; ``start_proxy`` is
    invoked only when an srt tier is chosen (its network must route through the chokepoint),
    and a typed :class:`~clio_agent.runtime.net_chokepoint.ChokepointStartError` drops the srt
    rung to Landlock/floor rather than starting children that silently lose all network.

    Windows always floors this slice (activation is B3's ``clio sandbox setup``).
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

    # Windows: srt Windows activation is B3; the floor reason is the provisioning gate.
    if platform.startswith("win"):
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

    Returns ``None`` (so the caller drops to the next rung with a typed
    :data:`REASON_CHOKEPOINT_START_FAILED`) when the network chokepoint cannot start: an srt
    child with no proxy would silently lose all network, which the campaign forbids.
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


class SandboxCompositionError(RuntimeError):
    """A fence prefix could not be composed at spawn time — typed, loud (no silent hole)."""


def _compose_fence_prefix(
    state: SandboxResult,
    profile: Profile,
    command: str,
    args: list[str],
    write_roots: Sequence[Path] | Sequence[str],
) -> tuple[str, list[str]]:
    """Compose the active fence's argv prefix around ``(command, args)`` (B2).

    srt: synthesize + schema-validate + write the per-profile settings (write territory =
    ``write_roots``, ``httpProxyPort`` = the chokepoint), then prepend
    ``srt -s <settings> --``. Landlock: prepend the ``landlock_exec`` shim over the roots.
    Any failure raises :class:`SandboxCompositionError` so the spawn fails loud rather than
    running unconfined.
    """
    mechanism = state.mechanism
    roots = list(write_roots)
    try:
        if mechanism in (MECHANISM_SRT_BWRAP, MECHANISM_SRT_SEATBELT):
            from clio_agent.runtime import sandbox_srt  # noqa: PLC0415

            proxy_port = state.details.get("proxy_port")
            config = sandbox_srt.synthesize_srt_config(roots, http_proxy_port=proxy_port)
            settings = sandbox_srt.settings_path_for(profile, config=config)
            sandbox_srt.write_settings_file(config, settings)  # validates (typed on reject)
            binary = str(state.details.get("srt_binary") or SRT_BINARY_NAME)
            prefix = sandbox_srt.srt_prefix(binary, settings)
            return prefix[0], [*prefix[1:], command, *args]
        if mechanism == MECHANISM_LANDLOCK:
            from clio_agent.runtime import sandbox_landlock  # noqa: PLC0415

            prefix = sandbox_landlock.landlock_shim_prefix(roots)
            return prefix[0], [*prefix[1:], command, *args]
    except Exception as exc:  # noqa: BLE001 — re-raised typed; a fence hole must never be silent
        raise SandboxCompositionError(
            f"fence composition failed mechanism={mechanism} reason={type(exc).__name__}: {exc}"
        ) from exc
    raise SandboxCompositionError(f"unknown active mechanism for composition: {mechanism}")


# The single spawn-composition point.                                          #
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

    THE single argv-composition owner (owner decision #974.5). On the floor the returned argv
    is byte-identical to the input (only ``pdeathsig`` where requested); on an ACTIVE srt /
    Landlock backend (B2) the fence prefix composes INNER and ``pdeathsig`` stays OUTERMOST,
    so the launch order is ``pdeathsig( fence( real-argv ) )``.

    ``command``/``args`` MUST be the FINAL resolved argv (wrap AFTER spawn-diet, so the
    fence wraps the real argv, not a launcher chain the diet deletes). ``write_roots`` is the
    child's writable territory (:func:`effective_write_roots`); ``net_policy`` rides the
    record (B4 enforces). Pass ``pdeathsig=True`` ONLY where it was applied before (preserve
    exact behavior). ``state`` defaults to the cached :func:`current_state`, else a typed
    :data:`REASON_NOT_INSTALLED` floor result. A fence that cannot compose at spawn time
    RAISES (typed) rather than silently spawning unconfined (no silent fence hole).
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

    # Active backend (B2): compose the fence prefix INNER (pdeathsig stays outermost below).
    if resolved_state.active and resolved_state.mechanism != MECHANISM_NONE:
        cmd, arg_list = _compose_fence_prefix(resolved_state, profile, cmd, arg_list, write_roots)

    # pdeathsig OUTERMOST (owner decision #974.5): fold the pre-existing argv-prefix helper
    # in as the last composer step so there is exactly one prefix owner. Passthrough on
    # non-Linux / where setpriv is absent, exactly as the helper guards.
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
        },
    )
    return ConfinedSpawn(
        command=cmd,
        args=arg_list,
        env_overlay=env_overlay,
        popen_kwargs=popen_kwargs,
        result=per_call,
    )


# Module state accessor (same pattern as process_tree.child_reaper_status).     #
# The resolved backend for THIS process, cached at install. ``None`` until installed
# (a standalone doctor CLI that never installed reports no standing state).
_STATE: SandboxResult | None = None


def install_sandbox(*, env: Optional[Mapping[str, str]] = None) -> SandboxResult:
    """Resolve + cache the confinement backend for this process (call at server boot).

    Mirrors :func:`clio_agent.runtime.process_tree.install_child_reaper`. Recomputes the
    ladder each call (cheap — a few ``which`` probes + one ``node --version``) and caches
    the result for :func:`current_state`. This slice always resolves to
    :data:`MECHANISM_NONE` with a typed reason; missing/broken host tooling never raises
    (a locked-down host still boots — the doctor row makes the missing fence visible). A
    *malformed config value* (``sandbox.enabled`` / ``CLIO_SANDBOX_ENABLED``) DOES raise,
    exactly like every other config knob — a config typo fails boot loud, per the
    boot-time environment-conformance rule, and is not a host condition to degrade over.
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


# Doctor probe: the ``sandbox`` row lives in the sandbox_doctor sibling (keeps this owner
# module under the ratchet); re-exported so callers keep reaching ``sandbox.probe_sandbox``.
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
