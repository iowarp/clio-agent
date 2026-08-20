"""Unit tests for the bounded resident message-ledger set (#889).

These exercise :class:`ResidentLedgerSet` against a fake store (and a real
:class:`MessageStore`) so every residency mechanic — lazy rehydration, the LRU
count cap, the byte cap, idle-TTL eviction, the active-session pin, and the typed
audit reasons — is asserted on the real object, sabotage-verifiable in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from clio_agent.gact.messages import LedgerReadError, MessageStore
from clio_agent.gact.resident_ledgers import (
    ResidentLedgerConfig,
    ResidentLedgerSet,
    _estimate_bytes,
    resident_ledger_reason_payload,
    seed_metrics_counters,
)
from clio_agent.gact.types import Message, Part


def _msg(mid: str, sid: str, *, role: str = "user", text: str = "hi") -> Message:
    return Message(
        id=mid,
        session_id=sid,
        role=role,
        created_at="t",
        updated_at="t",
        parts=[Part(id=f"{mid}_p0", type="text", text=text)],
    )


class _FakeStore:
    """In-memory stand-in for MessageStore's read surface + a read counter."""

    def __init__(self, data: Optional[dict[str, list[Message]]] = None) -> None:
        self._data: dict[str, list[Message]] = data or {}
        self.load_calls = 0

    def load_session(self, sid: str) -> Optional[list[Message]]:
        self.load_calls += 1
        rows = self._data.get(sid)
        return list(rows) if rows is not None else None

    def session_ids(self) -> list[str]:
        return list(self._data.keys())

    def has_session(self, sid: str) -> bool:
        return sid in self._data

    def iter_session_ledgers(self):
        for sid, rows in self._data.items():
            yield sid, list(rows)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- #
# Lazy rehydration
# --------------------------------------------------------------------------- #


def test_lazy_rehydration_on_first_access() -> None:
    store = _FakeStore({"s1": [_msg("m1", "s1")]})
    audit: list[dict] = []
    rl = ResidentLedgerSet(store, audit=audit.append)

    # Nothing resident until touched.
    assert rl.resident_count == 0
    assert store.load_calls == 0

    rows = rl.get("s1", [])
    assert [m.id for m in rows] == ["m1"]
    assert rl.resident_count == 1
    assert store.load_calls == 1  # sabotage: break load_session -> this read goes empty
    # A rehydration emits a typed audit reason (no silent fallback).
    assert any(a["reason"] == "rehydrate" and a["session_id"] == "s1" for a in audit)

    # Second access is a resident hit — no extra disk read.
    rl.get("s1", [])
    assert store.load_calls == 1


def test_missing_session_returns_default_without_residency() -> None:
    store = _FakeStore({})
    rl = ResidentLedgerSet(store)
    assert rl.get("nope", []) == []
    assert rl.resident_count == 0
    with pytest.raises(KeyError):
        _ = rl["nope"]


def test_setdefault_append_mutates_resident_copy() -> None:
    # The dict idiom every writer uses: setdefault(sid, []).append(msg) must return
    # the *resident* list so the append lands in the cached copy.
    store = _FakeStore({})
    rl = ResidentLedgerSet(store)
    rl.setdefault("s1", []).append(_msg("m1", "s1"))
    assert [m.id for m in rl["s1"]] == ["m1"]

    store2 = _FakeStore({"s1": [_msg("m1", "s1")]})
    rl2 = ResidentLedgerSet(store2)
    rl2.setdefault("s1", []).append(_msg("m2", "s1"))  # rehydrates then appends
    assert [m.id for m in rl2["s1"]] == ["m1", "m2"]


def test_iteration_and_len_span_the_whole_index() -> None:
    # list()/clear()/delete-by-id must see on-disk sessions that were never touched.
    store = _FakeStore({"a": [_msg("m", "a")], "b": [_msg("m", "b")]})
    rl = ResidentLedgerSet(store)
    rl.get("a", [])  # only 'a' resident
    assert set(rl) == {"a", "b"}
    assert len(rl) == 2
    assert "b" in rl  # __contains__ never forces a body load
    assert rl.resident_count == 1


# --------------------------------------------------------------------------- #
# Caps + eviction
# --------------------------------------------------------------------------- #


