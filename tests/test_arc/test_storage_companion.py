"""#1334 RPC diet: a ``segments`` put under the reserved ``_events`` family is ONE native
call; every other put keeps the stale-companion probe (or the companion put).

Hermetic: a recording fake ``clio_cte_core_ext`` under a real ``ClioCoreStore`` (the
``test_rpc_liveness`` construction), so the assertion is on the exact native call
sequence the daemon would see.
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc.clio_core_liveness import LivenessGate
from clio_agent.arc.companion_policy import (
    NEVER_INDEXED_SCOPE_PREFIX,
    SEGMENT_NAME_SEP,
    may_carry_companion,
)
from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.segments import SegmentStore
from clio_agent.arc.storage import ClioCoreStore


class _RecordingCte:
    """A fake ``clio_cte_core_ext`` that records every native call."""

    def __init__(self, companion_size: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.companion_size = companion_size

    def Tag(self, kind: str) -> Any:  # noqa: N802 - mirrors the native API
        outer = self

        class _Tag:
            def PutBlob(self, name: str, payload: Any, _flags: int) -> None:  # noqa: N802
                outer.calls.append(("PutBlob", name))

            def GetBlobSize(self, name: str) -> int:  # noqa: N802
                outer.calls.append(("GetBlobSize", name))
                return outer.companion_size

            def GetTagId(self) -> int:  # noqa: N802
                return 1

        return _Tag()


class _Client:
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self._calls = calls

    def DelBlob(self, _tag_id: int, name: str) -> bool:  # noqa: N802
        self._calls.append(("DelBlob", name))
        return True


def _store(cte: _RecordingCte) -> ClioCoreStore:
    store = ClioCoreStore.__new__(ClioCoreStore)
    store._cte = cte
    store._client = _Client(cte.calls)
    store._config_path = ""
    store._log_level = "error"
    store._gate = LivenessGate(config_path="", probe=lambda _p: True, ttl_s=100.0)
    store._reconnect = lambda: None  # type: ignore[method-assign]
    return store


def _events_name(scope: str) -> str:
    return SegmentStore._record_name("sess", scope)


def test_policy_pins_the_reserved_prefix_and_the_name_shape() -> None:
    assert NEVER_INDEXED_SCOPE_PREFIX == EVENTS_SCOPE
    assert _events_name("_events/m") == f"sess{SEGMENT_NAME_SEP}_events~m"
    assert may_carry_companion("segments", _events_name("_events")) is False
    assert may_carry_companion("segments", _events_name("_events/m")) is False
    assert may_carry_companion("segments", _events_name("_events/w/span1")) is False
    assert may_carry_companion("segments", _events_name("agentA")) is True
    assert may_carry_companion("segments", _events_name("_eventsish")) is False  # prefix rule
    assert may_carry_companion("variants", "anything") is True


def test_events_family_put_is_one_native_call() -> None:
    cte = _RecordingCte()
    _store(cte).put("segments", _events_name("_events/m"), b"x")
    assert [op for op, _n in cte.calls] == ["PutBlob"]


def test_indexed_scope_without_text_keeps_the_stale_companion_probe() -> None:
    cte = _RecordingCte(companion_size=0)
    _store(cte).put("segments", _events_name("agentA"), b"x")
    assert [op for op, _n in cte.calls] == ["PutBlob", "GetBlobSize"]
    cte = _RecordingCte(companion_size=12)
    _store(cte).put("segments", _events_name("agentA"), b"x")
    assert [op for op, _n in cte.calls] == ["PutBlob", "GetBlobSize", "DelBlob"]


def test_indexed_scope_with_text_writes_the_companion() -> None:
    cte = _RecordingCte()
    _store(cte).put("segments", _events_name("agentA"), b"x", search_text="hello")
    assert cte.calls == [
        ("PutBlob", "sess__agentA"),
        ("PutBlob", "sess__agentA.text"),
    ]


def test_non_segment_kinds_are_untouched_by_the_policy() -> None:
    cte = _RecordingCte()
    _store(cte).put("variants", "v1", b"x")
    assert [op for op, _n in cte.calls] == ["PutBlob", "GetBlobSize"]
