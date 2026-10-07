"""Readable review survives script failure without losing activity chronology."""

from __future__ import annotations

import base64
import gzip
import hashlib
import html
import io
import json
import re
import zipfile
from typing import Any

from clio_agent.gact.session_export_document import document_body
from clio_agent.gact.session_export_viewer import write_archive_review


def recorded_work() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return parallel calls whose result arrival order differs from request order."""
    transcript = {
        "session": {"id": "s", "title": "Experiment"},
        "children": [],
        "exported_at": "2026-10-05",
        "recording": {},
        "semantic_events": [],
        "loaded_skills": [{"skill_id": "audit", "content": "# Audit\nRead the manifest."}],
        "tool_records": [
            {
                "call_id": "a",
                "tool": "read_file",
                "input": {"path": "manifest.json"},
                "output": "VERSION SIX",
            },
            {
                "call_id": "b",
                "tool": "shell",
                "input": {"kwargs": {"command": "python audit.py"}},
                "output": {"stdout": "412 checked\n413 checked"},
            },
        ],
        "messages": [
            {
                "id": "m",
                "role": "assistant",
                "parts": [
                    {"type": "thinking", "text": "**Check the source**"},
                    {"type": "tool_call", "call_id": "a"},
                    {"type": "tool_call", "call_id": "b"},
                    {"type": "tool_result", "call_id": "b", "content": "TRUNCATED"},
                    {"type": "tool_result", "call_id": "a"},
                    {"type": "text", "text": "## Findings\n**All checked.**"},
                ],
            }
        ],
    }
    return transcript, {"mode": "transcript", "files": [], "artifacts": [], "omissions": []}


def test_script_free_conversation_keeps_order_and_complete_results() -> None:
    transcript, manifest = recorded_work()
    rendered = document_body(transcript, manifest)
    ordered = [
        "Check the source",
        "manifest.json",
        "python audit.py",
        "412 checked",
        "VERSION SIX",
        "Findings",
    ]
    assert [rendered.index(text) for text in ordered] == sorted(
        rendered.index(text) for text in ordered
    )
    assert "TRUNCATED" not in rendered
    assert "<h2>Findings</h2>" in rendered
    assert "<details>" not in rendered
    assert "412 checked\n413 checked" in rendered


def test_self_contained_viewer_has_readable_fallback_and_valid_script_hashes() -> None:
    transcript, manifest = recorded_work()
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w") as archive:
        write_archive_review(
            archive,
            transcript,
            manifest,
            {
                "javascript": 'console.log("</script><script>bad()");',
                "stylesheet": "body{color:black}",
                "snapshot": {"responses": {}},
            },
        )
    with zipfile.ZipFile(memory) as archive:
        rendered = archive.read("index.html").decode()
        assert "412 checked" in rendered
        assert "<script src=" not in rendered
        assert "<link" not in rendered
        scripts = re.findall(r"<script>(.*?)</script>", rendered, re.S)
        assert len(scripts) == 2
        policy = html.unescape(rendered)
        for script in scripts:
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in policy
        assert "</script><script>bad()" not in rendered
        assert "Read the manifest" in rendered
        assert "Exact record (JSON)" in rendered
        assert archive.namelist() == ["index.html"]


def test_script_free_images_use_captured_bytes_and_active_content_is_escaped() -> None:
    transcript, manifest = recorded_work()
    transcript["messages"][0]["parts"] = [
        {"type": "text", "text": "<script>bad()</script>\n\n![Plot](artifact://p)"}
    ]
    rendered = document_body(
        transcript, manifest, {"responses": {"GET /v1/artifacts/p/bytes": {"bytes": "iVBORfake"}}}
    )
    assert "<script>bad()" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "data:image/png;base64,iVBORfake" in rendered


def test_embedded_viewer_is_losslessly_compressed_with_no_companion_files() -> None:
    transcript, manifest = recorded_work()
    javascript = "const text='</script> UTF-8: café';\n" * 10000
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        write_archive_review(
            archive,
            transcript,
            manifest,
            {
                "javascript": javascript,
                "stylesheet": "",
                "snapshot": {},
            },
        )
        rendered = archive.read("index.html").decode()
        scripts = re.findall(r"<script>(.*?)</script>", rendered, re.S)
        packed = re.search(r'atob\(("[A-Za-z0-9+/=]+")\)', scripts[1])
        assert packed is not None
        assert gzip.decompress(base64.b64decode(json.loads(packed[1]))).decode() == javascript
        assert len(rendered) < len(javascript) / 2
        assert archive.namelist() == ["index.html"]


def test_workspace_media_redirect_uses_captured_bytes_without_reading_originals() -> None:
    transcript, manifest = recorded_work()
    snapshot = {
        "responses": {
            "GET /v1/artifacts/p/bytes": {
                "error": {"details": {"fetch_via": "/v1/workspaces/w/files/read?path=plot.png"}}
            },
            "GET /v1/workspaces/w/files/read?path=plot.png": {"bytes": "iVBORcaptured"},
        }
    }
    rendered = document_body(transcript, manifest, snapshot)
    assert 'id="saved-images"' in rendered
    assert "data:image/png;base64,iVBORcaptured" in rendered
