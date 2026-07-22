"""Native Linux Landlock backend: kernel probe + shim argv composition (#976/B2).

Sibling of the :mod:`clio_agent.runtime.sandbox` ladder owner (keeps it under the ratchet).
Landlock is the Linux rung BELOW srt — the answer to bwrap being broken by the Ubuntu 24.04+
AppArmor ``kernel.apparmor_restrict_unprivileged_userns`` restriction: an unprivileged,
userns-free kernel LSM write-fence. It is **fs-fence only** — Landlock has no network
control at the ABIs we target, so the net policy on this rung is reported ``env-cooperative``
(the chokepoint env is cooperation, never enforcement — B4 labels every edge honestly).

Landlock restricts the CALLING thread before ``exec``, so a subprocess must apply the
ruleset from INSIDE itself: the ladder composes a tiny python launcher shim
(:mod:`clio_agent.runtime.landlock_exec`) that applies the ruleset then ``execvp``s the real
argv. The kernel is probed via ``landlock_create_ruleset(NULL, 0, VERSION)`` (returns the
supported ABI) rather than parsing ``uname`` — the syscall is the source of truth. ABI ≥ 1
(kernel ≥ 5.13) = fs write-fence; ABI ≥ 2 (kernel ≥ 5.19) additionally handles+grants
``LANDLOCK_ACCESS_FS_REFER`` so a legitimate cross-directory ``rename``/``link`` between two
allowed roots (the stage-then-``os.replace`` atomic-write pattern) is PERMITTED instead of
failing ``EXDEV`` — the shim applies REFER whenever the running ABI supports it (never on
ABI 1, where an unknown access bit is ``EINVAL``). ``refer_supported`` records that verdict.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# Landlock syscall numbers (stable across x86_64/aarch64/arm — assigned together in 5.13).
_NR_LANDLOCK_CREATE_RULESET = 444
#: ``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)`` returns the ABI.
_LANDLOCK_CREATE_RULESET_VERSION = 1

#: Typed reasons this rung reports onto the ladder (no silent fallback).
REASON_LANDLOCK_UNAVAILABLE = "landlock_unavailable"
REASON_KERNEL_TOO_OLD = "kernel_too_old"

#: The Landlock ABI at which the REFER access right (cross-dir rename/link) appears (5.19).
_ABI_REFER = 2


@dataclass(frozen=True)
class LandlockProbe:
    """What the Landlock kernel probe found (probe only — never restricts this process).

    ``available`` is ``True`` when the running kernel reports ABI ≥ 1 (an fs write-fence is
    enforceable). ``abi`` is the raw supported ABI (0 when unavailable). ``refer_supported``
    is ABI ≥ 2. ``reason`` is the typed ladder rung on failure (:data:`REASON_KERNEL_TOO_OLD`
    when the syscall exists but reports no ABI, :data:`REASON_LANDLOCK_UNAVAILABLE` when the
    syscall is absent / not Linux), else ``""``.
    """

    available: bool
    abi: int
    refer_supported: bool
    reason: str


def _create_ruleset_version() -> int:
    """Return the kernel's supported Landlock ABI (>0), 0 if disabled, or <0 on ``ENOSYS``.

    Calls ``landlock_create_ruleset(NULL, 0, VERSION)`` — the sanctioned capability probe.
    Never raises: a kernel without the syscall returns ``-1`` (``ENOSYS``), which the caller
    maps to a typed unavailable reason.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return -1
    libc.syscall.restype = ctypes.c_long
    try:
        return int(
            libc.syscall(
                ctypes.c_long(_NR_LANDLOCK_CREATE_RULESET),
                None,
                ctypes.c_size_t(0),
                ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
            )
        )
    except Exception:  # noqa: BLE001 — a probe must never raise into boot; report unavailable
        return -1


def probe_landlock(*, platform: str = sys.platform) -> LandlockProbe:
    """Probe the running kernel for Landlock support (probe only, owner decision #974.1).

    Non-Linux hosts are :data:`REASON_LANDLOCK_UNAVAILABLE` without touching libc. On Linux
    the ABI is read from the syscall: ABI ≥ 1 → available fs write-fence; ABI ≤ 0 → a typed
    reason (``kernel_too_old`` when the syscall exists but Landlock is off/absent).
    """
    if not platform.startswith("linux"):
        return LandlockProbe(False, 0, False, REASON_LANDLOCK_UNAVAILABLE)
    abi = _create_ruleset_version()
    if abi >= 1:
        return LandlockProbe(True, abi, abi >= _ABI_REFER, "")
    # abi == 0: syscall present but Landlock disabled (lockdown / not compiled in); abi < 0:
    # ENOSYS (kernel < 5.13). Both are "no enforceable fence"; distinguish for the doctor.
    reason = REASON_KERNEL_TOO_OLD if abi < 0 else REASON_LANDLOCK_UNAVAILABLE
    return LandlockProbe(False, max(abi, 0), False, reason)


def landlock_shim_prefix(
    write_roots: Sequence[Path] | Sequence[str],
    *,
    python_exe: Optional[str] = None,
) -> list[str]:
    """Compose the Landlock launcher-shim argv PREFIX (the ladder appends the real argv).

    Produces ``[python, -m, clio_agent.runtime.landlock_exec, <root>..., "--"]``; the ladder
    concatenates ``<command> <args...>`` after the ``--``. The shim
    (:mod:`clio_agent.runtime.landlock_exec`) applies the fs write-fence to the roots then
    ``execvp``s the real argv, so the fence lands on the child, not the clio server.
    """
    interpreter = python_exe or sys.executable
    prefix = [interpreter, "-m", "clio_agent.runtime.landlock_exec"]
    prefix.extend(str(Path(r)) for r in write_roots)
    prefix.append("--")
    return prefix


__all__ = [
    "REASON_KERNEL_TOO_OLD",
    "REASON_LANDLOCK_UNAVAILABLE",
    "LandlockProbe",
    "landlock_shim_prefix",
    "probe_landlock",
]
