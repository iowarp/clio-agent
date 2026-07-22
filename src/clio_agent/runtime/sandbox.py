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

from clio_agent.runtime.status import IntegrationState, IntegrationStatus

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# srt package identity (owner note #974) — pinned, never guessed.             #
# --------------------------------------------------------------------------- #
#: The npm package that provides the srt runtime.
SRT_PACKAGE_NAME = "@anthropic-ai/sandbox-runtime"
#: The CLI binary the package installs (resolved on PATH unless overridden by config).
SRT_BINARY_NAME = "srt"
#: The latest srt release verified against this ladder (#974 live probe). Detection is
#: version-tolerant; this is the reference the doctor row cites, not a hard floor.
SRT_LATEST_KNOWN_VERSION = "0.0.66"
#: srt requires node >= 20.11 (owner note #974). A ``(major, minor)`` floor.
SRT_MIN_NODE_VERSION = (20, 11)

# --------------------------------------------------------------------------- #
# Mechanism labels (typed, so the doctor / trace / provenance never guess).   #
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# Typed ladder reasons (no silent fallback — every rung explains itself).      #
# --------------------------------------------------------------------------- #
REASON_SRT_NOT_INSTALLED = "srt_not_installed"
REASON_SRT_NODE_MISSING = "srt_node_missing"
REASON_SRT_NODE_TOO_OLD = "srt_node_too_old"
REASON_SRT_SOCAT_MISSING = "srt_socat_missing"
REASON_SRT_DETECTED_DEFERRED = "srt_detected_activation_deferred"
REASON_BWRAP_USERNS_RESTRICTED = "bwrap_userns_restricted"  # B2 rung (Ubuntu 24.04+)
REASON_KERNEL_TOO_OLD = "kernel_too_old"  # B2 rung (no Landlock)
REASON_LANDLOCK_DEFERRED = "landlock_activation_deferred"  # B2 rung
REASON_WINDOWS_UNPROVISIONED = "windows_unprovisioned"  # B3 rung (needs `clio sandbox setup`)
REASON_DISABLED = "disabled_by_config"
REASON_NOT_INSTALLED = "sandbox_not_installed"  # wrap_confined ran before install_sandbox()

# --------------------------------------------------------------------------- #
# Profiles + network policy (typed literals).                                  #
# --------------------------------------------------------------------------- #
#: ``fleet`` — the per-workspace, long-lived MCP servers (transport_for /
#: transport_from_spec). ``shell`` — the per-invocation shell subprocess.
Profile = Literal["fleet", "shell"]
PROFILE_FLEET: Profile = "fleet"
PROFILE_SHELL: Profile = "shell"

#: Network default is ALLOW + RECORD (owner decision #974.3); deny-by-default is an
#: opt-in per-workspace mode wired in B4. Recorded here, enforced later.
NetPolicy = Literal["allow_record", "deny"]
NET_ALLOW_RECORD: NetPolicy = "allow_record"
NET_DENY: NetPolicy = "deny"

# --------------------------------------------------------------------------- #
# Confinement classification of a census child by its coarse kind (#975).      #
# Makes the EXCLUDED seams visible policy in the process census, not an         #
# invisible omission: the CTE daemon, provider CLI links and serve.py are       #
# deliberately never wrapped (owner decision #974.5).                          #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Config knobs (config → env → default; the env reference is generated).        #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# srt detection (detection ONLY — never activates a fence this slice).          #
# --------------------------------------------------------------------------- #
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
    except (OSError, subprocess.SubprocessError):
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