def test_count_cap_evicts_oldest_idle_with_typed_reason() -> None:
    store = _FakeStore({f"s{i}": [_msg(f"m{i}", f"s{i}")] for i in range(4)})
    audit: list[dict] = []
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=10**12, max_sessions=2, idle_ttl_s=10**9),
        audit=audit.append,
    )
    rl.get("s0", [])
    rl.get("s1", [])
    rl.get("s2", [])  # over count cap -> evict oldest idle (s0)
    assert rl.resident_session_ids == ["s1", "s2"]
    assert any(a["reason"] == "capacity_count" and a["session_id"] == "s0" for a in audit)

    # Sabotage guard: the evicted session rehydrates byte-identically on next read.
    assert [m.id for m in rl.get("s0", [])] == ["m0"]


def test_byte_cap_evicts_until_under_budget() -> None:
    # Each ledger ~ 1000 chars + 256 overhead. Cap at ~1 ledger.
    store = _FakeStore({f"s{i}": [_msg(f"m{i}", f"s{i}", text="x" * 1000)] for i in range(3)})
    audit: list[dict] = []
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=1500, max_sessions=10**6, idle_ttl_s=10**9),
        audit=audit.append,
    )
    rl.get("s0", [])
    rl.get("s1", [])
    rl.get("s2", [])
    # Byte cap keeps only the newest (each ledger > 1000 bytes, cap 1500).
    assert rl.resident_count == 1
    assert rl.resident_session_ids == ["s2"]
    assert any(a["reason"] == "capacity_bytes" for a in audit)


def test_idle_ttl_evicts_stale_ledger() -> None:
    store = _FakeStore({"old": [_msg("m", "old")], "new": [_msg("m", "new")]})
    audit: list[dict] = []
    clock = _Clock()
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=10**12, max_sessions=10**6, idle_ttl_s=100.0),
        audit=audit.append,
        clock=clock,
    )
    rl.get("old", [])
    clock.now += 500.0  # 'old' now idle past the TTL
    rl.get("new", [])  # any install triggers the idle sweep
    assert rl.resident_session_ids == ["new"]
    assert any(a["reason"] == "idle_ttl" and a["session_id"] == "old" for a in audit)


# --------------------------------------------------------------------------- #
# Active-session pin
# --------------------------------------------------------------------------- #


def test_active_session_is_never_evicted() -> None:
    store = _FakeStore({f"s{i}": [_msg(f"m{i}", f"s{i}")] for i in range(4)})
    audit: list[dict] = []
    active = {"s0"}
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=10**12, max_sessions=2, idle_ttl_s=10**9),
        is_active=lambda sid: sid in active,
        audit=audit.append,
    )
    rl.get("s0", [])  # active -> pinned
    rl.get("s1", [])
    rl.get("s2", [])  # over cap: s0 is pinned, so s1 (oldest idle) is evicted instead
    rl.get("s3", [])
    assert "s0" in rl.resident_session_ids  # sabotage: unbound the pin -> s0 evicts, red
    # s0 was never an eviction victim.
    assert not any(a["session_id"] == "s0" and a["reason"].startswith("capacity") for a in audit)


def test_active_session_survives_idle_ttl() -> None:
    store = _FakeStore({"s0": [_msg("m", "s0")], "s1": [_msg("m", "s1")]})
    clock = _Clock()
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=10**12, max_sessions=10**6, idle_ttl_s=100.0),
        is_active=lambda sid: sid == "s0",
        clock=clock,
    )
    rl.get("s0", [])
    clock.now += 10_000.0  # way past TTL
    rl.get("s1", [])  # triggers sweep; s0 is active so it stays
    assert "s0" in rl.resident_session_ids


def test_all_active_over_cap_skips_eviction_with_typed_reason() -> None:
    store = _FakeStore({f"s{i}": [_msg(f"m{i}", f"s{i}")] for i in range(3)})
    audit: list[dict] = []
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=10**12, max_sessions=1, idle_ttl_s=10**9),
        is_active=lambda _sid: True,  # everything pinned
        audit=audit.append,
    )
    rl.get("s0", [])
    rl.get("s1", [])  # over cap but all active -> soft cap, no eviction
    assert set(rl.resident_session_ids) == {"s0", "s1"}
    assert any(a["reason"] == "eviction_skipped_all_active" for a in audit)


