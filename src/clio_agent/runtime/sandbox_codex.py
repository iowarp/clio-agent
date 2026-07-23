"""codex backend: detection, profile synthesis, clio-side validation, argv composition (B-codex-1).

Sibling of :mod:`clio_agent.runtime.sandbox_srt` — it holds the Codex-specific logic so the
ladder module (:mod:`clio_agent.runtime.sandbox`) stays under its file-size ratchet. The
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
    }
    validate_codex_profile(profile)
    return profile


#: The top-level keys clio's synthesizer is allowed to emit. clio validates its OWN table
#: against this closed set — a stray key is a synthesizer drift bug (``codex_profile_rejected``),
#: never a silent no-op that fences nothing.
_ALLOWED_PROFILE_KEYS = frozenset({"description", "filesystem"})
#: The only filesystem grant modes codex honors; anything else is drift.
_ALLOWED_FS_MODES = frozenset({"read", "write", "deny"})


def validate_codex_profile(profile: Any) -> None:
    """Validate a synthesized codex profile against clio's OWN closed key set (typed).

    A closed-world check that the table clio synthesized is exactly the shape clio intends,
    defensive against silent config drift (a stray key, a bogus mode) that codex might tolerate
    and quietly mis-fence. Raises :class:`CodexProfileError` (``codex_profile_rejected``) on any
    deviation: an unexpected top-level key, a missing ``filesystem`` table, a non-string
    description, an empty/non-string filesystem key, or a mode outside ``{read, write, deny}``.
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


def _toml_str(value: str) -> str:
    """Render ``value`` as a TOML basic string (backslash + quote escaped).

    Windows paths carry backslashes; an UNescaped backslash is a TOML escape lead-in, so every
    ``\\`` and ``"`` must be doubled/escaped or codex's parser mis-reads the path.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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

    Emits ``[windows]\\nsandbox = "elevated"`` when ``elevated`` (the caller gates this on win32),
    then ``[permissions.<name>.filesystem]`` with one ``"<escaped-path>" = "<mode>"`` line per
    grant. Backslashes are doubled by :func:`_toml_str` — this is genuine TOML, so a Windows path
    reads back exactly.
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


__all__ = [
    "CODEX_BINARY_NAME",
    "CODEX_MIN_SUPPORTED_VERSION",
    "REASON_CODEX_DETECTED",
    "REASON_CODEX_NOT_INSTALLED",
    "REASON_CODEX_PROFILE_REJECTED",
    "REASON_CODEX_VERSION_UNSUPPORTED",
    "CodexDetection",
    "CodexProfileError",
    "codex_layer_name",
    "codex_prefix",
    "detect_codex",
    "is_codex_version_supported",
    "parse_version",
    "synthesize_codex_profile",
    "validate_codex_profile",
    "write_codex_layer",
]
