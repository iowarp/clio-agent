"""Bounded PutBlob retry (#893 live-gate finding).

The gate's Seattle leg lost a turn to a transient clio-core write refusal
(PutBlob rc=13 with the tier under its cap; separately a container-restore
gap after a daemon restart). The store retries idempotent re-puts a bounded
number of times, loudly, then re-raises — these tests pin exactly that:
recovery on failure, typed warnings per attempt, and the final failure
surfacing unmodified.

They also pin the two rules the classification learned the hard way: EVERY
native refusal earns the bounded retry (no error-text allowlist), and write
health is live state a later successful write clears, never a process-life
latch that pins ``/v1/health`` at 503.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

import pytest

from clio_agent.arc.clio_core_retry import (
    CLIO_CORE_PUT_RETRY,
    CLIO_CORE_PUT_WRITE_LOST,
    CLIO_CORE_PUT_WRITE_RECOVERED,
    last_lost_put_write,
    put_blob_with_retry,
)
from clio_agent.runtime.clio_core_health import probe_clio_core_write_health
from clio_agent.runtime.status import IntegrationState


class _FlakyTag:
    """PutBlob fails ``failures`` times, then succeeds; records every call."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[tuple[str, bytes]] = []

    def PutBlob(self, name: str, payload: bytes, flags: int) -> None:  # noqa: N802
        self.calls.append((name, payload))
        if len(self.calls) <= self.failures:
            raise RuntimeError("PutBlob operation failed (rc=13)")


class _NovelErrorTag:
    """PutBlob fails ``failures`` times with an error text nobody captured."""

    def __init__(self, failures: int, message: str) -> None:
        self.failures = failures
        self.message = message
        self.calls = 0

    def PutBlob(self, name: str, payload: bytes, flags: int) -> None:  # noqa: N802
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(self.message)


@pytest.fixture(autouse=True)
def _reset_write_health() -> Iterator[None]:
    import clio_agent.arc.clio_core_retry as retry

    retry._reset_put_write_health_for_tests()
    yield
    retry._reset_put_write_health_for_tests()


