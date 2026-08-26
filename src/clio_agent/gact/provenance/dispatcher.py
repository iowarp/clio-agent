"""Bounded off-turn fan-out for semantic provenance providers."""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

from clio_agent.gact.provenance.protocol import (
    ExecutionProvenanceReader,
    ProvenanceProvider,
    ProviderHealth,
    ProviderReceipt,
)

if TYPE_CHECKING:
    from clio_agent.gact.semantic_events import SemanticEvent


class _ProviderWorker:
    """One bounded queue and worker for one provider."""

    def __init__(self, provider: ProvenanceProvider, *, queue_size: int) -> None:
        self.provider = provider
        self.queue: queue.Queue[SemanticEvent | None] = queue.Queue(maxsize=queue_size)
        self.health = ProviderHealth(
            name=provider.name,
            queryable=provider.queryable,
            durable=provider.durable,
        )
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"ProvenanceProvider-{provider.name}",
            daemon=True,
        )
        self._thread.start()

    def submit(self, event: SemanticEvent) -> ProviderReceipt:
        """Enqueue without blocking the semantic highway."""
        if self._closed:
            with self._lock:
                self.health.failed += 1
                self.health.status = "unavailable"
                self.health.last_error = "provider worker is closed"
            return ProviderReceipt.FAILED
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self.health.overflow += 1
                self.health.status = "degraded"
                self.health.last_error = "provider queue overflow"
            return ProviderReceipt.OVERFLOW
        with self._lock:
            self.health.accepted += 1
        return ProviderReceipt.ACCEPTED

    def snapshot(self) -> ProviderHealth:
        """Return a race-safe health copy."""
        with self._lock:
            return replace(self.health, queue_depth=self.queue.qsize())

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            try:
                if event is None:
                    return
                try:
                    receipt = self.provider.emit(event)
                    if receipt == ProviderReceipt.FILTERED:
                        with self._lock:
                            self.health.filtered += 1
                except Exception as exc:  # noqa: BLE001 - downstream cannot kill the worker
                    with self._lock:
                        self.health.failed += 1
                        self.health.status = "degraded"
                        self.health.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.queue.task_done()

    def close(self) -> None:
        """Drain accepted events and close the provider exactly once."""
        if self._closed:
            return
        self._closed = True
        self.queue.join()
        self.queue.put(None)
        self._thread.join(timeout=10.0)
        try:
            self.provider.close()
        except Exception as exc:  # noqa: BLE001 - health carries teardown failures
            with self._lock:
                self.health.failed += 1
                self.health.status = "degraded"
                self.health.last_error = f"{type(exc).__name__}: {exc}"


class ProvenanceDispatcher:
    """SemanticTraceBackend-compatible bounded provider dispatcher."""

    def __init__(self, providers: list[ProvenanceProvider], *, queue_size: int = 4096) -> None:
        self.name = providers[0].name if len(providers) == 1 else "provenance"
        if len(providers) == 1:
            provider = providers[0]
            # The legacy Python factory backend exposed these two inspection
            # attributes directly. Retain them when the dispatcher wraps the
            # sole provider so this migration does not break existing callers.
            if hasattr(provider, "config"):
                self.config = provider.config  # type: ignore[attr-defined]
            if hasattr(provider, "default_root"):
                self.default_root = provider.default_root  # type: ignore[attr-defined]
        self._workers = {
            provider.name: _ProviderWorker(provider, queue_size=queue_size)
            for provider in providers
        }

    @property
    def provider_names(self) -> tuple[str, ...]:
        """Configured provider names in deterministic order."""
        return tuple(self._workers)

    @property
    def durable(self) -> bool:
        """Whether a replay-capable provider is configured."""
        return any(worker.provider.durable for worker in self._workers.values())

    def emit(self, event: SemanticEvent) -> None:
        """Fan out without blocking on any provider."""
        for worker in self._workers.values():
            worker.submit(event)

    def flush(self) -> None:
        """Wait until every accepted provider event has been processed."""
        for worker in self._workers.values():
            worker.queue.join()

    def close(self) -> None:
        """Drain and close every provider."""
        for worker in self._workers.values():
            worker.close()

    def health(self) -> list[dict[str, object]]:
        """Return bounded provider status rows."""
        return [worker.snapshot().to_dict() for worker in self._workers.values()]

    def reader(self, name: str) -> ExecutionProvenanceReader | None:
        """Return a configured provider's optional query capability."""
        worker = self._workers.get(name)
        provider = worker.provider if worker is not None else None
        return provider if isinstance(provider, ExecutionProvenanceReader) else None
