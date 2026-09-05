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


def test_web_mcp_command_keeps_endpoint_configuration_in_launch_layer() -> None:
    leg = _load_leg()

    assert leg.web_mcp_command("") == '"clio-kit" "mcp-server" "web"'
    assert leg.web_mcp_command("http://10.0.0.102:8089") == (
        '"clio-kit" "mcp-server" "web" "--remote-url" "http://10.0.0.102:8089"'
    )
