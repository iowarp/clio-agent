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

import hashlib
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

#: The codex version whose config/profile shape clio validated against (validated live on this
#: box: ``codex-cli 0.145.0``). Detection is tolerant ABOVE this; a package BELOW it is
#: ``codex_version_unsupported`` — clio will not trust a config shape it never validated.
CODEX_MIN_SUPPORTED_VERSION = (0, 145, 0)

#: The plain binary name (``codex``); win32 detection prefers the launchable ``.cmd``/``.exe``.
CODEX_BINARY_NAME = "codex"

#: DISTINCTIVE prefix for clio's layer files inside the shared ``~/.codex``. Both the ``-p``
#: layer name AND the ``<name>.config.toml`` file stem carry it so pruning only ever reaps
#: clio's OWN layers — never the user's ``config.toml`` or any unrelated file.
CODEX_LAYER_PREFIX = "clio-sb"
#: Glob matching only clio's layer files (used by the bounded prune).
CODEX_LAYER_GLOB = f"{CODEX_LAYER_PREFIX}-*.config.toml"
#: Keep at most this many clio layer files in the shared home (most-recently-written win).
CODEX_LAYER_KEEP = 16

#: Typed reasons this module surfaces onto the ladder / doctor (no silent fallback).
REASON_CODEX_NOT_INSTALLED = "codex_not_installed"
REASON_CODEX_VERSION_UNSUPPORTED = "codex_version_unsupported"
REASON_CODEX_PROFILE_REJECTED = "codex_profile_rejected"
REASON_CODEX_DETECTED = "codex_detected"

#: How long to wait on the ``codex --version`` probe before giving up (an honest empty version).
_VERSION_PROBE_TIMEOUT_S = 5.0

#: Extracts the trailing ``X.Y.Z`` out of a ``codex-cli X.Y.Z`` banner.
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


class CodexProfileError(ValueError):
    """A synthesized codex profile failed clio's pinned key set — typed, never silent.

    Carries :attr:`reason` (``codex_profile_rejected``) so the ladder can degrade with a typed
    rung reason instead of handing codex a profile it never validated.
    """

    def __init__(self, message: str, *, reason: str = REASON_CODEX_PROFILE_REJECTED) -> None:
        super().__init__(message)
        self.reason = reason


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


def _default_read_roots(write_roots: Sequence[str], *, platform: str) -> list[str]:
    """The read-anywhere roots for a spawn: drive anchors on win32, ``/`` off-win32.

    On win32 codex grants ``"read"`` at the DRIVE level (``C:\\``, ``D:\\``) so reads are open
    everywhere the write roots' drives live; off-win32 the single filesystem root ``/`` is the
    read-anywhere grant. Deduplicated, order-preserving.
    """
    if platform.startswith("win"):
        anchors: list[str] = []
        seen: set[str] = set()
        for root in write_roots:
            anchor = PureWindowsPath(root).anchor
            if anchor and anchor not in seen:
                seen.add(anchor)
                anchors.append(anchor)
        return anchors or ["C:\\"]
    return ["/"]


def synthesize_codex_profile(
    write_roots: Sequence[Path] | Sequence[str],
    *,
    read_roots: Optional[Sequence[Path] | Sequence[str]] = None,
    profile_name: str = "clio",
    platform: str = sys.platform,
) -> dict[str, Any]:
    """Synthesize the ``[permissions.<name>]`` table for a spawn (read-anywhere, write-fence).

    ``filesystem`` maps every read root to ``"read"`` and every write root to ``"write"`` (a
    write grant WINS over an overlapping read grant — the write territory is the ONE shared
    boundary). ``read_roots`` defaults to the filesystem/drive roots (:func:`_default_read_roots`)
    so reads are open. Paths are normalized with ``str(Path(r))``. The returned table is
    validated by :func:`validate_codex_profile` before it is returned (typed on drift).
    """
    writes = [str(Path(r)) for r in write_roots]
    reads = (
        [str(Path(r)) for r in read_roots]
        if read_roots is not None
        else _default_read_roots(writes, platform=platform)
    )
    filesystem: dict[str, str] = {}
    for root in reads:
        filesystem[root] = "read"
    for root in writes:  # write wins over an overlapping read grant
        filesystem[root] = "write"
    profile: dict[str, Any] = {
        "description": (
            f"clio sandbox profile {profile_name!r}: read-anywhere, "
            f"write-fenced to {len(writes)} root(s)"
        ),
        "filesystem": filesystem,
        # Recipe A egress recording: codex's managed proxy chains to clio's per-child upstream;
        # ``mode="full"`` = observe-all (reads allowed); ``mitm`` OMITTED (table-not-bool in v0.145).
        "network": {"enabled": True, "mode": "full", "allow_upstream_proxy": True},
    }
    validate_codex_profile(profile)
    return profile


#: The top-level keys clio's synthesizer is allowed to emit. clio validates its OWN table
#: against this closed set — a stray key is a synthesizer drift bug (``codex_profile_rejected``),
#: never a silent no-op that fences nothing.
_ALLOWED_PROFILE_KEYS = frozenset({"description", "filesystem", "network"})
#: The only filesystem grant modes codex honors; anything else is drift.
_ALLOWED_FS_MODES = frozenset({"read", "write", "deny"})
#: Egress observation modes clio synthesizes (``full`` = observe-all; ``limited`` reserved). Drift else.
_ALLOWED_NET_MODES = frozenset({"full", "limited"})
#: The exact closed key set clio's ``network`` table carries (like the filesystem grants).
_ALLOWED_NET_KEYS = frozenset({"enabled", "mode", "allow_upstream_proxy"})


