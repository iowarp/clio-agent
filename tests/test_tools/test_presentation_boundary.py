"""A result's sandbox diagnosis is shown by the client's notice, not outlined as data."""

from __future__ import annotations

from clio_agent.tools.tool_presentation import standard_mcp_presentation


def _outline(structured: dict) -> str:
    view = standard_mcp_presentation({"structuredContent": structured, "content": []})
    return "\n".join(block.get("text", "") for block in view["blocks"])


def test_the_diagnosis_is_left_out_but_a_domain_boundary_field_stays() -> None:
    """SABOTAGE: outline the whole result -> the diagnosis appears -> red."""
    diagnosis = {"kind": "write_denied", "confidence": "confirmed", "message": "blocked"}
    text = _outline({"cwd": "/w", "exit_code": 1, "boundary": diagnosis})
    assert "write\\_denied" not in text and "write_denied" not in text
    assert "Exit code" in text
    assert "Boundary" in _outline({"boundary": [[0, 0], [1, 1]], "name": "plot 4"})
