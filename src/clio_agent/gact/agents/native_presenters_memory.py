"""Native presenters for the memory/resource/artifact family (#1333 ratchet payment).

Split out of ``native_presenters.py``: ``memory``, ``resource``, and ``artifact`` all
render workspace-scoped records (retained sessions, resources, minted artifacts) and
share the link/language helpers below.

``_resource_link``/``_session_link`` are called through the ``native_presenters``
module object (not imported by name) so that
``tests/test_gact/test_native_presentation_density.py``'s
``monkeypatch.setattr(native_presenters, "_resource_link"/"_session_link", ...)``
still reaches every call site, wherever the real definition now lives.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _resource_link(resource_id: str) -> dict[str, Any] | None:
    """Resolve a display name only inside the observing session's workspace."""
    from clio_agent.gact import context

    app = context.active_app()
    session_id = context.active_session_id()
    if app is None or not session_id:
        return None
    session = app.state.sessions.get(session_id)
    resource_store = getattr(app.state, "resource_store", None)
    if session is None or resource_store is None:
        return None
    record = resource_store.get(session.workspace_id, resource_id)
    if record is None:
        return None
    return {
        "id": "resource",
        "type": "link",
        "target": "resource",
        "uri": resource_id,
        "label": record.name,
    }


def _session_link(session_id: str) -> dict[str, Any] | None:
    """Resolve a retained session to the shared transcript-navigation link."""

    from clio_agent.gact import context

    if not session_id:
        return None
    app = context.active_app()
    if app is None:
        return None
    session = app.state.sessions.get(session_id)
    if session is None:
        return None
    return {
        "id": "session",
        "type": "link",
        "target": "session",
        "uri": session_id,
        "label": session.title or session_id,
    }


def _resource_code_language(name: str) -> str:
    """Return the shared viewer language for a code-bearing resource name."""

    lowered = name.rsplit("/", maxsplit=1)[-1].rsplit("\\", maxsplit=1)[-1].lower()
    if lowered in {"dockerfile", "makefile"}:
        return {"dockerfile": "dockerfile", "makefile": "make"}[lowered]
    extension = lowered.rsplit(".", maxsplit=1)[-1] if "." in lowered else ""
    return {
        "c": "c",
        "cc": "cpp",
        "cpp": "cpp",
        "css": "css",
        "go": "go",
        "h": "c",
        "hpp": "cpp",
        "html": "html",
        "java": "java",
        "js": "javascript",
        "json": "json",
        "jsx": "jsx",
        "mjs": "javascript",
        "php": "php",
        "ps1": "powershell",
        "py": "python",
        "rb": "ruby",
        "rs": "rust",
        "sh": "shellscript",
        "sql": "sql",
        "toml": "toml",
        "ts": "typescript",
        "tsx": "tsx",
        "vue": "vue",
        "xml": "xml",
        "yaml": "yaml",
        "yml": "yaml",
    }.get(extension, "")


def _artifact_rejection_message(artifact: Mapping[str, Any]) -> str:
    """Explain a rejected artifact with user-facing workspace semantics."""

    reason = str(artifact.get("reason") or "rejected")
    raw_name = str(artifact.get("name") or "").strip()
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1] or "This artifact"
    explanations = {
        "escapes_root": f"{name} is outside the active workspace, so it cannot be registered as an artifact.",
        "would_overwrite": (
            f"{name} already exists but is not a registered artifact. "
            "Register the existing file by path or choose another name."
        ),
        "path_missing": f"{name} does not exist, so it cannot be registered as an artifact.",
        "missing": f"{name} does not exist, so it cannot be registered as an artifact.",
        "not_found": f"{name} does not exist, so it cannot be registered as an artifact.",
    }
    return explanations.get(reason, f"{name} was rejected because {reason.replace('_', ' ')}.")


