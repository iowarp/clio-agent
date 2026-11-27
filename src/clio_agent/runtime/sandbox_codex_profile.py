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
from pathlib import Path
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


def _default_read_roots(*, platform: str) -> list[str]:
    """Use Codex's Windows root token for read-anywhere, ``/`` off Windows.

    Elevated Windows execution requires effective ``:root`` read access. Native
    drive anchors fail the client's permission-path round trip before any child
    can start. Explicit protected read/deny entries still override this root.
    """
    return [":root"] if platform.startswith("win") else ["/"]


def synthesize_codex_profile(
    write_roots: Sequence[Path] | Sequence[str],
    *,
    read_roots: Optional[Sequence[Path] | Sequence[str]] = None,
    profile_name: str = "clio",
    platform: str = sys.platform,
    mxc: bool = False,
) -> dict[str, Any]:
    """Synthesize the ``[permissions.<name>]`` table for a spawn (read-anywhere, write-fence).

    ``filesystem`` maps every read root to ``"read"`` and every write root to ``"write"`` (a
    write grant WINS over an overlapping read grant — the write territory is the ONE shared
    boundary). ``read_roots`` defaults to the filesystem root (:func:`_default_read_roots`)
    so reads are open. Paths are normalized with ``str(Path(r))``. The returned table is
    validated by :func:`validate_codex_profile` before it is returned (typed on drift).
    """
    writes = [str(Path(r)) for r in write_roots]
    reads = (
        [str(Path(r)) for r in read_roots]
        if read_roots is not None
        else _default_read_roots(platform=platform)
    )
    filesystem: dict[str, str] = {}
    for root in reads:
        filesystem[root] = "read"
    for root in writes:  # write wins over an overlapping read grant
        filesystem[root] = "write"
    from clio_agent.runtime.storage_access import storage_access_roots  # noqa: PLC0415

    read_only, denied = storage_access_roots()
    # A nested write grant must not override a protected parent. Denials also
    # remove nested read grants before the final profile is handed to Codex.
    filesystem = {
        path: access
        for path, access in filesystem.items()
        if not any(Path(path).resolve().is_relative_to(root) for root in denied)
        and not (
            access == "write"
            and any(Path(path).resolve().is_relative_to(root) for root in read_only)
        )
    }
    for protected_root in read_only:
        filesystem[str(protected_root)] = "read"
    for protected_root in denied:
        filesystem[str(protected_root)] = "deny"
    if platform.startswith("linux") and not mxc:
        for socket_root in linux_control_socket_masks(writes):
            filesystem.setdefault(socket_root, "deny")
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
    if mxc:
        # Full permits HTTP methods, not domains. CLIO's upstream chokepoint
        # owns domain decisions; the managed proxy must actually be enabled.
        profile["network"].update(allow_local_binding=True, domains={"*": "allow"})
    elif platform.startswith("linux"):
        profile["network"].update(_LINUX_MANAGED_NET)
    validate_codex_profile(profile)
    return profile


#: Linux runs the child in Codex's managed-proxy network namespace: its only route out is
#: Codex's proxy, which chains every public CONNECT to CLIO's per-child chokepoint. Codex dials
#: loopback and private targets directly instead of through that upstream, so local binding
#: stays off (refused, never unrecorded) and SOCKS is off (Codex does not chain it to CLIO).
#: Unix sockets are local IPC, not a route out of the namespace: ssh ``ControlMaster``,
#: munge (Slurm's credentials) and a server's own helpers need them. Codex can only allow them
#: all on Linux, so binds stay inside the writable territory (the rest of the tree is
#: read-only) and the host's control sockets are masked (:func:`linux_control_socket_masks`).
_LINUX_MANAGED_NET: dict[str, Any] = {
    "enable_socks5": False,
    "enable_socks5_udp": False,
    "allow_local_binding": False,
    "dangerously_allow_all_unix_sockets": True,
    "domains": {"*": "allow"},
}

#: Host sockets a fenced child must not reach once unix sockets are allowed: each controls
#: something outside the fence (containers, the session and system buses, systemd, the
#: display, terminal multiplexers) or resolves names on the host network (an unrecorded way
#: out). ``{runtime}`` is the user's runtime folder and ``{uid}`` the user's id.
_LINUX_CONTROL_SOCKETS: tuple[str, ...] = (
    "{runtime}",
    "/run/user/{uid}",
    "/run/dbus",
    "/var/run/dbus",
    "/run/systemd",
    "/run/nscd",
    "/var/run/nscd",
    "/run/docker.sock",
    "/var/run/docker.sock",
    "/run/containerd",
    "/run/podman",
    "/run/libvirt",
    "/run/snapd.socket",
    "/run/snapd-snap.socket",
    "/var/snap/lxd/common/lxd",
    "/run/screen",
    "/tmp/.X11-unix",
    "/tmp/.ICE-unix",
    "/tmp/tmux-{uid}",
    "{home}/.docker/run",
    "{home}/.screen",
)


def linux_control_socket_masks(write_roots: Sequence[str] = ()) -> list[str]:
    """The host control-socket paths to deny on Linux (see :data:`_LINUX_CONTROL_SOCKETS`).

    Only paths that exist are masked, by their resolved location: bubblewrap cannot mask a
    missing path (it fails to create the mount point under a read-only ``/run``). A mask that
    would hide a writable root is left out, so the territory always stays usable; paths that
    need a user id are skipped where there is none.
    """
    import os  # noqa: PLC0415 - only on the Linux path

    getuid = getattr(os, "getuid", None)
    uid = str(getuid()) if getuid is not None else ""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip() or (f"/run/user/{uid}" if uid else "")
    home = str(Path.home()).replace("\\", "/")
    masks: list[str] = []
    for template in _LINUX_CONTROL_SOCKETS:
        if ("{uid}" in template and not uid) or ("{runtime}" in template and not runtime):
            continue
        path = _present(template.format(uid=uid, runtime=runtime, home=home))
        if not path or path in masks or any(_contains(path, root) for root in write_roots):
            continue
        masks.append(path)
    return masks


