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

    ``available`` is ``True`` only when the kernel reports ABI ≥ 1 AND a real ruleset can be
    created (Landlock is in the active LSM — actually ENFORCEABLE, not merely present). ``abi``
    is the raw supported ABI (still reported for the doctor even when not enforceable).
    ``refer_supported`` is ABI ≥ 2. ``reason`` is the typed ladder rung on failure
    (:data:`REASON_KERNEL_TOO_OLD` when the syscall is absent, :data:`REASON_LANDLOCK_UNAVAILABLE`
    when not Linux, or when the syscall is present but Landlock cannot enforce), else ``""``.
    """

    available: bool
    abi: int
    refer_supported: bool
    reason: str


#: A minimal ABI-1-safe handled-access bit (``LANDLOCK_ACCESS_FS_WRITE_FILE``) used ONLY to
#: prove a ruleset can actually be CREATED (enforceability), never to restrict this process.
_FS_WRITE_FILE = 1 << 1


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


def _load_libc() -> Optional[ctypes.CDLL]:
    """Load libc for the syscall probes, or ``None`` when unavailable (non-glibc / Windows).

    ``ctypes.CDLL(None)`` raises ``OSError`` on some hosts and ``TypeError`` on Windows — both
    mean "no libc to probe through", so both map to ``None`` (probe reports unavailable). An
    injection seam for the unit tests, which exercise the enforceability branches without a
    real kernel.
    """
    try:
        return ctypes.CDLL(None, use_errno=True)
    except (OSError, TypeError):
        return None


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(ctypes.c_long(number), *args))


def _create_ruleset_version(libc: ctypes.CDLL) -> int:
    """Return the kernel's supported Landlock ABI (>0), 0 if disabled, or <0 on ``ENOSYS``.

    Calls ``landlock_create_ruleset(NULL, 0, VERSION)`` — the API-version probe. This proves
    the SYSCALL exists but NOT that Landlock can enforce (see :func:`_can_create_ruleset`).
    """
    try:
        return _syscall(
            libc,
            _NR_LANDLOCK_CREATE_RULESET,
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except Exception:  # noqa: BLE001 — a probe must never raise into boot; report unavailable
        return -1


def _can_create_ruleset(libc: ctypes.CDLL) -> bool:
    """Whether a REAL ruleset can be created — the non-destructive ENFORCEABILITY probe.

    The version probe returns the ABI on any kernel where the syscall EXISTS, even when
    Landlock is compiled but NOT in the active LSM stack (no ``lsm=…,landlock`` /
    ``CONFIG_LSM``) — e.g. some CI/cloud kernels. On such a kernel ``landlock_restrict_self``
    later fails ``EOPNOTSUPP``, so a fence "activated" off the version probe alone would make
    the shim exit 127 on EVERY spawn (a silent, total fence break). Creating a real ruleset
    with a minimal handled mask returns an fd ONLY when Landlock is actually enforceable
    (it returns ``EOPNOTSUPP`` when the LSM is inactive) — and creating a ruleset restricts
    NOTHING (only ``restrict_self`` does), so this is safe to run in the clio server process.
    ``/sys/kernel/security/lsm`` is NOT used: securityfs is unmounted on some hosts (e.g. WSL)
    where Landlock nonetheless works, so it would false-negative.
    """
    attr = _RulesetAttr(handled_access_fs=_FS_WRITE_FILE)
    try:
        fd = _syscall(
            libc,
            _NR_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.sizeof(attr),
            ctypes.c_uint(0),
        )
    except Exception:  # noqa: BLE001 — a probe must never raise into boot
        return False
    if fd < 0:
        return False
    import os  # noqa: PLC0415 - only on the probe path

    try:
        os.close(fd)
    except OSError:
        pass
    return True


def probe_landlock(*, platform: str = sys.platform) -> LandlockProbe:
    """Probe the running kernel for an ENFORCEABLE Landlock write-fence (owner decision #974.1).

    Non-Linux hosts are :data:`REASON_LANDLOCK_UNAVAILABLE` without touching libc. On Linux the
    ABI is read from the version probe AND enforceability is confirmed by creating a real
    ruleset (:func:`_can_create_ruleset`): a kernel where the syscall exists but Landlock is not
    in the active LSM reports ``landlock_unavailable`` rather than a fence that would 127 every
    spawn. ABI ≥ 1 AND creatable → available fs write-fence; else a typed reason
    (``kernel_too_old`` when the syscall is absent, ``landlock_unavailable`` when present but
    not enforceable).
    """
    if not platform.startswith("linux"):
        return LandlockProbe(False, 0, False, REASON_LANDLOCK_UNAVAILABLE)
    libc = _load_libc()
    if libc is None:
        return LandlockProbe(False, 0, False, REASON_LANDLOCK_UNAVAILABLE)
    abi = _create_ruleset_version(libc)
    if abi < 1:
        # abi == 0: syscall present but disabled; abi < 0: ENOSYS (kernel < 5.13).
        reason = REASON_KERNEL_TOO_OLD if abi < 0 else REASON_LANDLOCK_UNAVAILABLE
        return LandlockProbe(False, max(abi, 0), False, reason)
    if not _can_create_ruleset(libc):
        # Syscall present (ABI reported) but Landlock is not in the active LSM — not enforceable.
        return LandlockProbe(False, abi, abi >= _ABI_REFER, REASON_LANDLOCK_UNAVAILABLE)
    return LandlockProbe(True, abi, abi >= _ABI_REFER, "")


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
