"""Author-independent narrowing disclosure (#1325/#1113).

clio drives the MRTR loop, so it -- not the MCP server author -- guarantees the
main agent is told when a tool result was shaped by an agent-answered
elicitation. These pin the pure record/drain/stamp machinery
(``elicitation_correlation``) that ``tool_hooks.assemble_model_observation``
runs; the live end-to-end signal is asserted on the compute-node run.
"""

from __future__ import annotations

import json

from clio_agent.gact.elicitation_correlation import (
    drain_narrowing_disclosures,
    record_narrowing_disclosure,
    stamp_observation_with_disclosures,
)


def test_record_then_drain_is_session_and_tool_scoped() -> None:
    record_narrowing_disclosure("sess-A", "pandas_load_data", {"top_n": 100})
    record_narrowing_disclosure("sess-A", "other_tool", {"columns": "x"})

    # Draining one tool leaves the other's entry intact.
    got = drain_narrowing_disclosures("sess-A", "pandas_load_data")
    assert got == [{"tool": "pandas_load_data", "audience": "agent", "answer": {"top_n": 100}}]
    # Second drain of the same tool is empty (popped).
    assert drain_narrowing_disclosures("sess-A", "pandas_load_data") == []
    # The other tool's entry survived.
    assert drain_narrowing_disclosures("sess-A", "other_tool")[0]["answer"] == {"columns": "x"}
    # Unknown session never raises, returns empty.
    assert drain_narrowing_disclosures("nope", "pandas_load_data") == []


def test_stamp_json_result_merges_clio_elicitation_key() -> None:
    result = json.dumps({"size_guard": {"guarded": True}, "records": [1, 2, 3]})
    disclosures = [{"tool": "pandas_load_data", "audience": "agent", "answer": {"top_n": 100}}]

    stamped = stamp_observation_with_disclosures(result, disclosures)
    parsed = json.loads(stamped)
    assert parsed["_clio"]["elicitation"]["narrowed_by"] == "agent"
    assert parsed["_clio"]["elicitation"]["answers"] == [{"top_n": 100}]
    # Original payload is preserved, never rewritten.
    assert parsed["records"] == [1, 2, 3]


def test_stamp_non_json_result_prepends_typed_note() -> None:
    disclosures = [{"tool": "t", "audience": "agent", "answer": {"columns": "a,b"}}]
    stamped = stamp_observation_with_disclosures("plain text result", disclosures)
    assert stamped.startswith("[clio]")
    assert "narrowed mid-call" in stamped
    assert "plain text result" in stamped


def test_stamp_no_disclosures_is_identity() -> None:
    assert stamp_observation_with_disclosures("unchanged", []) == "unchanged"
    # Non-string observations pass through untouched even with disclosures present.
    obj = {"already": "structured"}
    assert stamp_observation_with_disclosures(obj, [{"answer": {}}]) is obj