def _present(path: str) -> str:
    """``path`` resolved through symlinks when it exists on this host, else ``""``."""
    import os  # noqa: PLC0415 - only on the Linux path

    if not os.path.lexists(path):
        return ""
    return os.path.realpath(path).replace("\\", "/")


def _contains(parent: str, child: str) -> bool:
    """Whether ``child`` is ``parent`` or lies under it (POSIX paths, compared as written)."""
    parent = parent.rstrip("/") or "/"
    child = child.replace("\\", "/")
    return child == parent or child.startswith(parent + "/")


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
_MXC_NET_KEYS = _ALLOWED_NET_KEYS | {"allow_local_binding", "domains"}
_LINUX_NET_KEYS = _ALLOWED_NET_KEYS | set(_LINUX_MANAGED_NET)


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
    the two flags bool, ``mode`` in :data:`_ALLOWED_NET_MODES`. MXC and Linux add their managed
    proxy keys, each pinned to the one value CLIO's chokepoint relies on.
    """
    if "network" not in profile:
        raise CodexProfileError("codex profile requires a 'network' table")
    network = profile["network"]
    if not isinstance(network, dict):
        raise CodexProfileError("codex profile 'network' must be a table")
    keys = set(network)
    if keys not in (_ALLOWED_NET_KEYS, _MXC_NET_KEYS, _LINUX_NET_KEYS):
        raise CodexProfileError(
            f"codex profile network keys must be exactly {sorted(_ALLOWED_NET_KEYS)}, "
            f"got {sorted(keys)}"
        )
    for flag in ("enabled", "allow_upstream_proxy"):  # int is not bool → no silent coercion
        if not isinstance(network[flag], bool):
            raise CodexProfileError(f"codex profile network[{flag!r}] must be a bool")
    if keys == _MXC_NET_KEYS and (
        network["allow_local_binding"] is not True or network["domains"] != {"*": "allow"}
    ):
        raise CodexProfileError("MXC requires local binding and CLIO's upstream domain policy")
    if keys == _LINUX_NET_KEYS and any(
        type(network[key]) is not type(value) or network[key] != value
        for key, value in _LINUX_MANAGED_NET.items()
    ):
        raise CodexProfileError(
            "Linux requires Codex's proxy without SOCKS or local binding, chained to CLIO, "
            "with unix sockets for local IPC"
        )
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


def codex_layer_name(
    profile_name: str, profile: dict[str, Any], *, windows_sandbox: str = ""
) -> str:
    """The content-addressed ``clio-sb-<sha8>`` name — BOTH the ``-p`` name and the file stem.

    Content-addressed by an 8-char sha256 of the (profile_name, profile) so two spawns with the
    SAME territory reuse ONE layer (idempotent) while DIFFERENT territory gets a DIFFERENT layer
    (no clobber). The distinctive :data:`CODEX_LAYER_PREFIX` makes pruning safe — only clio's own
    layers ever match :data:`CODEX_LAYER_GLOB`, never the user's ``config.toml``.
    """
    content: dict[str, Any] = {"p": profile_name, "t": profile}
    if windows_sandbox:
        content["windows_sandbox"] = windows_sandbox
    canonical = json.dumps(content, sort_keys=True)
    sha8 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"{CODEX_LAYER_PREFIX}-{sha8}"


def _render_layer_toml(
    profile_name: str, profile: dict[str, Any], *, elevated: bool, windows_sandbox: str = ""
) -> str:
    """Render the layer's real-TOML body (a FILE, no shell involved — plain escaping).

    Emits ``[windows]\\nsandbox = "elevated"`` when ``elevated`` (win32-gated), then the
    ``[permissions.<name>.filesystem]`` grants, then the ``[permissions.<name>.network]`` egress
    table (Recipe A). Values are read from the profile dict (never hardcoded) so a future mode
    change flows through. Backslashes are doubled by :func:`_toml_str` (genuine TOML).
    """
    lines: list[str] = []
    if windows_sandbox not in ("", "mxc", "elevated"):
        raise CodexProfileError(f"Unsupported Windows sandbox: {windows_sandbox}")
    network = profile["network"]
    if windows_sandbox == "mxc" or "enable_socks5" in network:
        lines.extend(["[features]", "network_proxy = true", ""])
    if elevated or windows_sandbox:
        lines.append("[windows]")
        lines.append(f'sandbox = "{windows_sandbox or "elevated"}"')
        lines.append("")
    lines.append(f"[permissions.{profile_name}.filesystem]")
    for path, mode in profile["filesystem"].items():
        lines.append(f"{_toml_str(path)} = {_toml_str(mode)}")
    lines.append("")
    # Recipe A egress table — emit profile values (never hardcoded); ``mitm`` absent by design.
    lines.append(f"[permissions.{profile_name}.network]")
    lines.append(f"enabled = {_toml_bool(network['enabled'])}")
    lines.append(f"mode = {_toml_str(network['mode'])}")
    lines.append(f"allow_upstream_proxy = {_toml_bool(network['allow_upstream_proxy'])}")
    for flag in (
        "enable_socks5",
        "enable_socks5_udp",
        "allow_local_binding",
        "dangerously_allow_all_unix_sockets",
    ):
        if flag in network:
            lines.append(f"{flag} = {_toml_bool(network[flag])}")
    if "domains" in network:
        lines.append('domains = { "*" = "allow" }')
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
