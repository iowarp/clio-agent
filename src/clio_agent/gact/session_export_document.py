"""Readable archive documents that remain useful without JavaScript."""

from __future__ import annotations

import html
import json
from types import MethodType
from typing import Any
from urllib.parse import quote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML

CSS = """
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f5f7f8;color:#233238;font:16px/1.65 system-ui,sans-serif}
main{max-width:980px;margin:40px auto;padding:0 24px}header{border-bottom:1px solid #d9e1e4;padding-bottom:24px;margin-bottom:32px}
h1{font-size:36px;line-height:1.2;margin:8px 0}h2{font-size:24px}h3{font-size:17px}p{margin:.6em 0}.eyebrow,.meta{color:#62767f;font-size:13px}.eyebrow{letter-spacing:.12em;text-transform:uppercase}
nav{display:flex;flex-wrap:wrap;gap:20px;margin-top:20px}a{color:#007b83;text-underline-offset:3px}article{background:white;border:1px solid #d9e1e4;border-radius:14px;padding:24px;margin:24px 0}
article.user{background:#eaf4f4;border-color:#c5dedf}.activity{border-left:2px solid #b5d5d6;padding:8px 18px;margin:12px 0}.activity h3{margin:0 0 8px}.step{color:#526d76;font-size:14px;margin:18px 0}.step p{margin:0}
pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;padding:16px;background:#f2f5f6;border-radius:8px;overflow:auto;max-height:360px}code{font-family:ui-monospace,monospace;font-size:.88em}
.output{max-height:360px;overflow:auto;padding:0 12px;border:1px solid #e0e8eb;border-radius:8px}dl{margin:0}dt{font-weight:600;font-size:13px;color:#58717a}dd{margin:0 0 10px;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #d9e1e4;padding:8px;text-align:left}
img{max-width:100%;height:auto}figure{margin:24px 0}figcaption{font-size:14px;color:#58717a}.notice{padding:12px 16px;background:#eaf4f4;border-radius:8px}.files li{overflow-wrap:anywhere}details{margin:16px 0}summary{cursor:pointer}section{scroll-margin-top:20px}
@media(max-width:600px){main{padding:0 14px;margin:24px auto}article{padding:16px}h1{font-size:28px}}
"""


def escape(value: Any) -> str:
    """Escape recorded text for HTML text and quoted attributes."""
    return html.escape(str(value))


def relative_link(path: str) -> str:
    """Encode archive path segments without turning filenames into fragments."""
    return "/".join(quote(segment, safe="") for segment in path.split("/"))


def _output_key(path: str) -> str:
    value = path.removeprefix("\\\\?\\").replace("\\", "/")
    return value.lower() if len(value) > 1 and value[1] == ":" else value


