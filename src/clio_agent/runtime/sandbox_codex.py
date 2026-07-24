"""codex backend: detection, profile synthesis, clio-side validation, argv composition (B-codex-1).

Sibling of the :mod:`clio_agent.runtime.sandbox` ladder — it holds the Codex-specific logic so the
ladder module stays under its file-size ratchet. The
OpenAI **Codex** sandbox (``codex``, open-source ``codex-rs``, Apache-2.0) is the rung that
enforces on native Windows using the SOUND primitive — a dedicated sandbox user + ACLs (not
srt's failing ``CreateProcessWithLogonW`` secondary logon) — and Seatbelt/bubblewrap on
mac/Linux. This module never spawns codex; it produces the inline config overrides + the argv
prefix the ladder composes, and validates the synthesized profile against clio's OWN pinned key
set.

WHY clio-side validation (mirrors the srt rationale, owner note #974 spike): a drifted/typo'd
synthesized profile — a stray top-level key, a bogus filesystem mode — would otherwise be
handed to codex and quietly fence nothing correct. clio therefore validates the profile it
synthesized against :func:`validate_codex_profile` (a typed ``codex_profile_rejected``) BEFORE
composing overrides, and pins the codex version it validated the config shape against
(``codex_version_unsupported`` below the floor) so a churny bump is caught, not trusted.

CONFIG-INJECTION STRATEGY (validated live on this box — the write-fence actually enforced,
child ``codexsandboxoffline``, write-inside-workspace ALLOWED / write-outside DENIED): the
profile is written as a SEPARATE LAYER FILE inside codex's DEFAULT CODEX_HOME (``~/.codex``)
and selected with ``-p <layer>``. Two approaches were proven WRONG live and rejected:

* A minimal fresh/custom CODEX_HOME (a clio-owned dir with only the layer) — the elevated
  backend activates but the per-workspace write grant SILENTLY never applies, because a fresh
  home lacks the per-home sandbox state codex needs. The layer MUST live in the real
  ``~/.codex`` so codex has that state.
* Inline ``-c KEY=VALUE`` overrides — codex's ``-c`` parser does NOT strip TOML key-quotes, so a
  quoted filesystem path is rejected (``filesystem path "C:\\" must be absolute``).

So clio writes ``<codex_home>/<layer>.config.toml`` (a real TOML file — no shell quoting) with a
DISTINCTIVE ``clio-sb-`` content-addressed name and selects it via ``-p`` — the user's own
``config.toml`` is NEVER touched, and pruning only ever reaps clio's own ``clio-sb-*`` layers.
The proven invocation:
``codex sandbox -p <layer> --permission-profile <p> -C <ws> -- <cmd>``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# The codex permission-profile concern (table synthesis + validation + TOML layer rendering + layer
# naming) lives in the sibling :mod:`sandbox_codex_profile` module (extracted to keep this file under
# its size ratchet). Re-exported below so this module's public surface is unchanged — callers keep
# importing these names from ``sandbox_codex``.
from .sandbox_codex_profile import (
    CODEX_LAYER_GLOB,
    CODEX_LAYER_KEEP,
    CODEX_LAYER_PREFIX,
    REASON_CODEX_PROFILE_REJECTED,
    CodexProfileError,
    _render_layer_toml,
    codex_layer_name,
    synthesize_codex_profile,
    validate_codex_profile,
)

logger = logging.getLogger(__name__)

#: The codex version whose config/profile shape clio validated against (validated live on this
#: box: ``codex-cli 0.145.0``). Detection is tolerant ABOVE this; a package BELOW it is
#: ``codex_version_unsupported`` — clio will not trust a config shape it never validated.
CODEX_MIN_SUPPORTED_VERSION = (0, 145, 0)

#: The plain binary name (``codex``); win32 detection prefers the launchable ``.cmd``/``.exe``.
CODEX_BINARY_NAME = "codex"

#: Typed reasons this module surfaces onto the ladder / doctor (no silent fallback).
#: (``REASON_CODEX_PROFILE_REJECTED`` lives with the profile concern in
#: :mod:`sandbox_codex_profile` and is re-exported above.)
REASON_CODEX_NOT_INSTALLED = "codex_not_installed"
REASON_CODEX_VERSION_UNSUPPORTED = "codex_version_unsupported"
REASON_CODEX_DETECTED = "codex_detected"

#: How long to wait on the ``codex --version`` probe before giving up (an honest empty version).
_VERSION_PROBE_TIMEOUT_S = 5.0

#: Extracts the trailing ``X.Y.Z`` out of a ``codex-cli X.Y.Z`` banner.
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


class CodexSpawnError(ValueError):
    """A codex spawn could not be composed (e.g. no write territory) — typed, never silent.

    The ladder wraps this in :class:`~clio_agent.runtime.sandbox.SandboxCompositionError` so a
    fence that cannot compose fails loud rather than spawning a child with no write territory.
    """


@dataclass(frozen=True)
class CodexDetection:
    """What the codex probe found (detection only — never activates anything).

    ``version`` is parsed from ``codex --version`` (reliable, unlike srt's lying banner).
    ``reason`` is the typed ladder rung the verdict implies
    (:data:`REASON_CODEX_NOT_INSTALLED`, :data:`REASON_CODEX_VERSION_UNSUPPORTED`,
    :data:`REASON_CODEX_DETECTED`).
    """

    installed: bool
    binary_path: str
    version: str
    reason: str


def parse_version(version: str) -> tuple[int, int, int]:
    """Parse an ``x.y.z`` version to a ``(major, minor, patch)`` tuple (``(0,0,0)`` on junk)."""
    cleaned = (version or "").strip().lstrip("vV")
    # Drop any pre-release / build suffix (``0.145.0-beta.1`` → ``0.145.0``).
    core = cleaned.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        return (0, 0, 0)
    return (major, minor, patch)


def is_codex_version_supported(version: str) -> bool:
    """Whether ``version`` is at least :data:`CODEX_MIN_SUPPORTED_VERSION` (the validated floor).

    An empty/unreadable version is NOT supported — clio cannot vouch for a config shape it
    never validated (the doctor row cites ``codex_version_unsupported``).
    """
    if not (version or "").strip():
        return False
    return parse_version(version) >= CODEX_MIN_SUPPORTED_VERSION


def _parse_codex_version_banner(text: str) -> str:
    """Extract the ``X.Y.Z`` out of a ``codex-cli X.Y.Z`` banner (``""`` when absent)."""
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else ""


def _read_codex_version(binary: str = "") -> str:
    """Return the codex version via ``codex --version`` (short timeout) or ``""`` on failure.

    Best-effort + typed-empty: any spawn/parse failure returns ``""`` (an honest empty version,
    logged), which :func:`is_codex_version_supported` treats as unsupported — never a guess.
    """
    exe = binary or shutil.which(CODEX_BINARY_NAME)
    if not exe:
        return ""
    import subprocess  # noqa: PLC0415 - only needed on the detection path

    try:
        out = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("codex version probe failed reason=codex_version_unreadable error=%r", exc)
        return ""
    return _parse_codex_version_banner(out.stdout or "")


def detect_codex(
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    version_reader: Callable[[str], str] = _read_codex_version,
    platform: str = sys.platform,
) -> CodexDetection:
    """Probe for the codex runtime + its version. DETECTION ONLY.

    Never spawns a fence. Every dependency is injectable so the ladder is unit-testable without
    a real codex install. On win32 the launchable ``codex.cmd``/``codex.exe`` are preferred over
    the extensionless ``codex`` (a POSIX shim ``which`` returns first cannot be exec'd by
    CreateProcess — the #1025 srt.cmd lesson). The returned :attr:`CodexDetection.reason` is the
    typed ladder reason the missing/present/old fence implies.
    """
    names = (
        ("codex.cmd", "codex.exe", CODEX_BINARY_NAME)
        if platform.startswith("win")
        else (CODEX_BINARY_NAME,)
    )
    binary = next((p for p in (which(n) for n in names) if p), "")
    if not binary:
        return CodexDetection(
            installed=False,
            binary_path="",
            version="",
            reason=REASON_CODEX_NOT_INSTALLED,
        )
    version = version_reader(binary) or ""
    reason = (
        REASON_CODEX_DETECTED
        if is_codex_version_supported(version)
        else REASON_CODEX_VERSION_UNSUPPORTED
    )
    return CodexDetection(
        installed=True,
        binary_path=binary,
        version=version,
        reason=reason,
    )


def _prune_layers(codex_home: Path, *, keep: int = CODEX_LAYER_KEEP) -> None:
    """Bound clio's layer files in the shared home to ``keep`` most-recent (no unbounded leak).

    Reaps ONLY files matching :data:`CODEX_LAYER_GLOB` (clio's ``clio-sb-*`` prefix) — NEVER the
    user's ``config.toml`` or any unrelated file. Best-effort + guarded: a prune failure must
    never break a spawn, and a concurrent-unlink race is ignored.
    """
    try:
        files = sorted(
            (p for p in codex_home.glob(CODEX_LAYER_GLOB) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass  # a peer spawn may have pruned it already — benign


def write_codex_layer(
    profile_name: str,
    profile: dict[str, Any],
    *,
    elevated: bool,
    codex_home: Optional[Path | str] = None,
    platform: str = sys.platform,
) -> str:
    """Write the ``-p`` layer file into codex's DEFAULT home and return its layer name.

    The layer lives at ``<codex_home>/<clio-sb-sha8>.config.toml`` inside the REAL ``~/.codex``
    (resolved from ``codex_home`` arg, else ``$CODEX_HOME``, else ``~/.codex``) — a fresh/custom
    home was proven to silently drop the write grant. The ``[windows] sandbox = "elevated"``
    block is emitted ONLY when ``elevated`` on win32 (the enforcement gate; a no-op elsewhere).
    Validation runs FIRST (:func:`validate_codex_profile`, typed) so a drift never reaches disk;
    the file is UTF-8 **WITHOUT a BOM** (a BOM breaks codex's TOML parser — verified live). The
    user's own ``config.toml`` is NEVER touched; only clio's ``clio-sb-*`` layers are pruned.
    """
    validate_codex_profile(profile)
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    home.mkdir(parents=True, exist_ok=True)
    layer = codex_layer_name(profile_name, profile)
    body = _render_layer_toml(
        profile_name, profile, elevated=elevated and platform.startswith("win")
    )
    # ``encoding="utf-8"`` writes NO BOM (unlike ``utf-8-sig``); a BOM breaks codex's TOML parse.
    (home / f"{layer}.config.toml").write_text(body, encoding="utf-8")
    _prune_layers(home)
    return layer


def codex_prefix(
    binary: str,
    profile_name: str,
    workspace: Path | str,
    *,
    layer_name: str,
) -> list[str]:
    """The codex argv PREFIX selecting the ``-p`` layer (validated live, write-fence enforced).

    Shape: ``[binary, "sandbox", "-p", <layer>, "--permission-profile", <p>, "-C", <ws>, "--"]``.
    The ``-p`` layer supplies the ``permissions.<p>`` grants from :func:`write_codex_layer`; the
    trailing ``--`` stops codex's option parsing so the wrapped ``command`` + its args pass
    through EXACTLY (the child argv threads fine through ``codex.cmd`` — no exe resolution).
    """
    return [
        str(binary),
        "sandbox",
        "-p",
        layer_name,
        "--permission-profile",
        profile_name,
        "-C",
        str(workspace),
        "--",
    ]


def compose_codex_spawn(
    write_roots: Sequence[Path] | Sequence[str],
    command: str,
    args: Sequence[str],
    *,
    binary: str,
    platform: str = sys.platform,
    codex_home: Optional[Path | str] = None,
) -> tuple[str, list[str]]:
    """Compose the Codex ``sandbox`` argv wrapping ``(command, args)`` (the ladder's spawn hook).

    Synthesizes the read-anywhere / write-fence profile for ``write_roots``, materializes it as a
    ``-p`` layer file in the DEFAULT codex home (``[windows] sandbox = "elevated"`` gated on win32),
    pins the primary write root ``write_roots[0]`` as the workspace (``-C``), and returns
    ``(binary, ["sandbox", … , "--", command, *args])``.

    Args:
        write_roots: The child's writable territory; the first root is the workspace cwd.
        command: The final resolved child executable (wrapped AFTER any spawn-diet).
        args: The child's arguments, threaded through verbatim past the ``--`` separator.
        binary: The resolved codex binary (``codex`` / ``codex.cmd`` / ``codex.exe``).
        platform: Injectable platform string (drives the win32 elevated gate + read-anywhere roots).
        codex_home: Override for the codex home the ``-p`` layer is written into (tests inject a
            tmp dir); ``None`` uses ``$CODEX_HOME`` else the real ``~/.codex``.

    Returns:
        The ``(command, args)`` pair to launch — the codex binary and its sandbox argv.

    Raises:
        CodexSpawnError: There is no write territory (an empty fence would confine nothing).
        CodexProfileError: The synthesized profile failed clio's pinned validation (upstream).
    """
    roots = [str(Path(r)) for r in write_roots]
    if not roots:
        raise CodexSpawnError("codex spawn requires at least one write root (no empty fence)")
    profile = synthesize_codex_profile(roots, platform=platform)
    layer = write_codex_layer(
        "clio",
        profile,
        elevated=platform.startswith("win"),
        codex_home=codex_home,
        platform=platform,
    )
    prefix = codex_prefix(binary, "clio", roots[0], layer_name=layer)
    return prefix[0], [*prefix[1:], command, *args]


# --------------------------------------------------------------------------- #
# Windows provisioning detection + enforcement verify (B-codex-3, #1026).       #
# --------------------------------------------------------------------------- #
#
# Codex's Windows ELEVATED backend runs confined children as dedicated local users
# (``codexsandboxoffline`` / ``codexsandboxonline``) created by codex's one-time setup helper on
# the first elevated use (one UAC prompt). Provisioned ⇔ those accounts exist. A provisioned
# account is NOT proof codex can actually confine a child — so clio runs a REAL behavioural probe
# (spawn a confined codex child, attempt an out-of-root write, confirm DENIED) and records the
# verdict in a small clio-owned marker the ladder reads at boot (no live probe every boot). The
# provisioned / enforcement-verified / unverified verdict pattern is the #1026 no-false-green rule.

#: The dedicated local accounts codex's elevated Windows backend runs confined children as
#: (created by codex's one-time setup helper). ``codexsandboxoffline`` existing ⇔ provisioned.
CODEX_WINDOWS_ACCOUNT_OFFLINE = "codexsandboxoffline"
CODEX_WINDOWS_ACCOUNT_ONLINE = "codexsandboxonline"
#: The clio-owned marker recording codex's Windows provisioning + enforcement verdict (under the
#: existing config dir — a single small file, never a fifth store).
CODEX_WINDOWS_MARKER_NAME = "codex-windows-provisioned.json"

#: Typed Windows provisioning / enforcement verdicts (no silent fallback — every outcome names itself).
REASON_NOT_WINDOWS = "not_windows"
REASON_CODEX_WINDOWS_PROVISIONED = "codex_windows_provisioned"
REASON_CODEX_WINDOWS_UNPROVISIONED = "codex_windows_unprovisioned"
REASON_CODEX_ENFORCEMENT_VERIFIED = "codex_enforcement_verified"
REASON_CODEX_ENFORCEMENT_UNVERIFIED = "codex_enforcement_unverified"
REASON_CODEX_ENFORCEMENT_ESCAPED = "codex_enforcement_escaped"

#: Bounded spawn timeout for the enforcement probe — a hung codex must never stall setup.
_CODEX_PROBE_TIMEOUT_S = 60
#: Short timeout for the non-mutating ``net user`` provisioning query.
_ACCOUNT_PROBE_TIMEOUT_S = 10


def _default_codex_account_check(*, account: str = CODEX_WINDOWS_ACCOUNT_OFFLINE) -> bool:
    """Whether a codex Windows sandbox account exists (a non-mutating ``net user`` query).

    Best-effort + short timeout: any spawn/query failure is treated as "cannot confirm" (not
    provisioned), never a raised error. ``net user`` is Windows-only; the caller guards platform.
    """
    import subprocess  # noqa: PLC0415 - only on this path

    try:
        proc = subprocess.run(
            ["net", "user", account],
            capture_output=True,
            text=True,
            timeout=_ACCOUNT_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("codex account probe failed reason=codex_account_probe_failed error=%r", exc)
        return False
    return proc.returncode == 0


def codex_windows_provisioned(
    *,
    platform: str = sys.platform,
    runner: Optional[Callable[[], bool]] = None,
) -> tuple[bool, str]:
    """Whether codex's Windows sandbox accounts are provisioned (injectable, best-effort).

    Off-win32 → ``(True, "not_windows")``: codex uses Seatbelt/bubblewrap there, so there is
    nothing to provision. On win32 the ``runner`` (default: ``net user codexsandboxoffline``)
    confirms the ``codexsandboxoffline`` account exists; any error → ``False`` (not provisioned).

    Returns ``(provisioned, reason)`` where reason is :data:`REASON_CODEX_WINDOWS_PROVISIONED`
    or :data:`REASON_CODEX_WINDOWS_UNPROVISIONED` (:data:`REASON_NOT_WINDOWS` off-win32).
    """
    if not platform.startswith("win"):
        return True, REASON_NOT_WINDOWS
    check = runner if runner is not None else _default_codex_account_check
    exists = bool(check())
    reason = REASON_CODEX_WINDOWS_PROVISIONED if exists else REASON_CODEX_WINDOWS_UNPROVISIONED
    return exists, reason


def verify_codex_enforcement(
    binary: str,
    write_root: str,
    *,
    platform: str = sys.platform,
    runner: Optional[Callable[[str, str], tuple[bool, str]]] = None,
) -> tuple[bool, str]:
    """Prove codex actually ENFORCES a Windows write fence (fail-safe, never raises).

    Off-win32 → ``(False, "not_windows")`` (this fence does not apply there); else run the injectable
    ``runner`` (default :func:`_run_codex_enforcement_probe`). Any exception is an honest
    ``(False, codex_enforcement_unverified)`` — the fence is unproven, so the ladder must NOT
    claim it (#1026 no-false-green; precision-over-recall: only an observed denial yields ``True``).

    ``runner`` is injectable so the whole matrix is unit-pinnable without spawning codex; the real
    probe is win32-only and never unit-run.
    """
    if not platform.startswith("win"):
        return False, REASON_NOT_WINDOWS
    run = runner if runner is not None else _run_codex_enforcement_probe
    try:
        return run(binary, write_root)
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ enforcement unproven, never a false-green
        logger.info(
            "codex enforcement verify failed reason=%s error=%r",
            REASON_CODEX_ENFORCEMENT_UNVERIFIED,
            exc,
        )
        return False, REASON_CODEX_ENFORCEMENT_UNVERIFIED


def _run_codex_enforcement_probe(
    binary: str, write_root: str
) -> tuple[bool, str]:  # pragma: no cover - win32 live gate only (never unit-run)
    """The real behavioural probe (win32; never unit-run — tests inject ``runner``).

    Fences a fresh temp ``allow`` dir and composes a confined codex child (via
    :func:`codex_prefix` over a :func:`write_codex_layer` elevated layer) that writes to a path
    OUTSIDE the fence. Enforcement ⇒ the write is denied (file absent AND the child spawned):
    :data:`REASON_CODEX_ENFORCEMENT_VERIFIED`. If the file appears the fence let an out-of-root
    write through: :data:`REASON_CODEX_ENFORCEMENT_ESCAPED`. If codex could not even spawn the
    confined child (``createprocesswithlogon`` in the output) the fence is
    :data:`REASON_CODEX_ENFORCEMENT_UNVERIFIED`. The outside redirect target is NOT quoted (the
    temp path is space-free; quoting mangles the child redirect through codex's spawn — a
    live-proven gotcha). ``write_root`` is the caller's declared territory (threaded for parity
    with :func:`verify_codex_enforcement`); the probe self-provisions its temp allow dir.
    """
    import subprocess  # noqa: PLC0415 - only on this path
    import tempfile  # noqa: PLC0415

    with (
        tempfile.TemporaryDirectory(prefix="clio-codex-allow-") as allow,
        tempfile.TemporaryDirectory(prefix="clio-codex-out-") as outside,
    ):
        outside_target = Path(outside) / "denied.txt"
        profile = synthesize_codex_profile([allow])
        layer = write_codex_layer("clio-verify", profile, elevated=True)
        argv = [
            *codex_prefix(binary, "clio-verify", allow, layer_name=layer),
            "cmd",
            "/c",
            f"type nul > {outside_target}",  # NO quotes — space-free temp path; quoting mangles it
        ]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_CODEX_PROBE_TIMEOUT_S, check=False
        )
        if outside_target.exists():
            # The confined child wrote OUTSIDE its territory — the fence did not hold.
            return False, REASON_CODEX_ENFORCEMENT_ESCAPED
        blob = f"{proc.stdout}\n{proc.stderr}".lower()
        if "createprocesswithlogon" in blob:
            # codex never spawned the confined child (elevated logon failed) — nothing enforced.
            return False, REASON_CODEX_ENFORCEMENT_UNVERIFIED
        # Child spawned and the out-of-root write did not land ⇒ the fence is genuinely in force.
        return True, REASON_CODEX_ENFORCEMENT_VERIFIED


def _codex_marker_path() -> Path:
    """The clio-owned codex provisioning marker path (under the existing config dir, no new store)."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return paths.user_config_dir() / "sandbox" / CODEX_WINDOWS_MARKER_NAME


def _read_codex_marker() -> Optional[dict[str, Any]]:
    """Read the codex provisioning marker, or ``None`` when absent/unreadable (honest empty)."""
    import json  # noqa: PLC0415 - only on this path

    path = _codex_marker_path()
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.info("codex windows marker unreadable reason=codex_marker_unreadable error=%r", exc)
        return None


def write_codex_provision_marker(
    version: str,
    *,
    enforcement_verified: Optional[bool] = None,
    enforcement_reason: str = "",
) -> Path:
    """Persist the codex Windows provisioning + enforcement verdict marker (written by setup).

    Records the codex version + timestamp + accounts so a later boot reads an HONEST cached state
    WITHOUT re-spawning codex. ``enforcement_verified`` is the result of the real behavioural check
    (:func:`verify_codex_enforcement`) — ``True`` only when codex was observed to actually deny an
    out-of-root write; anything else (``False``/``None``) leaves the fence unproven so the ladder
    floors honestly rather than reporting a false ``active`` (#1026). Returns the written path.
    """
    import json  # noqa: PLC0415 - only on this path
    from datetime import datetime, timezone  # noqa: PLC0415

    path = _codex_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "accounts": [CODEX_WINDOWS_ACCOUNT_OFFLINE, CODEX_WINDOWS_ACCOUNT_ONLINE],
        "codex_version": version,
        "provisioned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "enforcement_verified": enforcement_verified,
        "enforcement_reason": enforcement_reason,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def codex_windows_gate(
    *,
    platform: str = sys.platform,
    provisioned: Any = None,
    marker_reader: Any = None,
) -> tuple[bool, str]:
    """The ladder's cached Windows codex gate: provisioned account AND enforcement-verified marker.

    The TODO-free typed default the ladder (:func:`clio_agent.runtime.sandbox._resolve_backend`)
    reads at boot — it consults the CACHED marker, never a live probe every boot. Returns
    ``(ready, reason)``: no ``codexsandboxoffline`` account → :data:`REASON_CODEX_WINDOWS_UNPROVISIONED`;
    provisioned but the cached marker is not ``enforcement_verified`` (verify failed, or a marker
    predating the check) → :data:`REASON_CODEX_ENFORCEMENT_UNVERIFIED` (#1026, no false-green);
    else ``(True, codex_windows_provisioned)``. Sub-probes are injectable for unit tests.
    """
    check = provisioned if provisioned is not None else codex_windows_provisioned
    ok, _reason = check(platform=platform)
    if not ok:
        return False, REASON_CODEX_WINDOWS_UNPROVISIONED
    read = marker_reader if marker_reader is not None else _read_codex_marker
    marker = read()
    if marker is None or marker.get("enforcement_verified") is not True:
        return False, REASON_CODEX_ENFORCEMENT_UNVERIFIED
    return True, REASON_CODEX_WINDOWS_PROVISIONED


__all__ = [
    "CODEX_BINARY_NAME",
    "CODEX_LAYER_GLOB",
    "CODEX_LAYER_KEEP",
    "CODEX_LAYER_PREFIX",
    "CODEX_MIN_SUPPORTED_VERSION",
    "CODEX_WINDOWS_ACCOUNT_OFFLINE",
    "CODEX_WINDOWS_ACCOUNT_ONLINE",
    "CODEX_WINDOWS_MARKER_NAME",
    "REASON_CODEX_DETECTED",
    "REASON_CODEX_ENFORCEMENT_ESCAPED",
    "REASON_CODEX_ENFORCEMENT_UNVERIFIED",
    "REASON_CODEX_ENFORCEMENT_VERIFIED",
    "REASON_CODEX_NOT_INSTALLED",
    "REASON_CODEX_PROFILE_REJECTED",
    "REASON_CODEX_VERSION_UNSUPPORTED",
    "REASON_CODEX_WINDOWS_PROVISIONED",
    "REASON_CODEX_WINDOWS_UNPROVISIONED",
    "REASON_NOT_WINDOWS",
    "CodexDetection",
    "CodexProfileError",
    "CodexSpawnError",
    "codex_layer_name",
    "codex_prefix",
    "codex_windows_gate",
    "codex_windows_provisioned",
    "compose_codex_spawn",
    "detect_codex",
    "is_codex_version_supported",
    "parse_version",
    "synthesize_codex_profile",
    "validate_codex_profile",
    "verify_codex_enforcement",
    "write_codex_layer",
    "write_codex_provision_marker",
]
