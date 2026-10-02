"""Wire-equivalence golden for the #756 error-envelope turn (#767 PR2).

PR2 moves the turn-loop transcript lifecycle (open at turn start, settle on
every exit path) and routes the stream tap through the TurnTranscript ledger.
The finalize error envelope is one of those exit paths, so it gets its own
golden: a turn that streams live text and then crashes in the finalize region
must emit the identical SSE event stream and persist the identical error
assistant message as ``develop`` did.

The golden is captured on the PRISTINE reference tree (``origin/develop``) by
copying this module into a scratch worktree there and running::

    CLIO_TURN_TRANSCRIPT_GOLDEN_REGEN=1 uv run --extra dev pytest \
        tests/test_gact/test_turn_transcript_equivalence_pr2.py -k golden --no-cov

The golden was recaptured after a0e1d9a9 (clean delegation wire): routing
decisions are ``routing.decision`` semantic events now, so the stream carries
no ``routing_decision`` ``message.part.added`` and persists no routing part.

This module deliberately reuses the PR1 harness (normalizer, settle poll,
scenario builders) via import so both trees drive the exact same probe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests._harness import emit_live_text, install_scripted_module
from tests.turn_signals import wait_for_terminal_status

from .test_turn_transcript_equivalence import (
    _build,
    _normalized_trace,
    _PlainAgent,
    _Pred,
)

GOLDEN_DIR = Path(__file__).parent / "goldens" / "turn_transcript_pr2"
_GOLDEN_REGEN = os.environ.get("CLIO_TURN_TRANSCRIPT_GOLDEN_REGEN") == "1"


def scenario_error_envelope_turn(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """Live streamed text, then a finalize-region crash (#756 envelope).

    ``_enrich_cancellation_error_info`` runs unconditionally in the finalize
    region, so raising there simulates any finalize crash AFTER live deltas
    already went out — the envelope must settle the turn with a visible
    error message.
    """

    def _boom(app: Any, sid: str, error_info: Any) -> Any:
        raise RuntimeError("simulated finalize failure")

    def streamed_forward(**kwargs: Any) -> Any:
        del kwargs
        emit_live_text("Partial ")
        emit_live_text("streamed ")
        emit_live_text("answer.")
        return _Pred(
            answer="Partial streamed answer.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    monkeypatch.setattr("clio_agent.gact.app._enrich_cancellation_error_info", _boom)
    install_scripted_module(monkeypatch, streamed_forward)
    app = _build(tmp_path, "errenvelope", _PlainAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "e"}).json()["id"]
        cursor = app.state.bus.latest_event_id(sid)
        ack = client.post(f"/v1/sessions/{sid}/messages", json={"text": "stream then crash"})
        assert ack.status_code == 200, ack.text
        status = wait_for_terminal_status(app.state.bus, sid, after_event_id=cursor)
        assert status == "error", f"envelope did not settle the session: {status!r}"
        trace = _normalized_trace(app, client, sid)
    completed = [e for e in trace["events"] if e["type"] == "message.completed"]
    assert completed, "the envelope must publish message.completed"
    assert completed[-1]["payload"]["stop_reason"] == "error"
    assert completed[-1]["payload"]["error_info"]["error"] == "finalize_error"
    return trace


def _assert_matches_golden(name: str, trace: dict[str, Any]) -> None:
    golden_path = GOLDEN_DIR / f"{name}.json"
    if _GOLDEN_REGEN:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert golden_path.exists(), (
        f"golden {golden_path} missing — regenerate on the reference tree with "
        "CLIO_TURN_TRANSCRIPT_GOLDEN_REGEN=1 (see module docstring)"
    )
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert trace == golden, (
        f"wire behavior diverged from the develop golden for {name!r}: "
        "same turn must emit the identical SSE event sequence and persist "
        "the identical assistant parts"
    )


def test_error_envelope_turn_matches_develop_golden(tmp_path: Path, monkeypatch: Any) -> None:
    _assert_matches_golden(
        "error_envelope_turn", scenario_error_envelope_turn(tmp_path, monkeypatch)
    )