def test_transient_failure_recovers_with_typed_warnings(
    caplog: "pytest.LogCaptureFixture", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("clio_agent.arc.clio_core_retry.time.sleep", lambda _s: None)
    tag = _FlakyTag(failures=2)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_retry"):
        put_blob_with_retry(tag, "blob-a", b"payload")
    assert len(tag.calls) == 3
    # Every retried attempt is a typed loud warning — never silent.
    warns = [r for r in caplog.records if CLIO_CORE_PUT_RETRY in r.getMessage()]
    assert len(warns) == 2
    assert "rc=13" in warns[0].getMessage()


def test_persistent_failure_reraises_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("clio_agent.arc.clio_core_retry.time.sleep", lambda _s: None)
    tag = _FlakyTag(failures=99)
    with pytest.raises(RuntimeError, match="rc=13"):
        put_blob_with_retry(tag, "blob-b", b"payload")
    assert len(tag.calls) == 3  # bounded — no infinite retry


def test_success_first_try_is_silent(caplog: "pytest.LogCaptureFixture") -> None:
    tag = _FlakyTag(failures=0)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_retry"):
        put_blob_with_retry(tag, "blob-c", b"payload")
    assert len(tag.calls) == 1
    assert not caplog.records


def test_retry_resends_identical_payload() -> None:
    """The retry is safe because it is an IDENTICAL idempotent re-put."""
    import clio_agent.arc.clio_core_retry as m

    orig_sleep = m.time.sleep
    m.time.sleep = lambda _s: None
    try:
        tag = _FlakyTag(failures=1)
        put_blob_with_retry(tag, "blob-d", b"same-bytes")
    finally:
        m.time.sleep = orig_sleep
    assert tag.calls[0] == tag.calls[1] == ("blob-d", b"same-bytes")


@pytest.mark.parametrize(
    "message",
    [
        # The two shapes captured from ONE live incident used to be the only
        # texts that earned a retry; every other native refusal failed on
        # attempt 1. A deployment whose cte_main pool id is not 512.0 (a
        # never-rewritten cte.yaml, a workspace store_config) raises this.
        "Container not found for pool_id 256.0",
        "PutBlob operation failed (rc=21)",
        "chi::TagPut failed: unexpected native refusal",
    ],
)
def test_novel_error_text_still_gets_the_bounded_retry(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classification never reads the error TEXT: unknown == retried."""

    monkeypatch.setattr("clio_agent.arc.clio_core_retry.time.sleep", lambda _s: None)
    tag = _NovelErrorTag(failures=2, message=message)

    put_blob_with_retry(tag, "transcript-atom", b"payload")

    assert tag.calls == 3
    assert last_lost_put_write() is None
    assert probe_clio_core_write_health(env={"CLIO_ARC_STORE": "cte"}) == []


def test_exhausted_retry_reports_a_lost_write_not_a_permanent_latch(
    caplog: "pytest.LogCaptureFixture", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("clio_agent.arc.clio_core_retry.time.sleep", lambda _s: None)
    tag = _NovelErrorTag(failures=99, message="PutBlob operation failed (rc=13)")
    with caplog.at_level(logging.ERROR, logger="clio_agent.arc.clio_core_retry"):
        with pytest.raises(RuntimeError, match="rc=13"):
            put_blob_with_retry(tag, "transcript-atom", b"exact-payload")

    assert tag.calls == 3  # bounded, and only THEN is the write declared lost
    failure = last_lost_put_write()
    assert failure is not None
    assert failure.reason == CLIO_CORE_PUT_WRITE_LOST
    assert failure.name == "transcript-atom"
    assert failure.payload_bytes == len(b"exact-payload")
    assert any(
        CLIO_CORE_PUT_WRITE_LOST in record.getMessage()
        and "payload_bytes=13" in record.getMessage()
        for record in caplog.records
    )

    rows = probe_clio_core_write_health(env={"CLIO_ARC_STORE": "cte"})
    assert len(rows) == 1
    assert rows[0].name == "clio_core_write"
    assert rows[0].state is IntegrationState.UNAVAILABLE
    assert rows[0].required is True
    assert rows[0].details["payload_bytes"] == 13


def test_a_successful_write_clears_write_health_with_a_typed_recovery(
    caplog: "pytest.LogCaptureFixture", monkeypatch: pytest.MonkeyPatch
) -> None:
    """One recoverable hiccup must not pin /v1/health at 503 for process life."""

    monkeypatch.setattr("clio_agent.arc.clio_core_retry.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        put_blob_with_retry(
            _NovelErrorTag(failures=99, message="PutBlob operation failed (rc=13)"),
            "transcript-atom",
            b"exact-payload",
        )
    assert probe_clio_core_write_health(env={"CLIO_ARC_STORE": "cte"}) != []

    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_retry"):
        put_blob_with_retry(_NovelErrorTag(failures=0, message="unused"), "next-atom", b"ok")

    assert last_lost_put_write() is None
    assert probe_clio_core_write_health(env={"CLIO_ARC_STORE": "cte"}) == []
    # The recovery is typed and loud — the lost write is never silently erased.
    recoveries = [r for r in caplog.records if CLIO_CORE_PUT_WRITE_RECOVERED in r.getMessage()]
    assert len(recoveries) == 1
    assert "transcript-atom" in recoveries[0].getMessage()


def test_write_health_state_is_consistent_under_concurrent_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losses and recoveries race across ARC writer threads; state never tears."""

    monkeypatch.setattr("clio_agent.arc.clio_core_retry.time.sleep", lambda _s: None)
    errors: list[BaseException] = []

    def _loser() -> None:
        try:
            with pytest.raises(RuntimeError):
                put_blob_with_retry(
                    _NovelErrorTag(failures=99, message="boom"), "lost-atom", b"payload"
                )
        except BaseException as exc:  # noqa: BLE001 - surfaced to the assertion below
            errors.append(exc)

    def _winner() -> None:
        try:
            put_blob_with_retry(_NovelErrorTag(failures=0, message="unused"), "ok-atom", b"payload")
        except BaseException as exc:  # noqa: BLE001 - surfaced to the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_loser if index % 2 else _winner) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    observed = last_lost_put_write()
    # Either lane may have run last, but the record is always whole, never torn.
    assert observed is None or (
        observed.name == "lost-atom" and observed.reason == CLIO_CORE_PUT_WRITE_LOST
    )
    # A final uncontended success always clears it.
    put_blob_with_retry(_NovelErrorTag(failures=0, message="unused"), "final-atom", b"payload")
    assert last_lost_put_write() is None


def test_write_health_is_absent_before_a_failure() -> None:
    assert probe_clio_core_write_health(env={"CLIO_ARC_STORE": "cte"}) == []
    assert probe_clio_core_write_health(env={"CLIO_ARC_STORE": "local"}) == []
