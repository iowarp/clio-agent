"""Windows srt write-fence ENFORCEMENT verification (#1026 — kill the false-green).

``clio sandbox setup`` provisions the ``srt-sandbox`` principal, but a provisioned account is
NOT proof srt can actually confine a child. On some hosts srt's own credentialed spawn
(``CreateProcessWithLogonW(srt-sandbox)``) fails and NO write is ever fenced — yet the marker
+ principal both exist, so a marker-only probe reports ``active/ready``. That is a false-green
(a fence that does nothing, reported as protecting the box).

This module runs the real behavioural check: spawn a confined child that attempts an
out-of-root write and confirm it is DENIED. The provisioning flow persists the verdict in the
marker; the ladder then floors HONESTLY (``mechanism=none`` + a typed reason) when srt cannot
enforce, instead of claiming a write fence that is not in force. Split into its own owner
module (not appended to ``sandbox_provision``) to respect the no-accretion ratchet.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Typed enforcement verdicts (no silent fallback — every outcome names itself).
REASON_WINDOWS_ENFORCEMENT_VERIFIED = "srt_windows_enforcement_verified"
REASON_WINDOWS_ENFORCEMENT_UNVERIFIED = "srt_windows_enforcement_unverified"
REASON_WINDOWS_ENFORCEMENT_ESCAPED = "srt_windows_enforcement_escaped"
REASON_NOT_WINDOWS = "not_windows"

#: Bounded spawn timeout for the probe — a hung srt must never stall ``clio sandbox setup``.
_PROBE_TIMEOUT_S = 60


def verify_windows_enforcement(
    binary: str,
    *,
    platform: str = sys.platform,
    runner: Optional[Callable[[str], tuple[bool, str]]] = None,
) -> tuple[bool, str]:
    """Prove srt actually ENFORCES a Windows write fence (fail-safe, never raises).

    Spawns a confined child that attempts an out-of-root write and confirms it is DENIED.
    Returns ``(enforces, reason)``. Any spawn/setup failure is an honest
    ``(False, srt_windows_enforcement_unverified)`` — the fence is unproven, so the ladder must
    NOT claim it (precision-over-recall: only a positively-observed denial yields ``True``).

    ``runner`` is injectable so the whole matrix is unit-pinnable without touching srt; the real
    probe (:func:`_run_enforcement_probe`) is win32-only and never unit-run.
    """
    if not platform.startswith("win"):
        return False, REASON_NOT_WINDOWS
    run = runner if runner is not None else _run_enforcement_probe
    try:
        return run(binary)
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ enforcement unproven, never a false-green
        logger.info(
            "windows enforcement verify failed reason=%s error=%r",
            REASON_WINDOWS_ENFORCEMENT_UNVERIFIED,
            exc,
        )
        return False, REASON_WINDOWS_ENFORCEMENT_UNVERIFIED


def _run_enforcement_probe(binary: str) -> tuple[bool, str]:
    """The real behavioural probe (win32; never unit-run — tests inject ``runner``).

    Fences a fresh temp ``allow`` dir and runs ``srt -s <cfg> -- cmd /c`` writing to a path
    OUTSIDE the fence. Enforcement ⇒ the write is denied (file absent AND the child spawned):
    :data:`REASON_WINDOWS_ENFORCEMENT_VERIFIED`. If the file appears the fence let an out-of-root
    write through: :data:`REASON_WINDOWS_ENFORCEMENT_ESCAPED`. If srt could not even spawn the
    confined child (``CreateProcessWithLogonW`` in the output) the fence is UNVERIFIED (an
    upstream srt limitation): :data:`REASON_WINDOWS_ENFORCEMENT_UNVERIFIED`.
    """
    import json  # noqa: PLC0415 - only on this path
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from clio_agent.runtime import sandbox_srt  # noqa: PLC0415

    with (
        tempfile.TemporaryDirectory(prefix="clio-srt-allow-") as allow,
        tempfile.TemporaryDirectory(prefix="clio-srt-out-") as outside,
    ):
        target = Path(outside) / "denied.txt"
        cfg = sandbox_srt.synthesize_srt_config([allow])
        sandbox_srt.validate_srt_config(cfg)
        settings = Path(allow) / "_verify_settings.json"
        settings.write_text(json.dumps(cfg), encoding="utf-8")
        argv: list[Any] = [
            *sandbox_srt.srt_prefix(binary, settings),
            "cmd",
            "/c",
            f'type nul > "{target}"',
        ]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, check=False
        )
        if target.exists():
            # The confined child wrote OUTSIDE its territory — the fence did not hold.
            return False, REASON_WINDOWS_ENFORCEMENT_ESCAPED
        blob = f"{proc.stdout}\n{proc.stderr}".lower()
        if "createprocesswithlogon" in blob:
            # srt never spawned the confined child (credentialed logon failed) — nothing enforced.
            return False, REASON_WINDOWS_ENFORCEMENT_UNVERIFIED
        # Child spawned and the out-of-root write did not land ⇒ the fence is genuinely in force.
        return True, REASON_WINDOWS_ENFORCEMENT_VERIFIED


__all__ = [
    "REASON_NOT_WINDOWS",
    "REASON_WINDOWS_ENFORCEMENT_ESCAPED",
    "REASON_WINDOWS_ENFORCEMENT_UNVERIFIED",
    "REASON_WINDOWS_ENFORCEMENT_VERIFIED",
    "verify_windows_enforcement",
]
