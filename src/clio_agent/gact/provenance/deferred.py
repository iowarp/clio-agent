"""Attach-when-ready wrapper for provenance providers with external services.

A provider such as Flowcept needs its own services (Redis/MongoDB) at
construction. Those services are often started THROUGH CLIO, so failing
``clio-agent serve`` when they are down is a deadlock (F047). This wrapper
tries once at startup, then re-attaches from the provider worker thread at a
bounded interval; while detached it reports ``unavailable`` with the typed
reason instead of crashing or silently dropping.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact.provenance.protocol import ProviderReceipt

if TYPE_CHECKING:
    from clio_agent.gact.semantic_events import SemanticEvent

logger = logging.getLogger(__name__)

DEFAULT_RETRY_SECONDS = 30.0


class ProviderUnavailableError(RuntimeError):
    """The wrapped provider could not be attached (its services are down)."""


class DeferredProvider:
    """Builds the real provider lazily; contains construction failures."""

    def __init__(
        self,
        name: str,
        build: Callable[[], Any],
        *,
        durable: bool,
        queryable: bool,
        flush_durable: bool = True,
        flush_note: str = "",
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.durable = durable
        self.queryable = queryable
        self.flush_durable = flush_durable
        self.flush_note = flush_note
        self._build = build
        self._retry_seconds = retry_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._inner: Any = None
        self._next_attempt = 0.0
        self._closed = False
        self.unavailable_reason = ""
        self._attach(startup=True)

    @property
    def attached(self) -> bool:
        return self._inner is not None

    def _attach(self, *, startup: bool = False) -> Any:
        with self._lock:
            if self._inner is not None or self._closed:
                return self._inner
            if not startup and self._clock() < self._next_attempt:
                return None
            try:
                self._inner = self._build()
            except Exception as exc:
                # Loud, typed degradation: the reason is logged with its
                # traceback on every attempt (bounded by the retry interval)
                # and surfaced as provider health ``unavailable``.
                self._next_attempt = self._clock() + self._retry_seconds
                self.unavailable_reason = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "provenance provider %s unavailable (%s); CLIO continues without it "
                    "and retries in %.0fs",
                    self.name,
                    self.unavailable_reason,
                    self._retry_seconds,
                    exc_info=True,
                )
                return None
            if not startup or self.unavailable_reason:
                logger.info("provenance provider %s attached", self.name)
            self.unavailable_reason = ""
            return self._inner

    def recheck(self) -> bool:
        """Retry attaching (bounded by the retry interval); True when attached.

        Health reads call this so an idle CLIO notices a Flowcept that came up
        after boot, without waiting for the next event or a restart.
        """
        return self._attach() is not None

    def _require(self) -> Any:
        inner = self._attach()
        if inner is None:
            raise ProviderUnavailableError(
                f"provider_unavailable: {self.name} is not reachable ({self.unavailable_reason})"
            )
        return inner

    def emit(self, event: SemanticEvent) -> ProviderReceipt | None:
        return self._require().emit(event)

    def flush(self) -> None:
        if self._inner is not None:
            self._inner.flush()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            inner = self._inner
        if inner is not None:
            inner.close()

    def query_tasks(self, filter: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]] | None:
        """``None`` (query failed) while detached, as the wrapped reader reports it."""
        inner = self._attach()
        return None if inner is None else inner.query_tasks(filter, **kwargs)

    def query_workflows(self, filter: dict[str, Any]) -> list[dict[str, Any]] | None:
        inner = self._attach()
        return None if inner is None else inner.query_workflows(filter)

    def query_execution(
        self,
        *,
        session_id: str,
        child_session_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        return self._require().query_execution(
            session_id=session_id, child_session_ids=child_session_ids, limit=limit
        )

    def __getattr__(self, item: str) -> Any:
        # Only reached for attributes not defined above (e.g. a provider's
        # own helpers); detached → AttributeError like an absent capability.
        if item.startswith("_"):
            raise AttributeError(item)
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(item)
        return getattr(inner, item)
