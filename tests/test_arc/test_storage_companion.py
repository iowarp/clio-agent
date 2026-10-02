"""#1334 RPC diet: a ``segments`` put under the reserved ``_events`` family is ONE native
call; every other put keeps the stale-companion probe (or the companion put).

Hermetic: a recording fake ``clio_cte_core_ext`` under a real ``ClioCoreStore`` (the
``test_rpc_liveness`` construction), so the assertion is on the exact native call
sequence the daemon would see.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clio_agent import conf
from clio_agent.arc.clio_core_async_ops import TagIds
from clio_agent.arc.clio_core_liveness import LivenessGate
from clio_agent.arc.companion_policy import (
    NEVER_INDEXED_SCOPE_PREFIX,
    SEGMENT_NAME_SEP,
    may_carry_companion,
)
from clio_agent.arc.lane_chunking import chunk_for_append, chunk_scope
from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.segments import SegmentStore
from clio_agent.arc.storage import ClioCoreStore

# clio-core is the only ARC store: each backing ``path`` a test used to hand a local
# store maps to its own namespace under this test's namespace on the worker's private
# daemon, so a "cold store over the same dir" reopens the same records.
_OPENED_STORES: list = []


def _clio_core(path: object) -> object:
    """The real clio-core ARC store for ``path`` (namespaced per test and path)."""
    import hashlib  # noqa: PLC0415

    from clio_agent import conf  # noqa: PLC0415
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    base = conf.resolve("arc.namespace", env="CLIO_ARC_NAMESPACE", default="", cast=conf.as_str)
    suffix = hashlib.sha1(str(path).encode()).hexdigest()[:8]
    store = make_arc_store(backend="cte", namespace=f"{base or 'arc'}-{suffix}")
    _OPENED_STORES.append(store)
    return store


@pytest.fixture(autouse=True)
def _clear_clio_core_namespaces():
    yield
    while _OPENED_STORES:
        _OPENED_STORES.pop().clear()


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


class _Ready:
    def __init__(self, code: int = 0) -> None:
        self._code = code

    def done(self) -> bool:
        return True

    def wait(self, _max_sec: float = -1.0) -> int:
        return self._code


class _Client:
    """Writes and deletes go through the GIL-free async API (``clio_core_async_ops``)."""

    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self._calls = calls

    def AsyncPutBlob(self, _tag_id: int, name: str, _data: Any, _off: int = 0) -> _Ready:  # noqa: N802
        self._calls.append(("PutBlob", name))
        return _Ready(0)

    def AsyncDelBlob(self, _tag_id: int, name: str) -> _Ready:  # noqa: N802
        self._calls.append(("DelBlob", name))
        return _Ready(0)


def _store(cte: _RecordingCte) -> ClioCoreStore:
    store = ClioCoreStore.__new__(ClioCoreStore)
    store._cte = cte
    store._client = _Client(cte.calls)
    store._tag_ids = TagIds(cte)
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


def test_indexed_scope_without_text_drops_any_stale_companion() -> None:
    """One async delete of the companion, whether or not it exists (a missing one is a
    no-op): the blocking ``GetBlobSize`` existence probe (GIL-held) is gone, so a stale
    companion costs 2 RPCs instead of 3 and an absent one still 2."""
    cte = _RecordingCte()
    _store(cte).put("segments", _events_name("agentA"), b"x")
    assert cte.calls == [("PutBlob", "sess__agentA"), ("DelBlob", "sess__agentA.text")]


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
    assert [op for op, _n in cte.calls] == ["PutBlob", "DelBlob"]


# --------------------------------------------------------------------------- #
# #1339 review F4: the live-lane audit evidence -- one append re-puts ONLY the
# active chunk record, never a sibling. Against the REAL clio-core store (not the
# recording fake above): the ``store.put`` audit row this test pins is emitted
# from inside ``ClioCoreStore.put`` itself, so a real
# backend is the honest proof.
# --------------------------------------------------------------------------- #


def test_variant_put_carries_no_store_put_audit_row(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit is ``kind == "segments"`` only -- a non-segment put is silent."""

    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_path))

    _clio_core(str(tmp_path / "fs")).put("variants", "v1", b"x")

    rows = audit_path.read_text().splitlines() if audit_path.exists() else []
    assert not any(json.loads(row)["stage"] == "store.put" for row in rows)


def test_one_append_on_a_full_three_chunk_lane_puts_only_the_active_chunk(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mint a 4th message onto a lane whose first three chunks are already full
    (capacity 1 -- each append rolls to a fresh chunk): the ONE resulting
    ``store.put`` audit row names the newly active (4th) chunk's record, never
    one of the three sibling chunks the earlier appends already sealed."""

    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_path))

    sid = "sess-storage-audit"
    base = "_events/m"
    ss = SegmentStore(_clio_core(str(tmp_path / "arc")))
    for _ in range(3):
        scope = chunk_for_append(ss, sid, base, capacity=1)
        ss.append(sid, scope, "message_part", {})

    before = len(audit_path.read_text().splitlines()) if audit_path.exists() else 0

    active_scope = chunk_for_append(ss, sid, base, capacity=1)
    assert active_scope == chunk_scope(base, 4)
    ss.append(sid, active_scope, "message_part", {})

    rows = audit_path.read_text().splitlines()
    new_rows = [json.loads(row) for row in rows[before:]]
    put_rows = [row for row in new_rows if row["stage"] == "store.put"]
    assert put_rows, new_rows

    active_name = SegmentStore._record_name(sid, active_scope)
    assert all(row["name"] == active_name for row in put_rows)
    assert all(row["kind"] == "segments" for row in put_rows)
    for n in (1, 2, 3):
        sibling_name = SegmentStore._record_name(sid, chunk_scope(base, n))
        assert not any(row["name"] == sibling_name for row in put_rows)
