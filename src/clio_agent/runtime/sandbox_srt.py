"""srt backend: config synthesis, clio-side schema validation, argv composition (#976/B2).

Sibling of the :mod:`clio_agent.runtime.sandbox` ladder owner — it holds the srt-specific
logic so the ladder module stays under its file-size ratchet. srt
(``@anthropic-ai/sandbox-runtime``) is the strongest rung: macOS Seatbelt + Linux
bwrap+proxy. This module never spawns srt; it produces the settings JSON + the argv prefix
the ladder composes, and validates the synthesized config against clio's OWN pinned schema.

WHY clio-side validation (owner note #974 spike): srt's zod schema is ``strip`` mode — it
SILENTLY TOLERATES unknown keys (a bogus key validates). So a drifted/typo'd synthesized
config would pass srt and quietly fence nothing correct. clio therefore validates the doc it
synthesized against :func:`validate_srt_config` (a typed ``srt_config_rejected``) BEFORE
handing it to srt, and pins the srt version it validated the schema against
(``srt_version_unsupported`` below the floor) so a churny alpha bump is caught, not trusted.

CONFIG SHAPE (owner note #974 spike, live-probed against v0.0.66 ``SandboxRuntimeConfigSchema``):
``network.allowedDomains`` REJECTS ``"*"`` and there is no network-disable flag, so the
"allow all" story is ``network.httpProxyPort`` → an external allow-all CONNECT proxy
(clio's :mod:`clio_agent.runtime.net_chokepoint`, which "must handle domain filtering").
Required keys are ``network.{allowedDomains,deniedDomains}`` and
``filesystem.{denyRead,allowWrite,denyWrite}`` (empty arrays are valid). ``tlsTerminate`` is
OFF by omission (CONNECT-only, domain-level — no MITM/CA breakage).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

#: The srt version whose config schema clio validated against (owner note #974 spike).
#: Detection is tolerant ABOVE this; a package BELOW it is ``srt_version_unsupported`` —
#: clio will not trust a config shape it never validated.
SRT_MIN_SUPPORTED_VERSION = (0, 0, 66)

#: The per-profile settings filename written under the clio cache (one file per profile,
#: overwritten each spawn with that call's write territory — cheap, deterministic).
SRT_SETTINGS_DIRNAME = "srt-settings"

#: Typed reasons this module raises onto the ladder (no silent fallback).
REASON_SRT_VERSION_UNSUPPORTED = "srt_version_unsupported"
REASON_SRT_CONFIG_REJECTED = "srt_config_rejected"


class SrtConfigError(ValueError):
    """A synthesized srt config failed clio's pinned schema — typed, never silent.

    Carries :attr:`reason` (``srt_config_rejected``) so the ladder can degrade with a typed
    rung reason instead of handing srt a config it never validated.
    """

    def __init__(self, message: str, *, reason: str = REASON_SRT_CONFIG_REJECTED) -> None:
        super().__init__(message)
        self.reason = reason


def parse_version(version: str) -> tuple[int, int, int]:
    """Parse an ``x.y.z`` npm version to a ``(major, minor, patch)`` tuple (``(0,0,0)`` on junk)."""
    cleaned = (version or "").strip().lstrip("vV")
    # Drop any pre-release / build suffix (``0.0.66-beta.1`` → ``0.0.66``).
    core = cleaned.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        return (0, 0, 0)
    return (major, minor, patch)


def is_srt_version_supported(version: str) -> bool:
    """Whether ``version`` is at least :data:`SRT_MIN_SUPPORTED_VERSION` (the validated floor).

    An empty/unreadable version is NOT supported — clio cannot vouch for a config shape it
    never validated (the doctor row cites ``srt_version_unsupported``).
    """
    if not (version or "").strip():
        return False
    return parse_version(version) >= SRT_MIN_SUPPORTED_VERSION


def synthesize_srt_config(
    write_roots: Sequence[Path] | Sequence[str],
    *,
    http_proxy_port: Optional[int] = None,
) -> dict[str, Any]:
    """Synthesize the srt settings doc for a spawn (owner note #974 spike).

    Filesystem: ``allowWrite`` is the effective write territory (the ONE shared boundary);
    ``denyRead``/``denyWrite`` are empty (read-anywhere fs write-fence). Network: ALLOW +
    RECORD is expressed as ``httpProxyPort`` pointing at clio's CONNECT chokepoint (which
    handles domain filtering) with empty ``allowedDomains``/``deniedDomains`` — ``"*"`` is
    schema-rejected, so the proxy, not an allowlist, is the network story. ``tlsTerminate``
    is OFF by omission (CONNECT-only, domain-level). The doc is validated by
    :func:`validate_srt_config` before use.
    """
    roots = [str(Path(r)) for r in write_roots]
    network: dict[str, Any] = {"allowedDomains": [], "deniedDomains": []}
    if http_proxy_port is not None:
        network["httpProxyPort"] = int(http_proxy_port)
    return {
        "network": network,
        "filesystem": {"denyRead": [], "allowWrite": roots, "denyWrite": []},
    }


#: The top-level keys clio's synthesizer is allowed to emit. srt's schema strips unknown
#: keys silently, so clio validates its OWN doc against this closed set — a stray key is a
#: synthesizer drift bug (``srt_config_rejected``), never a silent no-op.
_ALLOWED_TOP_KEYS = frozenset({"network", "filesystem"})
_ALLOWED_NETWORK_KEYS = frozenset({"allowedDomains", "deniedDomains", "httpProxyPort"})
_ALLOWED_FS_KEYS = frozenset({"denyRead", "allowWrite", "denyWrite"})


def _require_str_list(value: Any, where: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise SrtConfigError(f"srt config {where} must be a list[str], got {type(value).__name__}")


def validate_srt_config(config: Any) -> None:
    """Validate a synthesized srt config against clio's OWN pinned schema (typed).

    NOT a re-implementation of srt's full zod schema — a closed-world check that the doc
    clio synthesized is exactly the shape clio intends, because srt's ``strip`` mode would
    otherwise swallow a drift/typo. Raises :class:`SrtConfigError` (``srt_config_rejected``)
    on any deviation: an unexpected top-level/section key, a missing required key, a wrong
    type, or a schema-forbidden ``"*"`` in ``allowedDomains``.
    """
    if not isinstance(config, dict):
        raise SrtConfigError("srt config must be a JSON object")
    extra = set(config) - _ALLOWED_TOP_KEYS
    if extra:
        raise SrtConfigError(f"srt config has unexpected top-level keys: {sorted(extra)}")
    if "network" not in config or "filesystem" not in config:
        raise SrtConfigError("srt config requires 'network' and 'filesystem' sections")

    network = config["network"]
    if not isinstance(network, dict):
        raise SrtConfigError("srt config 'network' must be an object")
    net_extra = set(network) - _ALLOWED_NETWORK_KEYS
    if net_extra:
        raise SrtConfigError(f"srt config network has unexpected keys: {sorted(net_extra)}")
    for key in ("allowedDomains", "deniedDomains"):
        if key not in network:
            raise SrtConfigError(f"srt config network.{key} is required")
        _require_str_list(network[key], f"network.{key}")
    if "*" in network["allowedDomains"]:
        # srt's schema rejects '*' in allowedDomains ("overly broad patterns not allowed").
        raise SrtConfigError("srt config network.allowedDomains must not contain '*'")
    if "httpProxyPort" in network:
        port = network["httpProxyPort"]
        if not isinstance(port, int) or isinstance(port, bool) or not (0 < port < 65536):
            raise SrtConfigError("srt config network.httpProxyPort must be a TCP port int")

    filesystem = config["filesystem"]
    if not isinstance(filesystem, dict):
        raise SrtConfigError("srt config 'filesystem' must be an object")
    fs_extra = set(filesystem) - _ALLOWED_FS_KEYS
    if fs_extra:
        raise SrtConfigError(f"srt config filesystem has unexpected keys: {sorted(fs_extra)}")
    for key in ("denyRead", "allowWrite", "denyWrite"):
        if key not in filesystem:
            raise SrtConfigError(f"srt config filesystem.{key} is required")
        _require_str_list(filesystem[key], f"filesystem.{key}")


def _config_json(config: dict[str, Any]) -> str:
    """The canonical (sorted) JSON serialization used for both hashing and writing."""
    return json.dumps(config, indent=2, sort_keys=True)


def write_settings_file(config: dict[str, Any], path: Path) -> Path:
    """Validate then ATOMICALLY write ``config`` to ``path`` as JSON. Returns ``path``.

    Validation runs FIRST (:func:`validate_srt_config`) so a rejected config never reaches
    disk / srt — the typed :class:`SrtConfigError` propagates. The write is atomic (temp +
    ``os.replace``) so a concurrent spawn can never read a half-written settings file.
    """
    validate_srt_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(_config_json(config), encoding="utf-8")
    os.replace(tmp, path)
    return path


def srt_prefix(binary: str, settings_path: Path | str) -> list[str]:
    """The srt argv PREFIX: ``[binary, "-s", <settings>, "--"]`` (owner note #974 spike).

    The ``--`` stops srt's own option parsing so the wrapped ``command`` + its args pass
    through EXACTLY (verified live: ``srt -s <cfg> -- <cmd> <args...>`` preserves argv,
    unlike ``-c`` shell-string mode which re-parses). The ladder composes this INSIDE
    ``pdeathsig`` (which stays outermost, owner decision #974.5).
    """
    return [str(binary), "-s", str(settings_path), "--"]


def settings_path_for(
    profile: str, *, config: Optional[dict[str, Any]] = None, cache_dir: Optional[Path] = None
) -> Path:
    """The srt settings file path for ``profile`` under the clio cache.

    CONTENT-ADDRESSED when ``config`` is given: the filename carries an 8-char digest of the
    (territory-specific) config, so two concurrent spawns with DIFFERENT write territory get
    DIFFERENT files instead of clobbering a shared per-profile file (a real race the fleet
    hits — long-lived servers in distinct workspaces). Identical territory reuses one file.
    """
    if cache_dir is None:
        from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

        cache_dir = paths.user_cache_dir()
    stem = profile
    if config is not None:
        digest = hashlib.sha256(_config_json(config).encode("utf-8")).hexdigest()[:8]
        stem = f"{profile}-{digest}"
    return cache_dir / SRT_SETTINGS_DIRNAME / f"{stem}.json"


__all__ = [
    "REASON_SRT_CONFIG_REJECTED",
    "REASON_SRT_VERSION_UNSUPPORTED",
    "SRT_MIN_SUPPORTED_VERSION",
    "SRT_SETTINGS_DIRNAME",
    "SrtConfigError",
    "is_srt_version_supported",
    "parse_version",
    "settings_path_for",
    "srt_prefix",
    "synthesize_srt_config",
    "validate_srt_config",
    "write_settings_file",
]
