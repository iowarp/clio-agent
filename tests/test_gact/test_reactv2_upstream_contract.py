"""Drift gate for the private DSPy ReActV2 seams CLIO consumes."""

from clio_agent.gact.agents.reactv2_upstream import dspy_reactv2_contract_snapshot


def test_dspy_reactv2_private_contract_has_not_drifted() -> None:
    snapshot = dspy_reactv2_contract_snapshot()
    assert set(snapshot["hashes"]) == {"forward", "_make_submit_tool", "_make_react_signature"}
    assert "_coerce_history" in snapshot["helpers"]
