"""Focused contract tests for the document-backed web qualification leg."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_leg() -> ModuleType:
    script_dir = Path(__file__).parents[2] / "scripts" / "live_verification"
    spec = importlib.util.spec_from_file_location(
        "leg_b_web_fetch", script_dir / "leg_b_web_fetch.py"
    )
    assert spec is not None and spec.loader is not None
    sys.path.insert(0, str(script_dir))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(script_dir))


def test_document_qualification_requires_pdf_file_and_event_semantics() -> None:
    leg = _load_leg()

    assert leg.STABLE_PDF_URL.endswith(".pdf")
    assert "to_file=true" in leg.PROMPT
    assert "web_search" in leg.PROMPT
    assert "web_fetch_events" in leg.PROMPT
    assert leg.NEEDED_AGENT_TOOLS == {"web_fetch", "web_fetch_events", "web_search"}


def test_web_mcp_endpoint_is_a_durable_user_configuration() -> None:
    leg = _load_leg()

    assert leg.web_mcp_configuration("http://search.example:8089") == {
        "name": "CLIO Web Search",
        "transport": "stdio",
        "command": "clio-kit",
        "args": [
            "mcp-server",
            "web",
            "--remote-url",
            "http://search.example:8089",
        ],
    }


def test_web_testing_agent_reports_a_human_result_and_keeps_json_in_details() -> None:
    leg = _load_leg()
    prompt = (leg.PACK_TEMPLATE_DIR / "experts" / "main.md").read_text(encoding="utf-8")

    assert "concise human-readable verification result" in prompt
    assert "provider" in prompt
    assert "document identity" in prompt
    assert "conversion lifecycle" in prompt
    assert "saved Markdown" in prompt
    assert "artifact links" in prompt
    assert "Raw tool JSON belongs in expandable technical tool details" in prompt
    assert "verbatim" not in prompt.casefold()


def test_document_gate_uses_structured_tool_results_not_agent_prose() -> None:
    leg = _load_leg()
    conversion_id = "conv_123"
    fetch = {
        "args": {"url": leg.STABLE_PDF_URL, "to_file": True},
        "result": {
            "conversion_id": conversion_id,
            "markdown_path": "D:/workspace/HDF5Intro.md",
            "metadata_path": "D:/workspace/HDF5Intro.json",
            "artifacts": [
                {
                    "artifact_id": "artifact_report",
                    "uri": "artifact://ws/HDF5Intro.md@v1",
                    "path": "D:/workspace/HDF5Intro.md",
                }
            ],
        },
    }
    events = {
        "args": {"conversion_id": conversion_id, "after_sequence": 0},
        "result": {
            "conversion_id": conversion_id,
            "events": [
                {"sequence": 1, "stage": "queued"},
                {"sequence": 2, "stage": "docling"},
                {"sequence": 3, "stage": "complete"},
            ],
        },
    }

    evidence = leg.document_conversion_evidence([fetch], [events])

    assert evidence["pass"] is True
    assert evidence["shared_conversion_ids"] == [conversion_id]
    assert evidence["progress_observed"] is True


def test_document_gate_rejects_a_successful_html_style_fetch_without_conversion() -> None:
    leg = _load_leg()

    evidence = leg.document_conversion_evidence(
        [{"args": {"url": "https://example.test", "to_file": False}, "result": {"text": "ok"}}],
        [],
    )

    assert evidence["pass"] is False
