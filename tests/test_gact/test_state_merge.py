"""#737 S6 — workflow_state is the recorded RESULT of a ``state_merge`` op.

These prove the design §2.8.d invariant: a delegated turn's ``workflow_state`` is the
recorded result of a ``state_merge`` op and is materialized onto the transcript
**schema-free** — NEVER re-folded on read under a newer pack schema.

Covered:

* build/record/load roundtrip of the op (``{inputs, produced, schema_version}``).
* **the schema_version replay test** (design (d)): a recorded result replays
  byte-identically even when the CURRENT pack schema differs — record under schema A,
  make schema B the live one, assert the served bytes stay A's; and prove a re-fold of
  the same inputs under B WOULD diverge (so the schema-free read is load-bearing).
* verbatim fallback when no op was recorded (pre-S6 / lifecycle-erased ledgers).
* the op lane drops with the transcript projection, never touching ARC memory.
* best-effort-but-loud recording (§3.4): a broken store logs a typed reason, no raise.

SABOTAGE (recorded, run manually):

* (a) re-fold on read under the mutated schema — in
  :func:`materialize_state_merge_projection` / :func:`resolve_row_workflow_state`,
  replace the recorded-result lookup with
  ``_workflow_state_from_handoff_rows([row], schema=<live schema>)``:
  ``test_replay_is_schema_free_under_mutated_schema`` goes RED (served bytes become
  B's re-fold, not A's recorded result). Restore the schema-free lookup.
* (b) drop ``schema_version`` from :func:`build_state_merge_content`:
  ``test_schema_version_present_on_every_op`` goes RED (the pin is absent). Restore it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.delegation import _workflow_state_from_handoff_rows
from clio_agent.gact.types import Message
from clio_agent.gact.workflow_state.schema import WorkflowSectionRule, WorkflowStateSchema
from clio_agent.gact.workflow_state.state_merge import (
    STATE_MERGE_SCHEMA_VERSION,
    STATE_MERGE_SCOPE,
    build_state_merge_content,
    build_state_merge_entries,
    delegation_scope_key,
    drop_state_merge_lane,
    load_state_merge_results,
    materialize_state_merge_projection,
    record_state_merge,
    record_state_merge_best_effort,
)

# Two pack schemas that rank the SAME "acquisition" carriers differently: under A a
# ``staged`` status outranks ``candidate_found`` (5 > 2); under B it is INVERTED
# (1 < 2). So folding the same inputs yields a different merged section — the exact
# "re-fold under a newer pack schema yields different bytes" hazard design §2.8.d names.
_SCHEMA_A = WorkflowStateSchema(
    sections={"acquisition": WorkflowSectionRule(status_ranks={"staged": 5, "candidate_found": 2})}
)
_SCHEMA_B = WorkflowStateSchema(
    sections={"acquisition": WorkflowSectionRule(status_ranks={"staged": 1, "candidate_found": 2})}
)

# The raw per-tool carriers a delegate row folded into its final workflow_state.
_CARRIER_CANDIDATE = {"acquisition": {"status": "candidate_found", "candidate": "x"}}
_CARRIER_STAGED = {"acquisition": {"status": "staged", "staged_path": "/tmp/y"}}


def _arc(tmp_path: Path) -> ARCMemory:
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _delegated_message(
    *, message_id: str = "msg_asst_1", produced: dict[str, Any], carriers: list[dict[str, Any]]
) -> Message:
    """A persisted assistant message carrying one delegate.completed row.

    ``produced`` is the merged workflow_state the upstream merge site produced (what the
    row carries verbatim); ``carriers`` are the raw ``tools_called[].workflow_state``
    inputs the op records as provenance.
    """

    row: dict[str, Any] = {
        "agent_id": "data",
        "parent_id": "main",
        "stage": "delegate.completed",
        "status": "completed",
        "workflow_state": produced,
        "tools_called": [{"name": "t", "workflow_state": c} for c in carriers],
    }
    return Message(
        id=message_id,
        session_id="sess_s6",
        role="assistant",
        created_at="2026-07-12T00:00:00Z",
        updated_at="2026-07-12T00:00:00Z",
        metadata={"expert_handoffs": [row]},
    )


# --------------------------------------------------------------------------- #
# build / record / load
# --------------------------------------------------------------------------- #


def test_build_entries_captures_produced_and_raw_inputs() -> None:
    produced = _workflow_state_from_handoff_rows(
        [{"tools_called": [{"workflow_state": c} for c in (_CARRIER_CANDIDATE, _CARRIER_STAGED)]}],
        schema=_SCHEMA_A,
    )
    msg = _delegated_message(produced=produced, carriers=[_CARRIER_CANDIDATE, _CARRIER_STAGED])
    entries = build_state_merge_entries(msg)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["scope"] == delegation_scope_key(
        "msg_asst_1", 0, msg.metadata["expert_handoffs"][0]
    )
    assert entry["produced"] == produced  # the RESULT, verbatim
    # inputs are the RAW carriers (candidate + staged), un-merged
    assert _CARRIER_CANDIDATE in entry["inputs"] and _CARRIER_STAGED in entry["inputs"]


def test_no_handoff_workflow_state_records_nothing(tmp_path: Path) -> None:
    plain = Message(
        id="msg_asst_plain",
        session_id="sess_s6",
        role="assistant",
        created_at="2026-07-12T00:00:00Z",
        updated_at="2026-07-12T00:00:00Z",
        metadata={},
    )
    assert build_state_merge_content(plain) is None
    arc = _arc(tmp_path)
    assert record_state_merge(arc, "sess_s6", plain) is None
    assert load_state_merge_results(arc, "sess_s6") == {}


def test_record_and_load_roundtrip_last_wins(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    produced_a = _workflow_state_from_handoff_rows(
        [{"tools_called": [{"workflow_state": _CARRIER_STAGED}]}], schema=_SCHEMA_A
    )
    msg = _delegated_message(produced=produced_a, carriers=[_CARRIER_STAGED])
    record_state_merge(arc, "sess_s6", msg)
    key = delegation_scope_key("msg_asst_1", 0, msg.metadata["expert_handoffs"][0])
    assert load_state_merge_results(arc, "sess_s6") == {key: produced_a}

    # A later op for the SAME scope supersedes (re-materialisation after undo/compact).
    produced_b = {"acquisition": {"status": "candidate_found"}}
    msg2 = _delegated_message(produced=produced_b, carriers=[_CARRIER_CANDIDATE])
    record_state_merge(arc, "sess_s6", msg2)
    assert load_state_merge_results(arc, "sess_s6")[key] == produced_b


# --------------------------------------------------------------------------- #
# (b) schema_version presence pin
# --------------------------------------------------------------------------- #


def test_schema_version_present_on_every_op() -> None:
    """SABOTAGE (b): dropping schema_version from the record turns this RED."""

    msg = _delegated_message(
        produced={"acquisition": {"status": "staged"}}, carriers=[_CARRIER_STAGED]
    )
    content = build_state_merge_content(msg)
    assert content is not None
    assert content["schema_version"] == STATE_MERGE_SCHEMA_VERSION
    assert content["op"] == "state_merge"


# --------------------------------------------------------------------------- #
# (d) the schema_version replay test — the CORE of the slice
# --------------------------------------------------------------------------- #


def test_replay_is_schema_free_under_mutated_schema(tmp_path: Path) -> None:
    """Record under schema A; make B the live schema; served bytes STAY A's.

    Proves design §2.8.d: the materialized workflow_state is the recorded RESULT, never
    a re-fold. It also proves the schema-free read is LOAD-BEARING by showing a re-fold
    of the SAME inputs under the mutated schema B WOULD diverge.
    """

    arc = _arc(tmp_path)
    carriers = [_CARRIER_CANDIDATE, _CARRIER_STAGED]

    # produced under schema A: staged (rank 5) wins the acquisition section.
    produced_a = _workflow_state_from_handoff_rows(
        [{"tools_called": [{"workflow_state": c} for c in carriers]}], schema=_SCHEMA_A
    )
    assert produced_a["acquisition"]["status"] == "staged"

    msg = _delegated_message(produced=produced_a, carriers=carriers)
    record_state_merge(arc, "sess_s6", msg)

    # A re-fold of the SAME inputs under the MUTATED live schema B diverges (candidate
    # now outranks staged) — this is exactly what the read path must NOT do.
    refold_under_b = _workflow_state_from_handoff_rows(
        [{"tools_called": [{"workflow_state": c} for c in carriers]}], schema=_SCHEMA_B
    )
    assert refold_under_b != produced_a
    assert refold_under_b["acquisition"]["status"] == "candidate_found"

    # The projection materializes the RECORDED result — schema-free — so even with B as
    # the live schema the served row keeps A's bytes.
    reloaded = [Message(**msg.model_dump(exclude_none=True))]
    materialize_state_merge_projection(arc, "sess_s6", reloaded)
    served_ws = reloaded[0].metadata["expert_handoffs"][0]["workflow_state"]
    assert served_ws == produced_a, "served workflow_state was re-folded (sabotage a) not replayed"
    assert served_ws != refold_under_b


def test_materialize_verbatim_fallback_when_no_op(tmp_path: Path) -> None:
    """No op recorded (pre-S6 / erased ledger): rows keep their verbatim value, no fold."""

    arc = _arc(tmp_path)
    verbatim = {"acquisition": {"status": "staged", "staged_path": "/tmp/y"}}
    msg = _delegated_message(produced=verbatim, carriers=[_CARRIER_STAGED])
    messages = [Message(**msg.model_dump(exclude_none=True))]
    # No record_state_merge call — the op lane is empty.
    materialize_state_merge_projection(arc, "sess_s6", messages)
    assert messages[0].metadata["expert_handoffs"][0]["workflow_state"] == verbatim


# --------------------------------------------------------------------------- #
# lane lifecycle + no-silent-fallback
# --------------------------------------------------------------------------- #


def test_drop_lane_erases_ops(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    msg = _delegated_message(
        produced={"acquisition": {"status": "staged"}}, carriers=[_CARRIER_STAGED]
    )
    record_state_merge(arc, "sess_s6", msg)
    assert load_state_merge_results(arc, "sess_s6")
    drop_state_merge_lane(arc, "sess_s6")
    assert load_state_merge_results(arc, "sess_s6") == {}
    # The op lane is a dedicated _events partition — dropping it never reaches memory.
    assert STATE_MERGE_SCOPE.startswith("_events/")


def test_local_fixtures_carry_no_schema_version_and_materialize_schema_free(tmp_path: Path) -> None:
    """The plan's open question, answered empirically on the committed corpus.

    Finding (measured on the full local ~60-ledger store AND the committed redacted
    corpus): real ``metadata.expert_handoffs[].workflow_state`` carriers exist but carry
    NO ``schema_version`` anywhere — they predate S6. Because no ``state_merge`` op exists
    for them, :func:`materialize_state_merge_projection` reads them VERBATIM (schema-free:
    no ``normalize_section`` / ``rank`` touch), so a pre-S6 ledger reloads byte-identically
    under any live pack schema.
    """

    from tests.equivalence import corpus as C

    ledgers = C.committed_corpus()
    assert ledgers, "committed redacted corpus is empty"

    def _find_schema_version(node: Any) -> bool:
        if isinstance(node, dict):
            return "schema_version" in node or any(_find_schema_version(v) for v in node.values())
        if isinstance(node, list):
            return any(_find_schema_version(v) for v in node)
        return False

    rows_with_ws = 0
    for _sid, rows in ledgers:
        for m in rows:
            assert not _find_schema_version(m), (
                "a pre-S6 fixture unexpectedly carries schema_version"
            )
            for r in (m.get("metadata") or {}).get("expert_handoffs", []) or []:
                if isinstance(r.get("workflow_state"), dict):
                    rows_with_ws += 1
    assert rows_with_ws > 0, "corpus has no workflow_state carriers to exercise"

    # A corpus-derived delegated message, with NO op recorded, materializes verbatim.
    arc = _arc(tmp_path)
    for _sid, rows in ledgers:
        for m in rows:
            handoffs = (m.get("metadata") or {}).get("expert_handoffs", []) or []
            if not any(isinstance(r.get("workflow_state"), dict) for r in handoffs):
                continue
            before = [dict(r) for r in handoffs]
            msg = Message(
                id=str(m.get("id") or "msg"),
                session_id="sess_corpus",
                role="assistant",
                created_at=str(m.get("created_at") or "2026-01-01T00:00:00Z"),
                updated_at=str(m.get("updated_at") or "2026-01-01T00:00:00Z"),
                metadata={"expert_handoffs": [dict(r) for r in handoffs]},
            )
            materialize_state_merge_projection(arc, "sess_corpus", [msg])
            assert msg.metadata["expert_handoffs"] == before  # verbatim, schema-free
            return


def test_record_best_effort_is_loud_but_non_fatal(caplog: Any) -> None:
    """SABOTAGE-safe: a broken store logs the typed reason and does NOT raise (§3.4)."""

    class _BrokenArc:
        @property
        def _segments(self) -> Any:
            raise RuntimeError("store down")

    msg = _delegated_message(
        produced={"acquisition": {"status": "staged"}}, carriers=[_CARRIER_STAGED]
    )
    with caplog.at_level("ERROR"):
        record_state_merge_best_effort(_BrokenArc(), "sess_s6", msg)  # must not raise
    assert any("state_merge_record_failed" in r.message for r in caplog.records)
