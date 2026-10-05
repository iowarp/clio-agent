"""Self-contained interactive review with a readable script-free fallback."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import zipfile
from typing import Any

from clio_agent.gact.session_export_document import CSS, document_body, embedded_evidence, escape


def _page(title: str, body: str, *, stylesheet: str = "", scripts: tuple[str, ...] = ()) -> str:
    safe_scripts = tuple(re.sub(r"</script", r"<\\/script", s, flags=re.I) for s in scripts)
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
        for s in safe_scripts
    )
    script_policy = hashes + " 'unsafe-eval'" if scripts else "'none'"
    policy = (
        f"default-src 'none'; script-src {script_policy}; style-src 'unsafe-inline'; "
        "img-src 'self' blob: data:; media-src 'self' blob: data:; font-src data:; "
        "worker-src blob:; connect-src 'none'"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(policy)}">'
        f"<title>{escape(title)} · CLIO archive</title><style>{CSS}</style>"
        + (f"<style>{stylesheet}</style>" if stylesheet else "")
        + "</head><body>"
        + body
        + "".join(f"<script>{s}</script>" for s in safe_scripts)
        + "</body></html>"
    )


def write_archive_review(
    archive: zipfile.ZipFile,
    transcript: dict[str, Any],
    manifest: dict[str, Any],
    visual_review: dict[str, Any] | None = None,
) -> None:
    """Write readable conversation/evidence and optional self-contained enhancement."""

    def write(name: str, content: bytes) -> None:
        archive.writestr(name, content)
        manifest["files"].append(
            {
                "archive_path": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    title = transcript["session"]["title"]
    snapshot = dict(visual_review["snapshot"]) if visual_review else {}
    # Transcript has no sibling files: saved outputs must be readable here too.
    output_text: dict[str, str] = {}
    for item in manifest.get("tool_output_files", []):
        output_text[item["source_path"]] = archive.read(item["archive_path"]).decode(
            "utf-8", errors="replace"
        )
    snapshot["tool_output_text"] = output_text
    embedded_outputs = (
        {
            item["archive_path"]: base64.b64encode(archive.read(item["archive_path"])).decode()
            for item in manifest.get("tool_output_files", [])
        }
        if manifest["mode"] == "transcript"
        else {}
    )
    evidence = embedded_evidence(transcript, manifest, snapshot)
    if visual_review is None:
        raw = json.dumps(
            {
                "transcript": transcript,
                "manifest": manifest,
                "snapshot": snapshot,
                "embedded_tool_outputs": embedded_outputs,
            },
            ensure_ascii=False,
        ).replace("<", "\\u003c")
        write(
            "index.html",
            _page(
                title,
                document_body(transcript, manifest, snapshot)
                + evidence
                + '<script type="application/json" id="clio-export-data">'
                + raw
                + "</script>",
            ).encode(),
        )
        return
    javascript = str(visual_review["javascript"])
    stylesheet = str(visual_review["stylesheet"])
    data = {
        "transcript": transcript,
        "manifest": manifest,
        "snapshot": snapshot,
        "embedded_tool_outputs": embedded_outputs,
    }
    compressed = gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0)
    bootstrap = "window.CLIO_EXPORT_DATA=" + json.dumps(base64.b64encode(compressed).decode()) + ";"
    fallback = (
        '<div id="archive-document">'
        + document_body(transcript, manifest, snapshot)
        + evidence
        + "</div>"
    )
    # Enhancement has a separate mount. Decoder/renderer failures leave the saved
    # document available rather than replacing it with an empty div.
    body = (
        '<div id="archive-status" role="status"></div>' + fallback + '<div id="root" hidden></div>'
    )
    write(
        "index.html",
        _page(title, body, stylesheet=stylesheet, scripts=(bootstrap, javascript)).encode(),
    )


def render_archive_review(transcript: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Render a complete human conversation and activity without running CLIO or JS."""
    return _page(transcript["session"]["title"], document_body(transcript, manifest))
