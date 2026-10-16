"""Structured capacity recovery never broadens retries of unknown or pending writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import clio_core_async_ops as ops
from clio_agent.arc import clio_core_growth as growth
from clio_agent.arc import clio_core_retry as retry


@pytest.fixture(autouse=True)
def retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "write_retry_attempts", lambda: 3)
    monkeypatch.setattr(retry, "write_retry_first_delay_s", lambda: 0)
    retry._reset_put_write_health_for_tests()


@pytest.mark.parametrize("code", [12, 13])
def test_only_structured_capacity_failures_can_expand(code: int) -> None:
    calls: list[bytes] = []
    recovered: list[bool] = []

    class Tag:
        def PutBlob(self, name: str, payload: bytes, offset: int) -> None:  # noqa: N802
            calls.append(payload)
            if len(calls) == 1:
                raise ops.ClioCorePutRefused(name, code)

    retry.put_blob_with_retry(
        Tag(), "same", b"identical", recover_capacity=lambda: recovered.append(True) or True
    )
    assert calls == [b"identical", b"identical"]
    assert recovered == [True]
    assert retry.last_lost_put_write() is None


def test_matching_error_text_never_triggers_growth() -> None:
    recovered: list[bool] = []

    class Tag:
        def PutBlob(self, name: str, payload: bytes, offset: int) -> None:  # noqa: N802
            raise RuntimeError("returned code 12")

    with pytest.raises(RuntimeError, match="returned code 12"):
        retry.put_blob_with_retry(
            Tag(), "same", b"payload", recover_capacity=lambda: recovered.append(True) or True
        )
    assert not recovered
    assert retry.last_lost_put_write() is not None


def test_capacity_failure_stays_loud_if_recovery_is_refused() -> None:
    recovered: list[bool] = []

    class Tag:
        def PutBlob(self, name: str, payload: bytes, offset: int) -> None:  # noqa: N802
            raise ops.ClioCorePutRefused(name, 12)

    with pytest.raises(RuntimeError, match="returned code 12"):
        retry.put_blob_with_retry(
            Tag(), "same", b"payload", recover_capacity=lambda: recovered.append(True) or False
        )
    assert recovered == [True]
    assert retry.last_lost_put_write() is not None


def test_partial_batch_only_retries_the_refused_record(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []
    recovered: list[str] = []

    class Future:
        def __init__(self, code: int) -> None:
            self.code = code

        def done(self) -> bool:
            return True

        def wait(self, timeout: float = 0) -> int:
            return self.code

    class Client:
        def AsyncPutBlob(self, tag: Any, name: str, payload: bytes, off: int) -> Future:  # noqa: N802
            attempts.append(name)
            return Future(12 if name == "refused" and attempts.count(name) == 1 else 0)

    monkeypatch.setattr(
        growth, "recover_capacity", lambda store, tag, name, size: recovered.append(name) or True
    )
    store = SimpleNamespace(_client=Client(), _tag_ids=SimpleNamespace(get=lambda kind: "tag"))
    assert (
        ops.store_put_many(store, "segments", [("stored", b"first"), ("refused", b"second")]) == {}
    )
    assert attempts == ["stored", "refused", "refused"]
    assert recovered == ["refused"]


def test_pending_capacity_rpc_does_not_reissue_the_refused_put() -> None:
    attempts: list[str] = []

    class Tag:
        def PutBlob(self, name: str, payload: bytes, offset: int) -> None:  # noqa: N802
            attempts.append(name)
            raise ops.ClioCorePutRefused(name, 12)

    def pending() -> bool:
        raise TimeoutError("capacity registration is still pending")

    with pytest.raises(TimeoutError, match="still pending"):
        retry.put_blob_with_retry(Tag(), "same", b"payload", recover_capacity=pending)
    assert attempts == ["same"]
    assert retry.last_lost_put_write() is not None
