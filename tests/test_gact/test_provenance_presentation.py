"""External-input provenance blocks on the tool presentation contract (#1336)."""

from __future__ import annotations

from clio_agent.gact.artifacts.provenance_presentation import with_provenance_blocks
from clio_agent.gact.tool_result_presentation import PresentationBlock, ToolPresentation

_PRESENTER_IDS = {"file-link", "resource", "subject", "source"}


def _base_presentation() -> dict:
    return {"action": "read", "subject": "report.csv", "summary": "", "blocks": []}


def _link_block(block_id: str, label: str, uri: str) -> dict:
    """Build the exact wire shape a ``link`` block validates to (all fields)."""
    return PresentationBlock(
        id=block_id,
        type="link",
        target="file",
        label=label,
        uri=uri,
        detail="external input (not hashed)",
    ).model_dump(exclude_none=True)


def test_appends_one_link_block_per_input() -> None:
    presentation = _base_presentation()
    provenance = {
        "provenance_inputs": [
            {
                "name": "real-source.csv",
                "locator": "/tmp/real-source.csv",
                "arg": "data_path",
                "evidence": "schema-arg",
                "note": "external_input_not_hashed",
            }
        ]
    }
    result = with_provenance_blocks(presentation, provenance)
    assert result is not None
    assert result["blocks"] == [
        _link_block("input-source-0", "real-source.csv", "/tmp/real-source.csv")
    ]
    # Schema-validated: extra="forbid" is the gate.
    ToolPresentation.model_validate(result)


def test_multiple_inputs_get_indexed_ids() -> None:
    presentation = _base_presentation()
    provenance = {
        "provenance_inputs": [
            {"name": "a.csv", "locator": "/tmp/a.csv"},
            {"name": "b.csv", "locator": "/tmp/b.csv"},
        ]
    }
    result = with_provenance_blocks(presentation, provenance)
    assert result is not None
    ids = [block["id"] for block in result["blocks"]]
    assert ids == ["input-source-0", "input-source-1"]


def test_warning_adds_incomplete_block_and_degrades_status() -> None:
    presentation = _base_presentation()
    provenance = {
        "provenance_warnings": [
            {
                "reason": "external_input_contract_unknown",
                "tool": "unknown_reader",
                "arg": "path",
                "value": "/tmp/real-source.csv",
            }
        ]
    }
    result = with_provenance_blocks(presentation, provenance)
    assert result is not None
    warning_block = next(b for b in result["blocks"] if b["id"] == "provenance-incomplete")
    assert warning_block["type"] == "text"
    assert warning_block["severity"] == "warning"
    assert warning_block["text"] == (
        "Provenance incomplete: an external file argument was not recognized (path)"
    )
    assert result["status"] == "degraded"
    assert result["diagnostic"] == "provenance_incomplete"
    ToolPresentation.model_validate(result)


def test_warning_never_overrides_failed_status() -> None:
    presentation = {**_base_presentation(), "status": "failed"}
    provenance = {
        "provenance_warnings": [{"reason": "external_input_contract_unknown", "arg": "path"}]
    }
    result = with_provenance_blocks(presentation, provenance)
    assert result is not None
    assert result["status"] == "failed"


def test_warning_preserves_existing_diagnostic() -> None:
    presentation = {**_base_presentation(), "diagnostic": "already_set"}
    provenance = {
        "provenance_warnings": [{"reason": "external_input_contract_unknown", "arg": "path"}]
    }
    result = with_provenance_blocks(presentation, provenance)
    assert result is not None
    assert result["diagnostic"] == "already_set"


def test_empty_provenance_returns_identity() -> None:
    presentation = _base_presentation()
    result = with_provenance_blocks(presentation, {})
    assert result is presentation


def test_none_presentation_returns_none() -> None:
    assert (
        with_provenance_blocks(None, {"provenance_inputs": [{"name": "x", "locator": "y"}]}) is None
    )


def test_block_ids_never_collide_with_presenter_ids() -> None:
    presentation = {
        "action": "read",
        "subject": "report.csv",
        "summary": "",
        "blocks": [{"id": "file-link", "type": "link", "target": "file", "uri": "u"}],
    }
    provenance = {
        "provenance_inputs": [{"name": "a.csv", "locator": "/tmp/a.csv"}],
        "provenance_warnings": [{"reason": "external_input_contract_unknown", "arg": "path"}],
    }
    result = with_provenance_blocks(presentation, provenance)
    assert result is not None
    ids = [block["id"] for block in result["blocks"]]
    assert len(ids) == len(set(ids))
    new_ids = set(ids) - _PRESENTER_IDS
    assert "input-source-0" in new_ids
    assert "provenance-incomplete" in new_ids
    ToolPresentation.model_validate(result)
