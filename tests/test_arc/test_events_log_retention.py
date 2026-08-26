"""Regression tests for #762: never erase the ONLY copy of the ``_events`` log.

The erase of the reserved ``_events`` scope on ``release_session`` /
``flush_and_release`` is justified by "the durable trace keeps the full history"
JSONL is default-on now. When downstream persistence is explicitly disabled,
the ``_events`` log is the only copy and must be retained.

The Phase-0 guard: the destructive erase is GATED on the durable trace backend
actually being enabled. Trace disabled -> the log is RETAINED (the hot copy is
still released write-through, losing nothing); trace enabled -> the historical
erase behavior still runs. Either path logs a structured reason — never silent.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent import conf
from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.gact.semantic_events import SemanticEvent
from tests._config_layer import set_config


def _turn_started(sid: str = "s1") -> SemanticEvent:
    return SemanticEvent(
        event_type="turn.started",
        session_id=sid,
        trace_id="trace_t1",
        turn_id="t1",
        occurred_at="2026-06-14T00:00:00+00:00",
        payload={"input": "stations near San Diego"},
    )


@pytest.fixture()
def hermetic_conf(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the process-wide config store at an empty home/cwd so a developer's
    real config file can never set ``trace.backend`` over the test's env."""
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))


def _arc_with_one_event(tmp_path: Any) -> ARCMemory:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.record_semantic_event(_turn_started())
    assert arc.render_segments("s1", EVENTS_SCOPE)  # log holds the persisted event
    return arc


class TestTraceDisabledRetainsEventsLog:
    """Explicitly disabled provenance: the ARC log is the only copy."""

    def test_release_session_retains_events_log(self, tmp_path, monkeypatch, hermetic_conf):
        set_config("provenance.agentic.providers", [])
        arc = _arc_with_one_event(tmp_path)

        result = arc.release_session("s1")

        # The only copy of the event log SURVIVES the release (#762)...
        segs = arc.render_segments("s1", EVENTS_SCOPE)
        assert len(segs) == 1
        assert segs[0].content["event_type"] == "turn.started"
        # ...and nothing was erased from the live projection's substrate.
        assert result["live"] == 0

    def test_release_session_logs_retention_reason(
        self, tmp_path, monkeypatch, hermetic_conf, caplog
    ):
        set_config("trace.backend", "none")  # file-layer (file > env); #985 config-first
        arc = _arc_with_one_event(tmp_path)

        with caplog.at_level("WARNING", logger="clio_agent.arc.memory"):
            arc.release_session("s1")

        assert any("reason=durable_trace_disabled" in r.message for r in caplog.records)

    def test_flush_and_release_retains_events_log(self, tmp_path, monkeypatch, hermetic_conf):
        set_config("provenance.agentic.providers", [])
        arc = _arc_with_one_event(tmp_path)

        arc.flush_and_release()

        segs = arc.render_segments("s1", EVENTS_SCOPE)
        assert len(segs) == 1
        assert segs[0].content["event_type"] == "turn.started"


class TestTraceEnabledStillErases:
    """With a durable file trace enabled, the historical erase behavior runs."""

    def test_release_session_erases_events_log(self, tmp_path, monkeypatch, hermetic_conf):
        set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first
        arc = _arc_with_one_event(tmp_path)

        result = arc.release_session("s1")

        assert arc.render_segments("s1", EVENTS_SCOPE) == []
        assert result["live"] == 1  # one turn erased (historical contract)

    def test_flush_and_release_erases_events_log(self, tmp_path, monkeypatch, hermetic_conf):
        set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first
        arc = _arc_with_one_event(tmp_path)

        arc.flush_and_release()

        assert arc.render_segments("s1", EVENTS_SCOPE) == []


def _multi_turn(sid: str = "s1", n: int = 5) -> list[SemanticEvent]:
    """``n`` distinct ``turn.started`` events for one session (each its own turn)."""
    return [
        SemanticEvent(
            event_type="turn.started",
            session_id=sid,
            trace_id=f"trace_t{i}",
            turn_id=f"t{i}",
            occurred_at=f"2026-06-14T00:{i:02d}:00+00:00",
            payload={"input": f"q{i}"},
        )
        for i in range(n)
    ]


class TestRetentionSpansTheWholeChunkFamily:
    """The #762 gating must cover EVERY chunk of the rolled ``_events`` family, not just
    chunk 1 — retain all when the trace is disabled, erase all when it is enabled."""

    def _arc_with_three_chunks(self, tmp_path, monkeypatch):
        # chunk 2 -> 5 events roll into 3 chunks [2, 2, 1].
        monkeypatch.setenv("CLIO_ARC_EVENTS_CHUNK_SEGMENTS", "2")
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        for e in _multi_turn(n=5):
            arc.record_semantic_event(e)
        assert arc._live.events_scopes("s1") == ["_events", "_events/2", "_events/3"]
        return arc

    def test_release_retains_all_chunks_when_trace_disabled(
        self, tmp_path, monkeypatch, hermetic_conf
    ):
        arc = self._arc_with_three_chunks(tmp_path, monkeypatch)
        set_config("provenance.agentic.providers", [])

        result = arc.release_session("s1")

        # Every chunk of the only copy survives (#762) and nothing was erased.
        assert arc._live.events_scopes("s1") == ["_events", "_events/2", "_events/3"]
        assert result["live"] == 0

    def test_release_erases_all_chunks_when_trace_enabled(
        self, tmp_path, monkeypatch, hermetic_conf
    ):
        arc = self._arc_with_three_chunks(tmp_path, monkeypatch)
        set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first

        arc.release_session("s1")

        # The WHOLE family is gone (not just chunk 1).
        assert arc._live.events_scopes("s1") == []
        for scope in ("_events", "_events/2", "_events/3"):
            assert arc.render_segments("s1", scope) == []

    def test_flush_and_release_erases_all_chunks_when_trace_enabled(
        self, tmp_path, monkeypatch, hermetic_conf
    ):
        arc = self._arc_with_three_chunks(tmp_path, monkeypatch)
        set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first

        arc.flush_and_release()

        assert arc._live.events_scopes("s1") == []
