"""ARC init-time degradation record + registry (owner module, #897).

When ARC is configured for the clio-core CTE backend (the default) but clio-core
is **not installed** or **fails to initialize**, the system degrades to
:class:`~clio_agent.arc.storage.LocalFSStore` -- but *loudly*, never silently
(CLAUDE.md no-silent-fallback ground rule). "Loudly" means three things, and this
module owns the machinery for all of them so ``storage.make_arc_store`` stays a
thin call site under its size ratchet:

* a **typed degradation reason** (one of :data:`ARC_INIT_DEGRADE_REASONS`)
  attributing the cause -- clio-core binding absent vs daemon spawn failure vs a
  generic init error -- reusing the #892 liveness/quarantine vocabulary;
* a **startup log line** at WARNING;
* a **process-local record** other in-process code can read -- the doctor
  surfaces it as a DEGRADED row
  (:func:`clio_agent.runtime.cte_health.probe_cte_init_degradation`), mirroring
  the #892 gate registry.

This is INIT-time only. A mid-life daemon loss on an already-initialized store
stays the #892 quarantine path. On the next boot clio-core is retried afresh (no
sticky state), so the record is process-local and not persisted.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Typed reason codes for an init-time degrade to LocalFS. The vocabulary mirrors
# the #892 liveness/quarantine reasons so operators read one consistent language.
ARC_INIT_DEGRADE_REASONS = (
    "cte_binding_absent",  # iowarp_core / clio_cte_core_ext not importable
    "cte_daemon_spawn_failed",  # launcher missing or the daemon never bound its port
    "cte_init_error",  # any other clio-core initialization failure
)


def classify_init_failure(error: BaseException) -> str:
    """Map a CTEStore init failure to a typed :data:`ARC_INIT_DEGRADE_REASONS` code.

    Args:
        error: The exception raised while constructing the CTE-backed store.

    Returns:
        ``"cte_binding_absent"`` for a missing clio-core Python binding,
        ``"cte_daemon_spawn_failed"`` for a launcher/port-bind failure, else
        ``"cte_init_error"``.
    """
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return "cte_binding_absent"
    message = str(error).lower()
    if isinstance(error, RuntimeError) and (
        "launcher" in message or "never bound port" in message or "clio_run" in message
    ):
        return "cte_daemon_spawn_failed"
    return "cte_init_error"


@dataclass(frozen=True)
class ArcInitDegradation:
    """A recorded init-time degrade from the CTE backend to LocalFS (#897)."""

    reason: str  # one of ARC_INIT_DEGRADE_REASONS
    choice: str  # the selected backend that degraded ("cte" or the default)
    was_explicit: bool  # True when CLIO_ARC_STORE=cte was set explicitly
    config_path: str  # the clio-core config path that was attempted
    error_type: str  # exception class name
    error: str  # exception message
    data_dir: str  # the LocalFS data dir the store fell back to

    def to_details(self) -> dict[str, object]:
        """JSON-safe detail payload for the doctor row."""
        return {
            "reason": self.reason,
            "backend_choice": self.choice,
            "was_explicit": self.was_explicit,
            "config_path": self.config_path,
            "error_type": self.error_type,
            "error": self.error,
            "fell_back_to": self.data_dir,
            "storage_mode": "local",
        }


# Process-local record of the most-recent init-time degradation. Init runs once
# per process (CTEStore._initialized guard), so a single latest-record slot is
# sufficient; a doctor report IN the same process surfaces it. A separate process
# that never degraded holds None and correctly reports nothing.
_lock = threading.Lock()
_last_degradation: ArcInitDegradation | None = None


def _cte_selected_explicitly(backend: str | None) -> bool:
    """Whether the CTE backend was chosen explicitly (arg or env) vs the default.

    The degrade is loud either way (owner ruling on #897); this only labels the
    doctor row so an operator can tell an explicit ``CLIO_ARC_STORE=cte`` deploy
    from the implicit default.
    """
    if (backend or "").strip().lower() == "cte":
        return True
    return os.environ.get("CLIO_ARC_STORE", "").strip().lower() == "cte"


def record_arc_init_degradation(
    *,
    backend: str | None,
    config_path: str,
    error: BaseException,
    data_dir: str,
) -> ArcInitDegradation:
    """Record a loud init-time degrade to LocalFS and emit the startup log line.

    Args:
        backend: The explicit ``backend`` arg passed to ``make_arc_store`` (or None
            when selection came from env/default); used only to label the row.
        config_path: The clio-core config path that was attempted.
        error: The exception raised during CTE init.
        data_dir: The LocalFS data dir the store fell back to.

    Returns:
        The recorded :class:`ArcInitDegradation`.
    """
    reason = classify_init_failure(error)
    record = ArcInitDegradation(
        reason=reason,
        choice="cte",
        was_explicit=_cte_selected_explicitly(backend),
        config_path=config_path,
        error_type=type(error).__name__,
        error=str(error),
        data_dir=data_dir,
    )
    with _lock:
        global _last_degradation
        _last_degradation = record
    logger.warning(
        "ARC degraded to LocalFSStore at init: the clio-core CTE backend is "
        "unavailable (reason=%s error=%s: %s); ARC is running on local files under "
        "%s. This is a LOUD degrade, not a silent fallback (#897): fix the clio-core "
        "install/config to restore the tiered backend, or set CLIO_ARC_STORE=local to "
        "choose LocalFS deliberately (no degrade row).",
        reason,
        record.error_type,
        record.error,
        data_dir,
    )
    return record


def arc_init_degradation_snapshot() -> ArcInitDegradation | None:
    """Return the most-recent init-time degradation in this process, or None."""
    with _lock:
        return _last_degradation


def reset_arc_init_degradation() -> None:
    """Clear the recorded degradation (test seam; not used in production)."""
    with _lock:
        global _last_degradation
        _last_degradation = None
