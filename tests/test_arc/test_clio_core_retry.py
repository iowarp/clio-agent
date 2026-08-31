"""Bounded PutBlob retry (#893 live-gate finding).

The gate's Seattle leg lost a turn to a transient clio-core write refusal
(PutBlob rc=13 with the tier under its cap; separately a container-restore
gap after a daemon restart). The store now retries idempotent re-puts a
bounded number of times, loudly, then re-raises — these tests pin exactly
that: recovery on transient failure, typed warnings per attempt, and the
final failure surfacing unmodified.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from clio_agent.arc.clio_core_retry import (
    CLIO_CORE_PUT_PERMANENT_FAILURE,
    CLIO_CORE_PUT_RETRY,
    last_permanent_put_failure,
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


class _PermanentFailureTag:
    """PutBlob always fails with a non-retriable physical-write error."""

    def __init__(self) -> None:
        self.calls = 0

    def PutBlob(self, name: str, payload: bytes, flags: int) -> None:  # noqa: N802
        self.calls += 1
        raise RuntimeError("PutBlob operation failed (rc=21)")


@pytest.fixture(autouse=True)
def _reset_permanent_failure_latch() -> Iterator[None]:
    import clio_agent.arc.clio_core_retry as retry

    retry._reset_permanent_put_failure_for_tests()
    yield
    retry._reset_permanent_put_failure_for_tests()


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


def test_permanent_rc21_fails_once_and_marks_health_unavailable(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    tag = _PermanentFailureTag()
    with caplog.at_level(logging.ERROR, logger="clio_agent.arc.clio_core_retry"):
        with pytest.raises(RuntimeError, match="rc=21"):
            put_blob_with_retry(tag, "transcript-atom", b"exact-payload")

    assert tag.calls == 1
    failure = last_permanent_put_failure()
    assert failure is not None
    assert failure.reason == CLIO_CORE_PUT_PERMANENT_FAILURE
    assert failure.name == "transcript-atom"
    assert failure.payload_bytes == len(b"exact-payload")
    assert any(
        CLIO_CORE_PUT_PERMANENT_FAILURE in record.getMessage()
        and "payload_bytes=13" in record.getMessage()
        for record in caplog.records
    )

    rows = probe_clio_core_write_health(env={"CLIO_ARC_STORE": "cte"})
    assert len(rows) == 1
    assert rows[0].name == "clio_core_write"
    assert rows[0].state is IntegrationState.UNAVAILABLE
    assert rows[0].required is True
    assert rows[0].details["payload_bytes"] == 13


def test_permanent_write_health_is_absent_before_a_failure() -> None:
    assert probe_clio_core_write_health(env={"CLIO_ARC_STORE": "cte"}) == []
    assert probe_clio_core_write_health(env={"CLIO_ARC_STORE": "local"}) == []