# --------------------------------------------------------------------------- #
# Reason catalog + metrics seed
# --------------------------------------------------------------------------- #


def test_unknown_reason_is_rejected() -> None:
    with pytest.raises(ValueError):
        resident_ledger_reason_payload("not_a_reason", session_id="s")


def test_seed_metrics_folds_every_session_without_residency() -> None:
    from clio_agent.gact.metrics_counters import MetricsCounters

    store = _FakeStore(
        {
            "a": [_msg("m1", "a", role="user"), _msg("m2", "a", role="assistant")],
            "b": [_msg("m3", "b", role="user")],
        }
    )
    counters = MetricsCounters()
    seed_metrics_counters(store, counters)
    assert counters.message_total == 3
    assert counters.by_role["user"] == 2
    assert counters.by_role["assistant"] == 1


# --------------------------------------------------------------------------- #
# Against a real MessageStore (write-through + rehydration)
# --------------------------------------------------------------------------- #


def test_real_store_roundtrip_and_rehydration(tmp_path: Path) -> None:
    store = MessageStore(path=tmp_path / "messages")
    store.append("s1", _msg("m1", "s1", text="hello world"))
    store.append("s1", _msg("m2", "s1", role="assistant", text="reply"))

    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=10**12, max_sessions=10**6, idle_ttl_s=10**9),
    )
    before = [m.model_dump() for m in rl.get("s1", [])]
    assert [m["id"] for m in before] == ["m1", "m2"]

    # Evict the resident copy (simulates an LRU/TTL drop); disk is authoritative.
    del rl["s1"]
    assert rl.resident_count == 0

    after = [m.model_dump() for m in rl.get("s1", [])]
    assert after == before  # rehydrated byte-identically


# --------------------------------------------------------------------------- #
# Read-FAILURE must not masquerade as an empty ledger (blocker #889/§3.3)
# --------------------------------------------------------------------------- #


def _heavy_tool_message(mid: str, sid: str, *, nested_text: str = "", data: str = "") -> Message:
    """A message whose weight lives OFF ``part.text`` — nested tool_result content
    and/or a base64 image ``data`` payload (the byte-cap blind spots #889 bounds)."""

    parts: list[Part] = []
    if nested_text:
        parts.append(
            Part(
                id=f"{mid}_tr",
                type="tool_result",
                tool_name="hdf5.analyze",
                content=[Part(id=f"{mid}_tr_c", type="text", text=nested_text)],
            )
        )
    if data:
        parts.append(Part(id=f"{mid}_img", type="image", data=data, media_type="image/png"))
    return Message(
        id=mid,
        session_id=sid,
        role="assistant",
        created_at="t",
        updated_at="t",
        parts=parts,
    )


class _RaisingStore(_FakeStore):
    """A store whose ``load_session`` raises ``LedgerReadError`` a set number of times
    (a transient disk/AV blip), then serves the persisted rows normally."""

    def __init__(self, data, *, fail_times: int) -> None:  # type: ignore[no-untyped-def]
        super().__init__(data)
        self._fail_times = fail_times

    def load_session(self, sid: str):  # type: ignore[no-untyped-def]
        if self._fail_times > 0:
            self._fail_times -= 1
            raise LedgerReadError(f"simulated transient read failure for {sid}")
        return super().load_session(sid)


def test_load_file_raises_on_corrupt_json(tmp_path: Path) -> None:
    # The root: a corrupt file must raise, NOT return [] (which the resident set
    # would install as an empty transcript). Sabotage: swallow in _load_file -> red.
    store = MessageStore(path=tmp_path / "messages")
    store.append("s1", _msg("m1", "s1", text="real content"))
    sess_file = tmp_path / "messages" / "s1.json"
    sess_file.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(LedgerReadError):
        store.load_session("s1")


def test_rehydrate_failure_propagates_and_is_not_cached() -> None:
    # A transient read failure must (a) emit a typed rehydrate_failed reason,
    # (b) cache NOTHING, (c) let the SECOND read serve the full ledger.
    store = _RaisingStore({"s1": [_msg("m1", "s1"), _msg("m2", "s1")]}, fail_times=1)
    audit: list[dict] = []
    rl = ResidentLedgerSet(store, audit=audit.append)

    with pytest.raises(LedgerReadError):
        rl["s1"]
    # (a) typed reason emitted; (b) nothing cached (no silent empty install).
    assert any(a["reason"] == "rehydrate_failed" and a["session_id"] == "s1" for a in audit)
    assert rl.resident_count == 0
    assert not any(a["reason"] == "rehydrate" for a in audit)

    # (c) the transient fault cleared -> the full ledger rehydrates byte-identically.
    rows = rl.get("s1", [])
    assert [m.id for m in rows] == ["m1", "m2"]