def present_memory_family(
    declaration: str, args: Mapping[str, Any], result: Any, row: Mapping[str, Any], summary: str
) -> tuple[str, list[dict[str, Any]], str, str, str]:
    """Render ``memory``/``resource``/``artifact``.

    Returns ``(summary, blocks, header_action, presentation_status, subject)``.
    """

    from clio_agent.gact.agents import native_presenters  # late: module-qualified for monkeypatch

    blocks: list[dict[str, Any]] = []
    header_action = ""
    presentation_status = ""
    subject = ""

    if declaration == "memory":
        memory_tool = str(row.get("tool") or "")
        if memory_tool == "memory_search_sessions":
            query = str(row.get("query") or args.get("query") or "").strip()
            if query:
                subject = "query"
                blocks.append({"id": subject, "type": "text", "text": query})
            hits = [hit for hit in row.get("hits", []) if isinstance(hit, Mapping)]
            searched = [str(value) for value in row.get("searched_sessions", []) if str(value)]
            if hits:
                summary = (
                    f"{len(hits)} {'match' if len(hits) == 1 else 'matches'} "
                    f"in {len(searched)} {'session' if len(searched) == 1 else 'sessions'}"
                )
            else:
                summary = "No matching sessions"
            for index, hit in enumerate(hits):
                session_id = str(hit.get("session_id") or "")
                session_title = str(hit.get("session_title") or session_id or "Session")
                if session_id:
                    blocks.append(
                        {
                            "id": f"hit-{index}-session",
                            "type": "link",
                            "target": "session",
                            "uri": session_id,
                            "label": session_title,
                        }
                    )
                excerpt = str(hit.get("text") or "").strip()
                if excerpt:
                    role = str(hit.get("role") or "").strip().replace("_", " ").capitalize()
                    blocks.append(
                        {
                            "id": f"hit-{index}-excerpt",
                            "type": "text",
                            "label": role,
                            "text": excerpt,
                        }
                    )
        elif memory_tool == "memory_read_session_summary":
            remembered = row.get("summary")
            if isinstance(remembered, Mapping):
                session_id = str(remembered.get("session_id") or "")
                session_title = str(remembered.get("title") or session_id or "Session")
                if session_id:
                    subject = "session"
                    blocks.append(
                        {
                            "id": subject,
                            "type": "link",
                            "target": "session",
                            "uri": session_id,
                            "label": session_title,
                        }
                    )
                message_count = remembered.get("message_count")
                state = str(remembered.get("status") or "").strip().replace("_", " ")
                facts = []
                if isinstance(message_count, int):
                    facts.append(
                        f"{message_count} {'message' if message_count == 1 else 'messages'}"
                    )
                if state:
                    facts.append(f"Status: {state.capitalize()}")
                summary = "\n".join(facts) or "Session summary"
                for index, excerpt in enumerate(remembered.get("recent_excerpts", [])):
                    if not isinstance(excerpt, Mapping):
                        continue
                    text = str(excerpt.get("excerpt") or "").strip()
                    if not text:
                        continue
                    role = str(excerpt.get("role") or "").strip().replace("_", " ").capitalize()
                    blocks.append(
                        {
                            "id": f"excerpt-{index}",
                            "type": "text",
                            "label": role,
                            "text": text,
                        }
                    )
        elif memory_tool == "memory_read_context_frame":
            frame = row.get("frame")
            if isinstance(frame, Mapping):
                session_id = str(frame.get("session_id") or "")
                session_link = native_presenters._session_link(session_id)
                if session_link is not None:
                    subject = "session"
                    session_link["id"] = subject
                    blocks.append(session_link)
                items = [item for item in frame.get("items", []) if isinstance(item, Mapping)]
                summary = (
                    f"{len(items)} retained {'item' if len(items) == 1 else 'items'}"
                    if items
                    else "No retained context items"
                )
                for index, item in enumerate(items):
                    kind = str(item.get("kind") or "Context item").replace("_", " ").capitalize()
                    role = str(item.get("role") or "").strip().replace("_", " ").capitalize()
                    if kind == "Message" and role:
                        kind = f"{role} message"
                    source = str(item.get("display_path") or item.get("path") or "").strip()
                    included = "Included" if item.get("included", True) else "Excluded"
                    reason = str(item.get("reason") or "").strip().replace("_", " ")
                    state = f"{included} from {reason}" if reason else included
                    detail = "\n".join(value for value in (source, state) if value)
                    blocks.append(
                        {
                            "id": f"item-{index}",
                            "type": "text",
                            "label": kind,
                            "text": detail,
                        }
                    )
    elif declaration == "resource":
        summary = str(row.get("name") or row.get("resource_id") or summary)
        if row.get("resources") == []:
            summary = "No workspace resources"
        record = row.get("resource")
        if isinstance(record, Mapping):
            summary = str(
                record.get("display_name")
                or record.get("name")
                or record.get("resource_id")
                or summary
            )
            fields = [str(record["detected_mime"])] if record.get("detected_mime") else []
            size = record.get("received_size", record.get("declared_size"))
            if isinstance(size, int):
                fields.append(f"{size:,} bytes")
            if record.get("revision") is not None:
                fields.append(f"Revision {record['revision']}")
            if record.get("state"):
                fields.append(str(record["state"]).capitalize())
            if fields:
                blocks.append({"id": "identity", "type": "text", "text": "\n".join(fields)})
            if record.get("failure"):
                blocks.append({"id": "failure", "type": "text", "text": str(record["failure"])})
        resource_id = str(
            row.get("resource_id")
            or (record.get("id") if isinstance(record, Mapping) else "")
            or ""
        )
        resource_link = native_presenters._resource_link(resource_id) if resource_id else None
        if resource_link is not None:
            summary = ""
            blocks.insert(0, resource_link)
        processing = row.get("processing")
        if isinstance(processing, Mapping):
            state = str(processing.get("state") or "")
            message = str(processing.get("message") or (f"Conversion {state}" if state else ""))
            if isinstance(processing.get("progress"), int | float) and state not in {
                "complete",
                "completed",
                "failed",
                "cancelled",
            }:
                message += f"\nProgress: {processing['progress']}%"
            if processing.get("error"):
                message += f"\n{processing['error']}"
            identity = next((block for block in blocks if block["id"] == "identity"), None)
            if message and identity is not None:
                identity["text"] += f"\n{message}"
            elif message:
                blocks.append({"id": "processing", "type": "text", "text": message})
        matches = row.get("matches")
        if isinstance(matches, list):
            if args.get("query"):
                summary = f"{len(matches)} {'match' if len(matches) == 1 else 'matches'} for “{args['query']}”"
            blocks.append(
                {
                    "id": "matches",
                    "type": "text",
                    "text": "\n".join(
                        f"{match.get('line', '')}: {match.get('text', '')}"
                        for match in matches
                        if isinstance(match, Mapping)
                    )
                    or "No matching passages",
                }
            )
        collections = row.get("collections")
        if isinstance(collections, Mapping):
            blocks.append(
                {
                    "id": "outline",
                    "type": "text",
                    "text": "\n".join(
                        f"{str(name).capitalize()}: {count}"
                        for name, count in collections.items()
                        if isinstance(count, int)
                    ),
                }
            )
        node = row.get("node")
        if isinstance(node, Mapping):
            # A document node is declared resource structure, not a model response.
            fields = [
                str(node[key])
                for key in ("title", "text", "content", "caption")
                if isinstance(node.get(key), str)
            ]
            blocks.append({"id": "node", "type": "text", "text": "\n".join(fields)})
        content = row.get("text", row.get("content"))
        if isinstance(content, str):
            resource_name = (
                str(resource_link.get("label") or "") if isinstance(resource_link, Mapping) else ""
            )
            language = _resource_code_language(resource_name)
            if row.get("representation") == "markdown":
                blocks.append({"id": "content", "type": "markdown", "text": content})
            elif language:
                blocks.append(
                    {
                        "id": "content",
                        "type": "code",
                        "language": language,
                        "text": content,
                    }
                )
            else:
                blocks.append({"id": "content", "type": "text", "text": content})
        if row.get("truncated") is True:
            blocks.append(
                {"id": "bounded", "type": "text", "text": "Result truncated by the resource tool"}
            )
        for index, resource in enumerate(row.get("resources", [])):
            if isinstance(resource, Mapping):
                blocks.append(
                    {
                        "id": f"resource-{index}",
                        "type": "link",
                        "target": "resource",
                        "uri": str(resource.get("resource_id") or resource.get("id") or ""),
                        "label": str(resource.get("name") or resource.get("id") or "Resource"),
                    }
                )
        links = [block for block in blocks if block["type"] == "link"]
        if len(links) == 1:
            subject = links[0]["id"]
    elif declaration == "artifact":
        artifact_rows = row.get("artifacts", [row])
        accepted = 0
        rejected = 0
        for index, artifact in enumerate(artifact_rows):
            if isinstance(artifact, Mapping):
                if artifact.get("accepted") is False:
                    rejected += 1
                    blocks.append(
                        {
                            "id": f"rejection-{index}",
                            "type": "text",
                            "label": "Artifact rejected",
                            "severity": "error",
                            "text": _artifact_rejection_message(artifact),
                        }
                    )
                    continue
                uri = str(artifact.get("uri") or artifact.get("artifact_id") or "")
                if uri:
                    accepted += 1
                    blocks.append(
                        {
                            "id": f"artifact-{index}",
                            "type": "link",
                            "target": "artifact",
                            "uri": uri,
                            "label": str(
                                artifact.get("name") or artifact.get("title") or "Artifact"
                            ),
                        }
                    )
        if rejected:
            presentation_status = "degraded" if accepted else "failed"
            summary = ""
        links = [block for block in blocks if block["type"] == "link"]
        if len(links) == 1:
            subject = links[0]["id"]
            artifacts = row.get("artifacts", [])
            if len(artifacts) == 1 and artifacts[0].get("accepted") is True:
                artifact = artifacts[0]
                header_action = "Create Artifact"
                outcome = "Created" if artifact.get("created") else "Already registered as"
                summary = outcome
                if artifact.get("version"):
                    summary = f"{outcome} version {artifact['version']}"

    return summary, blocks, header_action, presentation_status, subject
