"""A clio-core ARC store that cannot be brought up is a typed error (owner module).

clio-core is THE context system (agent-loop rebuild, owner principle 2026-09-28). When
the clio-core backend is not installed or fails to initialize, the store factory raises
:class:`ArcStoreUnavailableError` carrying a typed reason from
:data:`ARC_INIT_DEGRADE_REASONS`; nothing runs on another store. This supersedes the
#897 loud degrade to LocalFS. The one sanctioned fallback -- the platform cannot run
clio-core at all (``clio_core_binding_absent``) -> the loud DSPy ``History`` mode -- is
decided by the app from that reason, never by the store factory.
"""

from __future__ import annotations

from clio_agent.arc.clio_core_daemon_version import (
    CLIO_CORE_DAEMON_CONFIG_UNKNOWN,
    CLIO_CORE_DAEMON_VERSION_UNKNOWN,
    CLIO_CORE_VERSION_MISMATCH,
)
from clio_agent.errors import ClioError

CLIO_CORE_FILE_CAPACITY_UNAVAILABLE = "clio_core_file_capacity_unavailable"
CLIO_CORE_CLIENT_ATTACH_FAILED = "clio_core_client_attach_failed"
CLIO_CORE_POST_ATTACH_PROBE_TIMEOUT = "clio_core_post_attach_probe_timeout"
CLIO_CORE_CLIENT_ATTACH_TIMEOUT = "clio_core_client_attach_timeout"
CLIO_CORE_NATIVE_CLIENT_EXIT = "clio_core_native_client_exit"

# Typed reason codes for a clio-core init failure. The vocabulary mirrors
# the #892 liveness/quarantine reasons so operators read one consistent language.
ARC_INIT_DEGRADE_REASONS = (
    "clio_core_binding_absent",  # iowarp_core / clio_cte_core_ext not importable
    "clio_core_daemon_spawn_failed",  # launcher missing or the daemon never bound its port
    CLIO_CORE_FILE_CAPACITY_UNAVAILABLE,  # configured file bdev cannot fit safely
    CLIO_CORE_CLIENT_ATTACH_FAILED,  # daemon listening, native client handshake failed
    CLIO_CORE_CLIENT_ATTACH_TIMEOUT,  # the handshake got no answer within the bound
    CLIO_CORE_NATIVE_CLIENT_EXIT,  # the native client would have exited the process
    CLIO_CORE_POST_ATTACH_PROBE_TIMEOUT,  # attached, but the first RPC got no answer in time
    CLIO_CORE_VERSION_MISMATCH,  # daemon runs a different iowarp-core version: refused
    CLIO_CORE_DAEMON_VERSION_UNKNOWN,  # daemon has no version record: refused
    CLIO_CORE_DAEMON_CONFIG_UNKNOWN,  # daemon's recorded config is unreadable: refused
    "clio_core_init_error",  # any other clio-core initialization failure
)


def classify_init_failure(error: BaseException) -> str:
    """Map a ClioCoreStore init failure to a typed :data:`ARC_INIT_DEGRADE_REASONS` code.

    Args:
        error: The exception raised while constructing the clio-core-backed store.

    Returns:
        ``"clio_core_binding_absent"`` for a missing clio-core Python binding,
        ``"clio_core_daemon_spawn_failed"`` for a launcher/port-bind failure,
        the error's own typed ``degradation_reason`` when it carries a known one
        (file-tier preflight, client attach, daemon version/config refusal), else
        ``"clio_core_init_error"``.
    """
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return "clio_core_binding_absent"
    typed = getattr(error, "degradation_reason", None)
    if typed in ARC_INIT_DEGRADE_REASONS:
        return str(typed)
    message = str(error).lower()
    if isinstance(error, RuntimeError) and (
        "launcher" in message or "never bound port" in message or "clio_run" in message
    ):
        return "clio_core_daemon_spawn_failed"
    return "clio_core_init_error"


class ArcStoreUnavailableError(ClioError):
    """clio-core could not be brought up as the ARC store; nothing else is used."""

    def __init__(self, *, error: BaseException, config_path: str) -> None:
        self.reason = classify_init_failure(error)
        self.config_path = config_path
        super().__init__(
            f"clio-core is unavailable, so CLIO cannot hold the agent's context "
            f"(reason={self.reason}; config={config_path or '<default>'}): {error}",
            error_type=self.reason,
            details={
                "reason": self.reason,
                "config_path": config_path,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