def test_get_propagates_read_failure_instead_of_serving_empty() -> None:
    # .get(sid, default) must NOT swallow a read failure into the default — that is
    # the exact reload!=live divergence the blocker describes.
    store = _RaisingStore({"s1": [_msg("m1", "s1")]}, fail_times=1)
    rl = ResidentLedgerSet(store)
    with pytest.raises(LedgerReadError):
        rl.get("s1", [])


# --------------------------------------------------------------------------- #
# Byte cap sees the HEAVY off-text payloads (finding: _estimate_bytes blind spot)
# --------------------------------------------------------------------------- #


def test_estimate_bytes_counts_nested_and_image_payloads() -> None:
    light = [_msg("m", "s", text="hi")]
    nested = [_heavy_tool_message("m", "s", nested_text="Z" * 1_000_000)]
    image = [_heavy_tool_message("m", "s", data="B" * 1_000_000)]
    light_bytes = _estimate_bytes(light)
    # The heavy off-text payloads dominate; the old text-only estimate scored them
    # at ~256 bytes. Sabotage: revert _estimate_bytes to text-only -> these go tiny.
    assert _estimate_bytes(nested) > 900_000
    assert _estimate_bytes(image) > 900_000
    # Additive Part fields grow the light fixture's serialized floor: P2.11's
    # three empty fields moved it 960 -> 1,035; P2.14's four background-exit
    # fields (#1131) -> 1,111; #1190's ``structured_content`` -> 1,137. Three more
    # landed features each added their own Part fields: #1188 (285434f5)
    # ``content_blocks``; the document-artifacts merge (3347d283, DocumentPartFields
    # mixin -- review_id/artifact_id/artifact_version/artifact_sha256/review_text/
    # anchor); and the action_card part (52387c2e, source/severity/title/body/
    # actions) -> 1,322. It remains three orders of magnitude lighter than either
    # heavy payload.
    assert 1_100 <= light_bytes < 1_400


def test_byte_cap_evicts_tool_result_heavy_sessions() -> None:
    # Sessions whose weight is nested tool_result content (the product's main
    # workload) must count against the byte cap. Old text-only estimate: they were
    # exempt (each ~256 bytes) and nothing ever evicted.
    store = _FakeStore(
        {
            f"s{i}": [_heavy_tool_message(f"m{i}", f"s{i}", nested_text="Q" * 1_000_000)]
            for i in range(3)
        }
    )
    audit: list[dict] = []
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=1_500_000, max_sessions=10**6, idle_ttl_s=10**9),
        audit=audit.append,
    )
    rl.get("s0", [])
    rl.get("s1", [])
    rl.get("s2", [])
    assert rl.resident_count == 1  # ~1MB each, cap ~1.5MB -> only newest resident
    assert any(a["reason"] == "capacity_bytes" for a in audit)


# --------------------------------------------------------------------------- #
# Incremental byte accounting — no O(N*M) re-walk per cache miss (perf finding)
# --------------------------------------------------------------------------- #


def test_byte_accounting_is_incremental_not_full_rewalk(monkeypatch) -> None:
    import clio_agent.gact.resident_ledgers as rl_mod

    calls = {"n": 0}
    real = rl_mod._estimate_bytes

    def counting(messages):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real(messages)

    monkeypatch.setattr(rl_mod, "_estimate_bytes", counting)

    store = _FakeStore({f"s{i}": [_msg(f"m{i}", f"s{i}")] for i in range(50)})
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=10**12, max_sessions=10**6, idle_ttl_s=10**9),
    )
    for i in range(50):
        rl.get(f"s{i}", [])
    # 50 installs with up to 50 resident: incremental accounting measures each entry
    # ~once (only length-changed entries re-measure). A full re-walk on every install
    # would be O(N*M) ~ 1000s of calls. Sabotage: make _enforce_caps call the full
    # _recompute_bytes and this crosses the bound.
    assert calls["n"] <= 100  # O(N), not O(N*M)


