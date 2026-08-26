"""Bounded off-turn fan-out for semantic provenance providers."""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


class _ProviderWorker:
    """One bounded queue and worker for one provider."""

    def __init__(self, provider: ProvenanceProvider, *, queue_size: int) -> None:
        self.provider = provider
        self.queue: queue.Queue[SemanticEvent | None] = queue.Queue(maxsize=queue_size)
        # flush_durable/flush_note are OPTIONAL provider instance attributes
        # (not part of the required Protocol surface -- only the flush()
        # METHOD is required) read ONCE here, at construction, since they are
        # a static property of the provider instance, not something that
        # changes call to call. Default True/"" is the honest stance for a
        # provider that does not set them (its emit() is already
        # synchronous, so an unstated flush() returning immediately IS a
        # real barrier); a provider with a genuine gap (Flowcept, a
        # factory-wrapped backend with no flush) sets flush_durable=False
        # with a flush_note explaining why, surfaced verbatim via
        # ProviderHealth/GET /v1/provenance/providers.
        self.health = ProviderHealth(
            name=provider.name,
            queryable=provider.queryable,
            durable=provider.durable,
            flush_durable=bool(getattr(provider, "flush_durable", True)),
            flush_note=str(getattr(provider, "flush_note", "") or ""),
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
                        first_failure = self.health.failed == 1
                        self.health.status = "degraded"
                        self.health.last_error = f"{type(exc).__name__}: {exc}"
                    if first_failure:
                        # No-silent-fallback: health captured this, but nothing
                        # read health during the CMF qualification and the
                        # zero-edges outcome went unnoticed (#1247). First
                        # failure per worker is LOUD; the rest stay counters.
                        logger.warning(
                            "provenance provider %s degraded on emit "
                            "(event_type=%s): %s: %s -- further failures are "
                            "counted in health only",
                            getattr(self.provider, "name", "?"),
                            getattr(event, "event_type", "?"),
                            type(exc).__name__,
                            exc,
                        )
            finally:
                self.queue.task_done()

    def flush(self) -> None:
        """Block until every accepted event is processed AND, per this
        provider's own stated guarantee, actually persisted.

        ``queue.join()`` alone only proves ``provider.emit()`` returned for
        every accepted event — it says nothing about that provider's OWN
        downstream persistence. ``provider.flush()`` (REQUIRED by the
        Protocol) is the second, provider-owned half of the guarantee.
        Contained exactly like :meth:`_run` contains a raising ``emit``: a
        raising ``flush()`` is recorded in health, logged loudly on first
        occurrence, and never aborts a SIBLING provider's join/flush (an
        arbitrary user-supplied ``CLIO_SEMANTIC_TRACE_FACTORY`` backend is
        reachable here, so this must not be allowed to propagate into an API
        response or skip the rest of ``ProvenanceDispatcher.flush()``'s
        loop).
        """
        self.queue.join()
        try:
            self.provider.flush()
        except Exception as exc:  # noqa: BLE001 - a raising flush must not crash the caller or skip siblings
            with self._lock:
                self.health.failed += 1
                first_failure = self.health.failed == 1
                self.health.status = "degraded"
                self.health.last_error = f"{type(exc).__name__}: {exc}"
            if first_failure:
                # Mirrors _run's loud-first-failure posture (#1247): health
                # captures every occurrence, but nothing reads health unless
                # asked, so the FIRST failure per worker is also logged.
                logger.warning(
                    "provenance provider %s degraded on flush: %s: %s -- "
                    "further failures are counted in health only",
                    getattr(self.provider, "name", "?"),
                    type(exc).__name__,
                    exc,
                )

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
        """Wait until every accepted event is processed and, PER PROVIDER, as
        durably persisted as that provider can honestly promise.

        This is NOT a uniform hard barrier across every configured
        provider — check ``health()[...]["flush_durable"]`` (surfaced at
        ``GET /v1/provenance/providers``) to know which providers this call
        actually guarantees:

        * ``flush_durable=True`` (jsonl; native/CMF artifact providers): a
          returning ``flush()`` IS a real synchronous-persistence barrier
          for that provider — the #1247 CI regression this fixed (jsonl's
          own writer is async behind ``emit()``; the old ``queue.join()``-only
          implementation only proved hand-off, not persistence).
        * ``flush_durable=False`` (Flowcept; a factory backend with no
          ``flush``): this call still RUNS that provider's ``flush()`` (an
          honest no-op) but does NOT guarantee its buffered delivery has
          landed — a caller reading through that provider's own query path
          right after ``flush()`` can still race it (see
          ``routes/provenance.py``'s execution-provenance read).

        A provider whose ``flush()`` raises is CONTAINED per-provider
        (:meth:`_ProviderWorker.flush`) so one bad provider's failure never
        escapes into a caller (e.g. an API response) and never skips the
        join/flush of the OTHER configured providers.
        """
        for worker in self._workers.values():
            worker.flush()

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