def validate_codex_profile(profile: Any) -> None:
    """Validate a synthesized codex profile against clio's OWN closed key set (typed).

    A closed-world check that the table clio synthesized is exactly the shape clio intends,
    defensive against silent config drift (a stray key, a bogus mode) that codex might tolerate
    and quietly mis-fence. Raises :class:`CodexProfileError` (``codex_profile_rejected``) on any
    deviation: an unexpected top-level key, a missing ``filesystem`` or ``network`` table, a
    non-string description, an empty/non-string filesystem key, a filesystem mode outside
    ``{read, write, deny}``, or a drifted ``network`` table (key set / bool / mode).
    """
    if not isinstance(profile, dict):
        raise CodexProfileError("codex profile must be a table (dict)")
    extra = set(profile) - _ALLOWED_PROFILE_KEYS
    if extra:
        raise CodexProfileError(f"codex profile has unexpected top-level keys: {sorted(extra)}")
    if "filesystem" not in profile:
        raise CodexProfileError("codex profile requires a 'filesystem' table")
    description = profile.get("description")
    if description is not None and not isinstance(description, str):
        raise CodexProfileError("codex profile 'description' must be a string")
    filesystem = profile["filesystem"]
    if not isinstance(filesystem, dict):
        raise CodexProfileError("codex profile 'filesystem' must be a table")
    for key, mode in filesystem.items():
        if not isinstance(key, str) or not key.strip():
            raise CodexProfileError(
                f"codex profile filesystem key must be a non-empty string: {key!r}"
            )
        if mode not in _ALLOWED_FS_MODES:
            raise CodexProfileError(
                f"codex profile filesystem[{key!r}] must be one of "
                f"{sorted(_ALLOWED_FS_MODES)}, got {mode!r}"
            )
    _validate_codex_network(profile)


def _validate_codex_network(profile: dict[str, Any]) -> None:
    """Validate the REQUIRED ``network`` egress table (Recipe A) against clio's closed key set.

    clio always synthesizes ``network`` (egress recording is not optional), so a MISSING table is
    drift like a missing ``filesystem`` — a typed ``codex_profile_rejected``, never a silent no-op
    leaving egress unrecorded. Must carry EXACTLY ``{enabled, mode, allow_upstream_proxy}``:
    the two flags bool, ``mode`` in :data:`_ALLOWED_NET_MODES`.
    """
    if "network" not in profile:
        raise CodexProfileError("codex profile requires a 'network' table")
    network = profile["network"]
    if not isinstance(network, dict):
        raise CodexProfileError("codex profile 'network' must be a table")
    keys = set(network)
    if keys != _ALLOWED_NET_KEYS:
        raise CodexProfileError(
            f"codex profile network keys must be exactly {sorted(_ALLOWED_NET_KEYS)}, "
            f"got {sorted(keys)}"
        )
    for flag in ("enabled", "allow_upstream_proxy"):  # int is not bool → no silent coercion
        if not isinstance(network[flag], bool):
            raise CodexProfileError(f"codex profile network[{flag!r}] must be a bool")
    mode = network["mode"]
    if mode not in _ALLOWED_NET_MODES:
        raise CodexProfileError(
            f"codex profile network['mode'] must be one of {sorted(_ALLOWED_NET_MODES)}, "
            f"got {mode!r}"
        )


def _toml_str(value: str) -> str:
    """Render ``value`` as a TOML basic string (backslash + quote escaped).

    Windows paths carry backslashes; an UNescaped backslash is a TOML escape lead-in, so every
    ``\\`` and ``"`` must be doubled/escaped or codex's parser mis-reads the path.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    """Render a bool as a TOML literal (lowercase ``true``/``false``)."""
    return "true" if value else "false"


def codex_layer_name(profile_name: str, profile: dict[str, Any]) -> str:
    """The content-addressed ``clio-sb-<sha8>`` name — BOTH the ``-p`` name and the file stem.

    Content-addressed by an 8-char sha256 of the (profile_name, profile) so two spawns with the
    SAME territory reuse ONE layer (idempotent) while DIFFERENT territory gets a DIFFERENT layer
    (no clobber). The distinctive :data:`CODEX_LAYER_PREFIX` makes pruning safe — only clio's own
    layers ever match :data:`CODEX_LAYER_GLOB`, never the user's ``config.toml``.
    """
    canonical = json.dumps({"p": profile_name, "t": profile}, sort_keys=True)
    sha8 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"{CODEX_LAYER_PREFIX}-{sha8}"


def _render_layer_toml(profile_name: str, profile: dict[str, Any], *, elevated: bool) -> str:
    """Render the layer's real-TOML body (a FILE, no shell involved — plain escaping).

    Emits ``[windows]\\nsandbox = "elevated"`` when ``elevated`` (win32-gated), then the
    ``[permissions.<name>.filesystem]`` grants, then the ``[permissions.<name>.network]`` egress
    table (Recipe A). Values are read from the profile dict (never hardcoded) so a future mode
    change flows through. Backslashes are doubled by :func:`_toml_str` (genuine TOML).
    """
    lines: list[str] = []
    if elevated:
        lines.append("[windows]")
        lines.append('sandbox = "elevated"')
        lines.append("")
    lines.append(f"[permissions.{profile_name}.filesystem]")
    for path, mode in profile["filesystem"].items():
        lines.append(f"{_toml_str(path)} = {_toml_str(mode)}")
    lines.append("")
    # Recipe A egress table — emit profile values (never hardcoded); ``mitm`` absent by design.
    network = profile["network"]
    lines.append(f"[permissions.{profile_name}.network]")
    lines.append(f"enabled = {_toml_bool(network['enabled'])}")
    lines.append(f"mode = {_toml_str(network['mode'])}")
    lines.append(f"allow_upstream_proxy = {_toml_bool(network['allow_upstream_proxy'])}")
    lines.append("")
    return "\n".join(lines)


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