# --------------------------------------------------------------------------- #
# Delete does not rehydrate; clear terminates
# --------------------------------------------------------------------------- #


def test_discard_drops_without_materializing() -> None:
    store = _FakeStore({"big": [_msg(f"m{i}", "big") for i in range(100)]})
    audit: list[dict] = []
    rl = ResidentLedgerSet(store, audit=audit.append)
    # 'big' is NOT resident. discard must not read disk and must emit no rehydrate row.
    rl.discard("big")
    assert store.load_calls == 0  # sabotage: route delete through pop() -> load_calls == 1
    assert not any(a["reason"] == "rehydrate" for a in audit)
    assert rl.resident_count == 0


def test_delete_of_evicted_session_does_not_flush_warm_neighbor() -> None:
    # Deleting a big evicted session must not materialize it and evict a warm one.
    store = _FakeStore(
        {
            "small": [_msg("s", "small")],
            "big": [_heavy_tool_message("b", "big", nested_text="Y" * 1_000_000)],
        }
    )
    audit: list[dict] = []
    rl = ResidentLedgerSet(
        store,
        config=ResidentLedgerConfig(max_bytes=1_500_000, max_sessions=10**6, idle_ttl_s=10**9),
        audit=audit.append,
    )
    rl.get("small", [])  # 'small' resident; 'big' never touched
    rl.discard("big")  # delete-precursor for the non-resident big session
    assert store.load_calls == 1  # only the 'small' get; 'big' was never read
    assert "small" in rl.resident_session_ids  # warm neighbor survives
    assert not any(a["reason"] == "capacity_bytes" for a in audit)


def test_clear_terminates_and_is_memory_only() -> None:
    # clear() must not infinite-loop when sessions are persisted on disk, and it
    # releases the cache (memory) only — the durable index survives.
    store = _FakeStore({"a": [_msg("m", "a")], "b": [_msg("m", "b")]})
    rl = ResidentLedgerSet(store)
    rl.get("a", [])
    rl.get("b", [])
    assert rl.resident_count == 2
    rl.clear()  # sabotage: fall back to the mixin clear() -> spins forever on the disk index
    assert rl.resident_count == 0
    assert set(rl) == {"a", "b"}  # durable index untouched; rehydrates on next access
    assert [m.id for m in rl.get("a", [])] == ["m"]


def test_popitem_is_memory_only_and_terminates() -> None:
    store = _FakeStore({"a": [_msg("m", "a")]})
    rl = ResidentLedgerSet(store)
    with pytest.raises(KeyError):
        rl.popitem()  # nothing resident
    rl.get("a", [])
    sid, rows = rl.popitem()
    assert sid == "a"
    assert [m.id for m in rows] == ["m"]
    assert rl.resident_count == 0


# --------------------------------------------------------------------------- #
# Config coherence — invalid values fall back to defaults, never crash boot
# --------------------------------------------------------------------------- #


def test_config_non_numeric_env_falls_back_not_crash(monkeypatch) -> None:
    import clio_agent.conf as conf

    conf.reload()
    monkeypatch.setenv("CLIO_RESIDENT_LEDGERS_MAX_BYTES", "lots")
    cfg = ResidentLedgerConfig.from_conf()  # sabotage: cast in from_conf without guard -> raises
    assert cfg.max_bytes == ResidentLedgerConfig.max_bytes


def test_config_out_of_domain_env_falls_back(monkeypatch) -> None:
    import clio_agent.conf as conf

    conf.reload()
    monkeypatch.setenv("CLIO_RESIDENT_LEDGERS_MAX", "-5")
    monkeypatch.setenv("CLIO_RESIDENT_LEDGERS_TTL_S", "0")
    cfg = ResidentLedgerConfig.from_conf()
    assert cfg.max_sessions == ResidentLedgerConfig.max_sessions
    assert cfg.idle_ttl_s == ResidentLedgerConfig.idle_ttl_s


def test_config_valid_env_is_honored(monkeypatch) -> None:
    import clio_agent.conf as conf

    conf.reload()
    monkeypatch.setenv("CLIO_RESIDENT_LEDGERS_MAX", "7")
    cfg = ResidentLedgerConfig.from_conf()
    assert cfg.max_sessions == 7
