"""codex permission-profile concern: table synthesis, clio-side validation, TOML layer rendering.

Sibling of :mod:`clio_agent.runtime.sandbox_codex` — it holds the cohesive "codex permission
profile" logic (the ``[permissions.<name>]`` TABLE synthesis + clio's closed-key validation + the
real-TOML layer rendering + the content-addressed layer naming) so the codex backend module stays
under its file-size ratchet. Nothing here spawns codex; it produces + validates the profile table
and renders the ``-p`` layer body the backend materializes.

WHY clio-side validation (owner note #974 spike): a drifted/typo'd synthesized profile — a stray
top-level key, a bogus filesystem mode — would otherwise be handed to codex and quietly fence
nothing correct. clio therefore validates the profile it synthesized against
:func:`validate_codex_profile` (a typed ``codex_profile_rejected``) BEFORE composing overrides.

The names here are re-exported from :mod:`clio_agent.runtime.sandbox_codex` so the backend module's
public surface is unchanged — callers keep importing them from ``sandbox_codex``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Optional, Sequence

#: Typed reason surfaced when a synthesized profile fails clio's pinned key set (no silent fallback).
REASON_CODEX_PROFILE_REJECTED = "codex_profile_rejected"

#: DISTINCTIVE prefix for clio's layer files inside the shared ``~/.codex``. Both the ``-p``
#: layer name AND the ``<name>.config.toml`` file stem carry it so pruning only ever reaps
#: clio's OWN layers — never the user's ``config.toml`` or any unrelated file.
CODEX_LAYER_PREFIX = "clio-sb"
#: Glob matching only clio's layer files (used by the bounded prune).
CODEX_LAYER_GLOB = f"{CODEX_LAYER_PREFIX}-*.config.toml"
#: Keep at most this many clio layer files in the shared home (most-recently-written win).
CODEX_LAYER_KEEP = 16


class CodexProfileError(ValueError):
    """A synthesized codex profile failed clio's pinned key set — typed, never silent.

    Carries :attr:`reason` (``codex_profile_rejected``) so the ladder can degrade with a typed
    rung reason instead of handing codex a profile it never validated.
    """

    def __init__(self, message: str, *, reason: str = REASON_CODEX_PROFILE_REJECTED) -> None:
        super().__init__(message)
        self.reason = reason


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
        # ``isinstance`` guard FIRST so a drifted unhashable mode (list/dict) raises the typed
        # CodexProfileError, never a bare TypeError from the ``in`` membership test.
        if not isinstance(mode, str) or mode not in _ALLOWED_FS_MODES:
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
    # ``isinstance`` guard FIRST so a drifted unhashable mode (list/dict) raises the typed
    # CodexProfileError, never a bare TypeError from the ``in`` membership test.
    if not isinstance(mode, str) or mode not in _ALLOWED_NET_MODES:
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


__all__ = [
    "CODEX_LAYER_GLOB",
    "CODEX_LAYER_KEEP",
    "CODEX_LAYER_PREFIX",
    "REASON_CODEX_PROFILE_REJECTED",
    "CodexProfileError",
    "codex_layer_name",
    "synthesize_codex_profile",
    "validate_codex_profile",
]
