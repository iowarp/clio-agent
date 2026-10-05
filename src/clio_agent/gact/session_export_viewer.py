"""Script-free, offline review of a session archive."""

from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import zipfile
from typing import Any


def write_archive_review(
    archive: zipfile.ZipFile,
    transcript: dict[str, Any],
    manifest: dict[str, Any],
    visual_review: dict[str, Any] | None = None,
) -> None:
    """Write the offline viewer and append hashes without changing recorded evidence."""

    def write(name: str, content: bytes) -> None:
        archive.writestr(name, content)
        manifest["files"].append(
            {
                "archive_path": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    if visual_review is None:
        write("index.html", render_archive_review(transcript, manifest).encode())
        return
    write("review.js", str(visual_review["javascript"]).encode())
    write("review.css", str(visual_review["stylesheet"]).encode())
    # Keep full semantic events in transcript.json and traces/. The visual
    # projection needs messages, tool records and skills rather than a second
    # in-memory copy of the potentially very large trace corpus.
    review_transcript = {
        key: value for key, value in transcript.items() if key != "semantic_events"
    }
    review_transcript["children"] = [
        {key: value for key, value in row.items() if key != "semantic_events"}
        for row in transcript["children"]
    ]
    data = {
        "transcript": review_transcript,
        "manifest": manifest,
        "snapshot": visual_review["snapshot"],
    }
    compressed = gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0)
    write(
        "review-data.js",
        (
            "window.CLIO_EXPORT_DATA=" + json.dumps(base64.b64encode(compressed).decode()) + ";"
        ).encode(),
    )
    write(
        "index.html",
        (
            '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src blob: data:; media-src blob: data:; font-src data:; worker-src blob:; connect-src 'none'\">"
            '<title>CLIO session archive</title><link rel="stylesheet" href="review.css"><div id="root"></div>'
            '<script src="review-data.js"></script><script src="review.js"></script></html>'
        ).encode(),
    )


def render_archive_review(transcript: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Render escaped recorded evidence and relative links without a running CLIO."""

    def esc(value: Any) -> str:
        return html.escape(str(value))

    def detail(label: str, value: Any) -> str:
        return f"<details><summary>{esc(label)}</summary><pre>{esc(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>"

    sections = []
    for row in [transcript, *transcript["children"]]:
        sections.append(f"<h2>{esc(row['session']['title'])}</h2>")
        sections.append(detail("Recording coverage and limitations", row["recording"]))
        for message in row["messages"]:
            sections.append(detail(f"Message · {message['role']}", message))
        sections.append("<h3>Tools</h3>")
        for tool in row["tool_records"]:
            sections.append(detail(f"{tool.get('tool', '')} · {tool['call_id']}", tool))
        sections.append("<h3>Loaded skills</h3>")
        for skill in row["loaded_skills"]:
            sections.append(
                detail(f"{skill.get('skill_id', '')} · {skill.get('file', 'procedure')}", skill)
            )
    links = "".join(
        f'<li><a href="{esc(row["archive_path"])}">{esc(row["archive_path"])}</a> ({row["bytes"]} bytes)</li>'
        for row in manifest["files"]
    )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:\">"
        "<title>CLIO session archive</title><style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20252b;background:#fff}summary{cursor:pointer;padding:.7rem;background:#edf2f5;margin:.3rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px monospace;padding:1rem;border:1px solid #ccd4db}a{color:#12617c}</style>"
        f"<h1>{esc(transcript['session']['title'])}</h1><p>{esc(manifest['mode'].title())} export · {esc(transcript['exported_at'])}</p>"
        '<p><a href="transcript.json">Raw transcript</a> · <a href="manifest.json">File checksums and coverage</a></p>'
        + detail("Files unavailable or excluded", manifest["omissions"])
        + "".join(sections)
        + f"<h2>Included files</h2><ul>{links}</ul></html>"
    )