def _resolve_backend(
    *,
    env: Optional[Mapping[str, str]] = None,
    platform: str = sys.platform,
    detection: Optional[SrtDetection] = None,
) -> SandboxResult:
    """Resolve the confinement backend (ALWAYS :data:`MECHANISM_NONE` this slice).

    Detection informs the typed reason; it NEVER activates a fence (floor-first, #975).
    The reason is the most-informative ladder rung: a disabled knob, the srt detection
    verdict, or — on Windows with srt absent — the provisioning gate.
    """
    det = detection if detection is not None else detect_srt(env=env, platform=platform)
    target = _target_mechanism_for_platform(platform)
    base_details: dict[str, Any] = {
        "platform": platform,
        "target_mechanism": target,
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
        "landlock": "deferred_to_b2" if platform.startswith("linux") else "not_applicable",
    }

    if not _sandbox_enabled(env):
        return SandboxResult(
            mechanism=MECHANISM_NONE,
            active=False,
            reason=REASON_DISABLED,
            details=base_details,
        )

    if det.reason == REASON_SRT_DETECTED_DEFERRED:
        # srt + preconditions present, but this slice does not activate it.
        return SandboxResult(
            mechanism=MECHANISM_NONE,
            active=False,
            reason=REASON_SRT_DETECTED_DEFERRED,
            details=base_details,
        )

    # srt not viable. On Windows the honest reason is the provisioning gate (owner
    # decision #974.2); elsewhere the srt detection reason stands (Landlock is the B2
    # Linux rung, not asserted live here).
    if det.reason == REASON_SRT_NOT_INSTALLED and platform.startswith("win"):
        reason = REASON_WINDOWS_UNPROVISIONED
    else:
        reason = det.reason
    return SandboxResult(
        mechanism=MECHANISM_NONE,
        active=False,
        reason=reason,
        details=base_details,
    )


# --------------------------------------------------------------------------- #
# Effective write roots — the ONE shared boundary (owner decision #974.6).      #
# --------------------------------------------------------------------------- #
def _platform_tool_cache_dirs() -> list[Path]:
    """Platform tool-cache dirs the MCP fleet must be able to write (false-positive guard).

    A fence that forgot these would break ``uv``/``npm``/``pip`` launchers mid-spawn. Kept
    bounded + honest: the clio-owned caches plus the common per-user tool caches (present or
    not — they are writable territory, not a precondition).
    """
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    dirs: list[Path] = [paths.user_cache_dir(), paths.user_config_dir(), paths.user_data_dir()]
    home = Path.home()
    # Common per-user tool caches (uv, npm, pip). Present or not, they are writable
    # territory for a launcher, so the fence must include them.
    dirs.extend(
        [
            home / ".cache" / "uv",
            home / ".cache" / "pip",
            home / ".npm",
        ]
    )
    return dirs


def effective_write_roots(
    profile: Profile,
    *,
    policy: Any = None,
    env: Optional[Mapping[str, str]] = None,
    workspace_root: Optional[str] = None,
) -> tuple[Path, ...]:
    """The writable territory for ``profile`` — the ONE boundary both twins consume (#974.6).

    Anti-drift by construction: the base is the ADVISORY
    :attr:`clio_agent.tools.file_policy.FileAccessPolicy.allowed_roots` (the same source
    the tool-boundary check reads), so the fence territory can never be narrower than what
    file_policy already permits. The fence then ADDS the caches a spawned launcher needs
    (tempdir + clio + tool caches) so confinement never false-positives on a legitimate
    ``uv``/``npm`` cache write.

    ``profile`` is :data:`PROFILE_FLEET` (long-lived MCP servers; adds the mcp-uv-cache +
    platform tool caches) or :data:`PROFILE_SHELL` (per-invocation). ``policy`` supplies the
    advisory base (defaults to :meth:`FileAccessPolicy.from_mapping` when ``env`` given, else
    ``from_env``); ``workspace_root`` includes an explicit root (shell computes it per
    invocation). Returns a deduped, order-stable tuple (advisory roots first).
    """
    import tempfile  # noqa: PLC0415 - cheap, only on this path

    from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415 - avoid cycle

    if policy is None:
        policy = (
            FileAccessPolicy.from_mapping(env) if env is not None else FileAccessPolicy.from_env()
        )
    roots: list[Path] = list(policy.allowed_roots)

    def _add(path: Path) -> None:
        resolved = path.expanduser()
        if resolved not in roots:
            roots.append(resolved)

    if workspace_root:
        _add(Path(workspace_root))
    _add(Path(tempfile.gettempdir()))

    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    _add(paths.user_cache_dir())
    _add(paths.user_config_dir())

    if profile == PROFILE_FLEET:
        from clio_agent.tools.mcp_config import _mcp_uv_cache_dir  # noqa: PLC0415 - avoid cycle

        _add(_mcp_uv_cache_dir())
        for cache in _platform_tool_cache_dirs():
            _add(cache)

    return tuple(roots)


