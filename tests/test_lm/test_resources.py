"""Ownership, cancellation and error semantics of operation-scoped LM clients."""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from clio_agent.lm.resources import async_model_resources, model_resources, record_created_lm


class Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed = 0
        self.fail = fail

    def close(self) -> None:
        self.closed += 1

    async def aclose(self) -> None:
        self.closed += 1
        if self.fail:
            raise RuntimeError("cleanup failed")


def test_sync_scope_closes_only_its_fresh_clients_and_nested_scopes_are_independent() -> None:
    borrowed = Client()
    with model_resources():
        outer = Client()
        record_created_lm(outer)
        with model_resources():
            inner = Client()
            record_created_lm(inner)
        assert inner.closed == 1 and outer.closed == 0
    assert outer.closed == 1 and borrowed.closed == 0


def test_late_resolver_after_cancel_cannot_leave_a_new_client_unowned() -> None:
    with model_resources():
        inherited = contextvars.copy_context()
    late = Client()
    inherited.run(record_created_lm, late)
    assert late.closed == 1


def test_async_scope_closes_despite_cancellation_and_preserves_primary_failure() -> None:
    async def run() -> None:
        good, bad = Client(), Client(fail=True)
        with pytest.raises(asyncio.CancelledError) as caught:
            async with async_model_resources():
                record_created_lm(good)
                record_created_lm(bad)
                raise asyncio.CancelledError()
        assert good.closed == 1 and bad.closed == 1
        assert "cleanup also failed" in caught.value.__notes__[0]

    asyncio.run(run())


def test_cleanup_failure_is_reported_after_all_clients_are_closed() -> None:
    first, second = Client(fail=True), Client()
    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        with model_resources():
            record_created_lm(second)
            record_created_lm(first)
    assert first.closed == 1 and second.closed == 1


def test_copied_worker_context_enrolls_only_into_the_current_async_scope() -> None:
    async def run() -> None:
        lm = Client()
        async with async_model_resources():
            await asyncio.to_thread(record_created_lm, lm)
        assert lm.closed == 1

    asyncio.run(run())
