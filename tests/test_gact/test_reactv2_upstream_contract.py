"""Drift gate for the private DSPy ReActV2 seams CLIO consumes."""

from clio_agent.gact.agents.reactv2_upstream import dspy_reactv2_contract_snapshot


def test_dspy_reactv2_private_contract_has_not_drifted() -> None:
    snapshot = dspy_reactv2_contract_snapshot()
    # #1282 F14: _execute_tool_calls + __init__ joined the pin -- the #1275 D1
    # fix makes both load-bearing (the swallow body _execute_tool_calls's own
    # per-call escalation relies on running verbatim; __init__'s signature is
    # hardcoded by _RetainingReActV2.__init__'s override).
    assert set(snapshot["hashes"]) == {
        "forward",
        "_make_submit_tool",
        "_make_react_signature",
        "_execute_tool_calls",
        "__init__",
    }
    assert "_coerce_history" in snapshot["helpers"]