# --------------------------------------------------------------------------- #
# The single spawn-composition point.                                          #
# --------------------------------------------------------------------------- #
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
    """Compose the confined spawn plan for a resolved ``(command, args)`` (#975).

    THE single argv-composition owner (owner decision #974.5). This slice adds NO fence
    prefix and NO env overlay — the returned argv is byte-identical to the input, except
    for the ``pdeathsig`` outermost prefix wherever the caller requests it (exactly as
    today). Order of composition is fence-first (inner), ``pdeathsig`` last (outer), so
    when B2 adds a fence prefix ``pdeathsig`` stays outermost.

    ``command``/``args`` MUST be the FINAL resolved argv (wrap AFTER spawn-diet, so the
    fence wraps the real argv, not a launcher chain the diet deletes). ``write_roots`` +
    ``net_policy`` are recorded now, enforced in B2/B4. Pass ``pdeathsig=True`` ONLY where
    it was applied before this slice (preserve exact behavior). ``state`` defaults to the
    cached :func:`current_state`, else a typed :data:`REASON_NOT_INSTALLED` floor result.
    Returns a :class:`ConfinedSpawn` ready for the transport / ``subprocess`` call.
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

    # (fence prefix / env overlay would compose here on an active backend — none this slice)

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


# --------------------------------------------------------------------------- #
# Module state accessor (same pattern as process_tree.child_reaper_status).     #
# --------------------------------------------------------------------------- #
# The resolved backend for THIS process, cached at install. ``None`` until installed
# (a standalone doctor CLI that never installed reports no standing state).
_STATE: SandboxResult | None = None


def install_sandbox(*, env: Optional[Mapping[str, str]] = None) -> SandboxResult:
    """Resolve + cache the confinement backend for this process (call at server boot).

    Mirrors :func:`clio_agent.runtime.process_tree.install_child_reaper`. Recomputes the
    ladder each call (cheap — a few ``which`` probes + one ``node --version``) and caches
    the result for :func:`current_state`. This slice always resolves to
    :data:`MECHANISM_NONE` with a typed reason; it never raises (a locked-down host still
    boots — the doctor row makes the missing fence visible).
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


