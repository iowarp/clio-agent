"""Configuration and lazy construction for agentic provenance providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher
from clio_agent.gact.provenance.flowcept import FlowceptProviderConfig
from clio_agent.gact.provenance.jsonl import JsonlProvenanceProvider
from clio_agent.gact.provenance.protocol import ProviderReceipt

# The provider precedence ladder + default live in the neutral top-level module
# (arc/ reads the same decision without a gact import); re-exported here so
# this module's public surface is unchanged (#1247).
from clio_agent.provenance_config import (  # noqa: F401 - re-exported public API
    configured_provider_names,
    native_durable_provenance_enabled,
)


class _LegacyFactoryProvider:
    """Compatibility wrapper for the established Python trace factory."""

    name = "factory"
    durable = True
    queryable = False

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.name = str(getattr(backend, "name", "factory") or "factory")
        # Preserve the public inspection seam of the established custom
        # factory backend. Existing callers use these values to verify that
        # configuration and the default trace root reached their factory.
        self.config = getattr(backend, "config", None)
        self.default_root = getattr(backend, "default_root", None)
        # The wrapped backend is an ARBITRARY user-supplied object
        # (CLIO_SEMANTIC_TRACE_FACTORY) -- whether it can make flush() a
        # real persistence barrier is only knowable at construction, not
        # something to silently re-derive on every flush() call. Recorded
        # ONCE here and surfaced via ProviderHealth (GET
        # /v1/provenance/providers) so a factory backend with no flush hook
        # is a DISCOVERABLE gap, never a silently assumed one.
        backend_flush = getattr(backend, "flush", None)
        self._backend_flush = backend_flush if callable(backend_flush) else None
        self.flush_durable = self._backend_flush is not None
        self.flush_note = (
            ""
            if self._backend_flush is not None
            else (
                "the configured CLIO_SEMANTIC_TRACE_FACTORY backend has no "
                "flush(); this provider's flush() is a no-op and cannot "
                "guarantee pending writes have reached the backend's storage"
            )
        )

    def emit(self, event: Any) -> ProviderReceipt:
        self._backend.emit(event)
        return ProviderReceipt.ACCEPTED

    def flush(self) -> None:
        """Proxy to the wrapped factory backend's flush, when it has one.

        Mirrors :class:`JsonlProvenanceProvider.flush` — a custom factory
        backend may itself be an off-loop async writer, so silently no-op'ing
        here would reintroduce the same incomplete-flush gap this class
        exists to avoid. When the backend has no ``flush``, this IS an
        intentional no-op — not a bare, unexplained ``pass`` — because the
        gap is already declared once, at construction, via
        ``flush_durable``/``flush_note`` above.
        """
        if self._backend_flush is not None:
            self._backend_flush()

    def close(self) -> None:
        close = getattr(self._backend, "close", None)
        if callable(close):
            close()


def build_provenance_backend(default_root: Path) -> Any:
    """Build the selected downstream provider dispatcher."""
    from clio_agent.gact.semantic_events import NoopSemanticTraceBackend

    names = configured_provider_names()
    if not names:
        return NoopSemanticTraceBackend()

    providers: list[Any] = []
    for name in names:
        if name == "jsonl":
            raw_path = conf.resolve(
                "provenance.agentic.jsonl.path",
                env="CLIO_PROVENANCE_JSONL_PATH",
                default="",
                cast=conf.as_str,
            ).strip()
            if not raw_path:
                raw_path = conf.resolve(
                    "trace.path",
                    env="CLIO_SEMANTIC_TRACE_PATH",
                    default="",
                    cast=conf.as_str,
                ).strip()
            providers.append(
                JsonlProvenanceProvider(Path(raw_path).expanduser() if raw_path else default_root)
            )
        elif name == "flowcept":
            from clio_agent.gact.provenance.flowcept import FlowceptProvenanceProvider

            providers.append(FlowceptProvenanceProvider(_flowcept_config()))
        else:
            providers.append(_build_legacy_factory(default_root))

    queue_size = conf.resolve(
        "provenance.agentic.queue_size",
        env="CLIO_PROVENANCE_QUEUE_SIZE",
        default=4096,
        cast=conf.as_int,
    )
    if queue_size < 1:
        raise ValueError("provenance.agentic.queue_size must be at least 1")
    return ProvenanceDispatcher(providers, queue_size=queue_size)


def _flowcept_config() -> FlowceptProviderConfig:
    return FlowceptProviderConfig(
        settings_path=conf.resolve(
            "provenance.agentic.flowcept.settings_path",
            env="FLOWCEPT_SETTINGS_PATH",
            default="",
            cast=conf.as_str,
        ).strip(),
        workflow_scope=conf.resolve(
            "provenance.agentic.flowcept.workflow_scope",
            env="CLIO_FLOWCEPT_WORKFLOW_SCOPE",
            default="session",
            cast=conf.as_str,
        )
        .strip()
        .lower(),
        campaign_scope=conf.resolve(
            "provenance.agentic.flowcept.campaign_scope",
            env="CLIO_FLOWCEPT_CAMPAIGN_SCOPE",
            default="session",
            cast=conf.as_str,
        )
        .strip()
        .lower(),
        campaign_id=conf.resolve(
            "provenance.agentic.flowcept.campaign_id",
            env="CLIO_FLOWCEPT_CAMPAIGN_ID",
            default="",
            cast=conf.as_str,
        ).strip(),
        privacy=conf.resolve(
            "provenance.agentic.flowcept.privacy",
            env="CLIO_FLOWCEPT_PRIVACY",
            default="metadata",
            cast=conf.as_str,
        )
        .strip()
        .lower(),
        include_events=tuple(
            conf.resolve(
                "provenance.agentic.flowcept.include_events",
                env="CLIO_FLOWCEPT_INCLUDE_EVENTS",
                default=["*"],
                cast=conf.as_csv,
            )
        ),
        exclude_events=tuple(
            conf.resolve(
                "provenance.agentic.flowcept.exclude_events",
                env="CLIO_FLOWCEPT_EXCLUDE_EVENTS",
                default=["lm.token.delta", "thinking.*"],
                cast=conf.as_csv,
            )
        ),
        check_safe_stops=conf.resolve(
            "provenance.agentic.flowcept.check_safe_stops",
            env="CLIO_FLOWCEPT_CHECK_SAFE_STOPS",
            default=True,
            cast=conf.as_bool,
        ),
    )


def _build_legacy_factory(default_root: Path) -> _LegacyFactoryProvider:
    from clio_agent.gact.semantic_events import _load_factory

    factory_path = conf.resolve(
        "trace.semantic_factory",
        env="CLIO_SEMANTIC_TRACE_FACTORY",
        default="",
        cast=conf.as_str,
    ).strip()
    if not factory_path:
        raise ValueError("CLIO_SEMANTIC_TRACE_FACTORY is required for provider 'factory'")
    raw_config = conf.resolve(
        "trace.semantic_config",
        env="CLIO_SEMANTIC_TRACE_CONFIG",
        default="",
        cast=conf.as_str,
    ).strip()
    backend = _load_factory(factory_path)(
        default_root=default_root,
        config=json.loads(raw_config) if raw_config else {},
    )
    if not callable(getattr(backend, "emit", None)):
        raise TypeError("semantic trace factory must return an object with emit(event)")
    return _LegacyFactoryProvider(backend)