class DocumentRenderer:
    """Format saved content, resolving only captured or included file references."""

    def __init__(self, manifest: dict[str, Any], snapshot: dict[str, Any] | None) -> None:
        self.manifest = manifest
        self.snapshot = snapshot or {}
        self.references: dict[str, set[str]] = {}
        self.markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
        renderer = self.markdown.renderer
        assert isinstance(renderer, RendererHTML)
        renderer.rules["image"] = MethodType(DocumentRenderer._image, self)
        renderer.rules["link_open"] = MethodType(DocumentRenderer._link, self)

    def target(self, uri: str, *, image: bool = False) -> str:
        """Map artifact references to captured images or portable artifact files."""
        matches = self.references.get(uri, set())
        if not uri.startswith("artifact://") and len(matches) == 1 and next(iter(matches)) != uri:
            return self.target(next(iter(matches)), image=image)
        if uri.startswith("artifact://"):
            aid = uri.removeprefix("artifact://").split("/")[0]
            if image:
                response = self.snapshot.get("responses", {}).get(
                    f"GET /v1/artifacts/{aid}/bytes", {}
                )
                fetch_via = response.get("error", {}).get("details", {}).get("fetch_via")
                if isinstance(fetch_via, str):
                    response = self.snapshot.get("responses", {}).get(f"GET {fetch_via}", {})
                encoded = response.get("bytes")
                if isinstance(encoded, str):
                    # Only raster images are embedded; recorded SVG/HTML is not executed.
                    media = (
                        "image/png"
                        if encoded.startswith("iVBOR")
                        else (
                            "image/jpeg"
                            if encoded.startswith("/9j/")
                            else ("image/gif" if encoded.startswith("R0lGOD") else "")
                        )
                    )
                    if media:
                        return f"data:{media};base64,{encoded}"
            for item in self.manifest.get("artifacts", []):
                if item["artifact_id"] == aid and item.get("archive_path"):
                    return relative_link(item["archive_path"])
            return ""
        parsed = urlsplit(uri)
        if image and parsed.scheme:
            return ""  # No network image requests from a portable review.
        if parsed.scheme in {"http", "https", "mailto"} and not image:
            return uri
        if not parsed.scheme and uri in {item["archive_path"] for item in self.manifest["files"]}:
            return relative_link(uri)
        return ""

    def _image(self, tokens: Any, index: int, options: Any, env: Any) -> str:
        token = tokens[index]
        target = self.target(token.attrGet("src") or "", image=True)
        caption = escape(token.content or "Recorded image")
        if not target:
            return f"<span>{caption} · image was not captured</span>"
        return f'<img src="{escape(target)}" alt="{caption}">'

    def previews(self) -> str:
        """Show captured raster results even if the interactive renderer cannot run."""
        figures = []
        responses = self.snapshot.get("responses", {})
        for key in responses:
            if not key.startswith("GET /v1/artifacts/") or not key.endswith("/bytes"):
                continue
            aid = key.split("/")[3]
            target = self.target(f"artifact://{aid}", image=True)
            if not target.startswith("data:image/"):
                continue
            name = next(
                (
                    response["json"].get("name")
                    for response in responses.values()
                    if isinstance(response.get("json"), dict)
                    and response["json"].get("uri") == f"artifact://{aid}"
                ),
                None,
            )
            figures.append(
                f'<figure><img src="{escape(target)}" alt="{escape(name or "Captured result")}"><figcaption>{escape(name or "Captured result")}</figcaption></figure>'
            )
        return (
            '<section id="saved-images"><h2>Captured results</h2>' + "".join(figures) + "</section>"
            if figures
            else ""
        )

    def _link(self, tokens: Any, index: int, options: Any, env: Any) -> str:
        target = self.target(tokens[index].attrGet("href") or "")
        return f'<a href="{escape(target)}">' if target else '<a aria-disabled="true">'

    def text(self, value: str) -> str:
        """Render Markdown while escaping raw HTML and rejecting active links."""
        return self.markdown.render(value)

    def value(self, value: Any) -> str:
        """Format actual inputs and outputs without exposing the record envelope."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            if value.get("status") == "spilled" and isinstance(value.get("path"), str):
                item = next(
                    (
                        item
                        for item in self.manifest.get("tool_output_files", [])
                        if _output_key(item["source_path"]) == _output_key(value["path"])
                    ),
                    None,
                )
                if item is None:
                    return '<p class="notice">The complete saved output is unavailable. The recorded excerpt is retained.</p>'
                target = relative_link(item["archive_path"])
                text = self.snapshot.get("tool_output_text", {}).get(item["source_path"])
                return (
                    f"<p>Complete saved output · {item['bytes']:,} bytes</p>"
                    if self.manifest["mode"] == "transcript"
                    else f'<p><a href="{escape(target)}">Open complete saved output</a> · {item["bytes"]:,} bytes</p>'
                ) + (f"<pre>{escape(text)}</pre>" if text is not None else "")
            if "structuredContent" in value and value.get("content") == []:
                return self.value(value["structuredContent"])
            if value.get("type") == "text" and isinstance(value.get("text"), str):
                return self.text(value["text"])
            if list(value) == ["kwargs"]:
                return self.value(value["kwargs"])
            return (
                "<dl>"
                + "".join(
                    f"<dt>{escape(key.replace('_', ' ').title())}</dt><dd>"
                    + (
                        f"<pre>{escape(item)}</pre>"
                        if key in {"command", "cmd", "code", "stdout", "stderr"}
                        and isinstance(item, str)
                        else self.value(item)
                    )
                    + "</dd>"
                    for key, item in value.items()
                )
                + "</dl>"
            )
        if isinstance(value, list):
            return "<ul>" + "".join(f"<li>{self.value(item)}</li>" for item in value) + "</ul>"
        return escape(json.dumps(value, ensure_ascii=False))


def document_body(
    transcript: dict[str, Any], manifest: dict[str, Any], snapshot: dict[str, Any] | None = None
) -> str:
    """Render every saved message part in place, with complete matched tool results."""
    renderer = DocumentRenderer(manifest, snapshot)
    sections: list[str] = []
    for row in [transcript, *transcript.get("children", [])]:
        tools = {tool["call_id"]: tool for tool in row["tool_records"]}
        parts: list[str] = []
        for message in row["messages"]:
            renderer.references = {}
            for part in message["parts"]:
                if part["type"] == "resource_link" and part.get("name") and part.get("uri"):
                    renderer.references.setdefault(part["name"], set()).add(part["uri"])
            content: list[str] = []
            for part in message["parts"]:
                kind = part["type"]
                if kind == "tool_call":
                    tool = tools.get(part["call_id"], {})
                    name = part.get("tool_title") or part.get("tool_name") or tool.get("tool")
                    content.append(
                        f'<section class="activity" data-part="tool_call"><h3>{escape(name or "Tool")} · request</h3>'
                        + renderer.value(tool.get("input", part.get("input", {})))
                        + "</section>"
                    )
                elif kind == "tool_result":
                    tool = tools.get(part["call_id"], {})
                    output = tool.get("output", part.get("content", []))
                    if tool.get("error"):
                        output = (
                            tool["error"]
                            if not output
                            else {"error": tool["error"], "result": output}
                        )
                    name = part.get("tool_title") or part.get("tool_name") or tool.get("tool")
                    status = tool.get("status", "failed" if part.get("is_error") else "returned")
                    rendered_output = renderer.value(output)
                    if name == "load_skill":
                        rendered_output = (
                            "<details><summary>Show loaded skill instructions</summary>"
                            + rendered_output
                            + "</details>"
                        )
                    content.append(
                        f'<section class="activity" data-part="tool_result"><h3>{escape(name or "Tool")} · {escape(status)}</h3><div class="output">'
                        + rendered_output
                        + "</div></section>"
                    )
                elif kind in {"thinking", "injection"}:
                    content.append(
                        f'<section class="step" data-part="{kind}"><span class="meta">'
                        + ("Recorded step" if kind == "thinking" else "Session instruction")
                        + "</span>"
                        + renderer.text(part.get("text", ""))
                        + "</section>"
                    )
                elif kind == "text":
                    content.append(renderer.text(part.get("text", "")))
                elif kind == "image":
                    target = renderer.target(part.get("url", ""), image=True)
                    if target:
                        content.append(f'<img alt="Recorded image" src="{escape(target)}">')
                elif kind == "resource_link":
                    target = renderer.target(part.get("uri", ""))
                    label = part.get("name") or part.get("uri") or "Recorded file"
                    content.append(
                        f'<p><a href="{escape(target)}">{escape(label)}</a></p>'
                        if target
                        else f"<p>{escape(label)} · source file requires Effects or Full</p>"
                    )
                elif kind == "a2ui":
                    content.append(
                        '<p class="meta">Saved interactive view · available in the enhanced review.</p>'
                    )
                else:
                    content.append(
                        f'<p class="meta">Recorded {escape(kind.replace("_", " "))} · exact record in Evidence.</p>'
                    )
            parts.append(
                f'<article class="{escape(message["role"])}" id="{escape(message["id"])}"><h3>'
                + (
                    "You"
                    if message["role"] == "user"
                    else "CLIO"
                    if message["role"] == "assistant"
                    else escape(message["role"])
                )
                + f'</h3><p class="meta">{escape(message.get("created_at", ""))}</p>'
                + (
                    "".join(content)
                    or '<p class="meta">No visible content was recorded for this message.</p>'
                )
                + "</article>"
            )
        sections.append(
            f'<section id="{escape(row["session"]["id"])}"><h2>{escape(row["session"]["title"])}</h2>'
            + "".join(parts)
            + "</section>"
        )
    body = (
        '<main><header><p class="eyebrow">CLIO · Session archive</p>'
        f"<h1>{escape(transcript['session']['title'])}</h1><p>{escape(manifest['mode'].title())} · "
        f"{len(transcript['messages'])} messages · {len(transcript['tool_records'])} tool records</p>"
        '<nav><a href="#conversation">Conversation & activity</a><a href="#saved-images">Captured results</a><a href="#included-files">Files</a>'
        '<a href="#evidence">Evidence & skills</a></nav></header>'
        '<section id="conversation">'
        + "".join(sections)
        + "</section>"
        + renderer.previews()
        + '<section id="included-files"><h2>Included files</h2><p>The archive keeps the full transcript, tool inputs and outputs, traces and loaded skill instructions.</p>'
        '<p><a href="transcript.json">Raw transcript</a> · <a href="manifest.json">Complete file inventory and checksums</a> · <a href="evidence.html">Read tools and skills</a></p><ul class="files">'
        + "".join(
            f'<li><a href="{escape(relative_link(item["archive_path"]))}">{escape(item["archive_path"])}</a></li>'
            for item in manifest["files"]
            if not item["archive_path"].startswith("workspace/")
        )
        + '</ul><p class="meta">Full workspace files are listed in manifest.json; the enhanced review searches the complete inventory.</p></section></main>'
    )
    if manifest["mode"] == "transcript":
        body = body.split('<section id="included-files">')[0] + "</main>"
        body = body.replace('<a href="#included-files">Files</a>', "")
    return body


def evidence_body(
    transcript: dict[str, Any], manifest: dict[str, Any], snapshot: dict[str, Any] | None = None
) -> str:
    """Keep exact records separate from the human conversation."""
    renderer = DocumentRenderer(manifest, snapshot)
    content = [
        '<main><header><p class="eyebrow">CLIO · Evidence</p><h1>Recorded work</h1><nav><a href="index.html">Back to conversation</a><a href="#skills">Loaded skills</a><a href="transcript.json">Raw transcript</a><a href="manifest.json">File inventory</a></nav></header>'
    ]
    for row in [transcript, *transcript.get("children", [])]:
        content.append(f"<h2>{escape(row['session']['title'])} · tool records</h2>")
        for tool in row["tool_records"]:
            content.append(
                f'<article id="{escape(tool["call_id"])}"><h3>{escape(tool.get("tool") or "Historical tool")} · {escape(tool.get("status", "recorded"))}</h3>'
                f'<p class="meta">{escape(tool["call_id"])}</p><h3>Input</h3>'
                + renderer.value(tool.get("input"))
                + '<h3>Output</h3><div class="output">'
                + renderer.value(tool.get("error") or tool.get("output"))
                + "</div><details><summary>Exact record (JSON)</summary><pre>"
                + escape(json.dumps(tool, indent=2, ensure_ascii=False))
                + "</pre></details></article>"
            )
        content.append('<section id="skills"><h2>Loaded skill instructions</h2>')
        for skill in row["loaded_skills"]:
            content.append(
                "<article><h3>"
                + escape(skill.get("skill_id", "Skill"))
                + "</h3>"
                + "<details><summary>Show loaded skill instructions</summary>"
                + renderer.text(skill.get("content", "The original body was not recorded."))
                + "</details>"
                + "</article>"
            )
        content.append("</section><h2>Recording coverage</h2>" + renderer.value(row["recording"]))
    content.append(
        "<h2>Unavailable or excluded files</h2>" + renderer.value(manifest["omissions"]) + "</main>"
    )
    if snapshot and snapshot.get("failures"):
        content.insert(
            -1, "<h2>Uncaptured visual dependencies</h2>" + renderer.value(snapshot["failures"])
        )
    return "".join(content)


def embedded_evidence(
    transcript: dict[str, Any], manifest: dict[str, Any], snapshot: dict[str, Any]
) -> str:
    """Keep skills and exact records inside the same HTML document."""
    content = evidence_body(transcript, manifest, snapshot)
    content = content.replace("<main>", '<main id="evidence">', 1).replace(
        'href="index.html"', 'href="#conversation"'
    )
    if manifest["mode"] == "transcript":
        content = content.replace(
            '<a href="transcript.json">Raw transcript</a><a href="manifest.json">File inventory</a>',
            "",
        )
    return content
