"""Runtime integration status and doctor helpers."""

__all__ = [
    "IntegrationState",
    "IntegrationStatus",
    "RuntimeProbe",
    "RuntimeReport",
    "collect_runtime_status",
]


# PEP 562 lazy attribute access. ``from clio_agent.runtime import X``
# still works without triggering status's heavy imports — clio_agent.tools.gateway
# alone is ~1 s. (The old ``runtime.hooks`` registry was deleted in P2.2 #1070; the
# hook system now lives in ``clio_agent.gact.hooks``.)
def __getattr__(name: str):
    if name in {
        "IntegrationState",
        "IntegrationStatus",
        "RuntimeProbe",
        "RuntimeReport",
        "collect_runtime_status",
    }:
        from clio_agent.runtime import status as _status  # noqa: PLC0415

        return getattr(_status, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