def emit_boot_state_event(app: Any, state: SandboxResult | None) -> None:
    """Emit the boot ``sandbox.state`` conformance event (#975), best-effort.

    Mirrors the ``artifact.cas.tmp_swept`` boot-event pattern: a trace-only semantic event
    stamping the resolved OS write-confinement mechanism + typed reason so the conformance
    floor is queryable per boot. Uses the boot sid (``""``). Never blocks agent readiness —
    a failed emit is logged with a typed reason (no silent path). Called from the gact
    lifespan once ARC (the highway source) is live; the LOGIC lives here (owner module), the
    app only calls it (no accretion into the god file).
    """
    if state is None:
        return
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            "",
            "sandbox.state",
            status="completed",
            summary=(
                f"OS write-confinement resolved: mechanism={state.mechanism}, "
                f"active={state.active}, reason={state.reason}."
            ),
            actor={"mechanism": "harness"},
            payload={
                "mechanism": state.mechanism,
                "active": state.active,
                "reason": state.reason,
                "details": state.details,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a boot conformance emit must never block readiness
        logger.warning(
            "sandbox state event emit skipped reason=sandbox_state_emit_failed error=%r", exc
        )


# --------------------------------------------------------------------------- #
# Doctor probe: the ``sandbox`` row (DEGRADED never ERROR — the floor is legal). #
# --------------------------------------------------------------------------- #
def probe_sandbox(*, state: SandboxResult | None = None) -> IntegrationStatus:
    """Report the confinement backend as a doctor row (#975).

    READY when an OS fence is active; DEGRADED (surfaced, never silent — but NEVER an error
    state) on the honest floor, because a missing fence is a *legal* configuration (the
    advisory file_policy still applies; HPC/no-npm hosts are floor-only). Cites the
    mechanism, the typed reason and the srt detection details.
    """
    resolved = state if state is not None else current_state()
    if resolved is None:
        return IntegrationStatus(
            name="sandbox",
            state=IntegrationState.SKIPPED,
            summary="Confinement backend not resolved in this process (no server boot).",
            config_source="runtime:sandbox",
            next_action="Start the gact server to resolve the confinement backend.",
            details={"reason": REASON_NOT_INSTALLED},
            required=False,
        )

    details: dict[str, Any] = {
        "reason": resolved.reason,
        "mechanism": resolved.mechanism,
        "active": resolved.active,
        **resolved.details,
    }
    if resolved.active and resolved.mechanism in KNOWN_MECHANISMS - {MECHANISM_NONE}:
        return IntegrationStatus(
            name="sandbox",
            state=IntegrationState.READY,
            summary=(f"OS write-confinement active (mechanism={resolved.mechanism})."),
            config_source="runtime:sandbox",
            next_action="No action required.",
            capabilities=["write-fence"],
            details=details,
            required=False,
        )

    srt = resolved.details.get("srt", {}) if isinstance(resolved.details, dict) else {}
    if resolved.reason == REASON_SRT_DETECTED_DEFERRED:
        next_action = (
            f"{SRT_PACKAGE_NAME} v{srt.get('version') or '?'} is installed; the OS fence "
            "activates in a later slice (B2/B3). No action required."
        )
    elif resolved.reason == REASON_WINDOWS_UNPROVISIONED:
        next_action = "Run `clio sandbox setup` (B3) to provision the Windows write fence."
    elif resolved.reason == REASON_DISABLED:
        next_action = "Set sandbox.enabled=true (CLIO_SANDBOX_ENABLED) to resolve a fence."
    else:
        next_action = (
            f"Install {SRT_PACKAGE_NAME} (needs node>={SRT_MIN_NODE_VERSION[0]}."
            f"{SRT_MIN_NODE_VERSION[1]}"
            f"{', socat on Linux' if sys.platform.startswith('linux') else ''}) for the OS "
            "write fence; the advisory file_policy still applies meanwhile."
        )
    return IntegrationStatus(
        name="sandbox",
        state=IntegrationState.DEGRADED,
        summary=(
            f"No OS write-confinement (mechanism=none, reason={resolved.reason}); the "
            "advisory file_policy applies at the tool boundary. Out-of-root writes are "
            "recorded on the provenance floor, not yet prevented."
        ),
        config_source="runtime:sandbox",
        next_action=next_action,
        fallback="advisory-file-policy-only",
        details=details,
        required=False,
    )


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
    "REASON_SRT_SOCAT_MISSING",
    "REASON_SRT_DETECTED_DEFERRED",
    "REASON_BWRAP_USERNS_RESTRICTED",
    "REASON_KERNEL_TOO_OLD",
    "REASON_LANDLOCK_DEFERRED",
    "REASON_WINDOWS_UNPROVISIONED",
    "REASON_DISABLED",
    "REASON_NOT_INSTALLED",
    "PROFILE_FLEET",
    "PROFILE_SHELL",
    "NET_ALLOW_RECORD",
    "NET_DENY",
    "CONFINEMENT_WRAPPED",
    "CONFINEMENT_EXCLUDED",
    "SandboxResult",
    "SrtDetection",
    "ConfinedSpawn",
    "confinement_for_kind",
    "detect_srt",
    "effective_write_roots",
    "wrap_confined",
    "install_sandbox",
    "current_state",
    "emit_boot_state_event",
    "probe_sandbox",
]
