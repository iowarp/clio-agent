"""Observer-only skill identity, purpose and exact supporting-file reads."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PureWindowsPath
from typing import Any


def requested_skill_files(args: Mapping[str, Any]) -> list[str]:
    """Return the same ordered, distinct reference selection as the skill reader."""
    values = [args.get("file"), *(args.get("files") or [])]
    return list(
        dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip())
    )


def _body(result: Any, skill_id: str) -> str:
    if not isinstance(result, str):
        return ""
    lines = result.splitlines(keepends=True)
    if lines and lines[0].strip() == f"# Skill: {skill_id}":
        lines.pop(0)
    # Older text presenters had already removed the identity heading, leaving
    # this model-facing carrier at the start of the displayed document.
    if lines and lines[0].startswith(("Skill directory (use as SKILL_ROOT):", "Skill directory:")):
        lines.pop(0)
    return "".join(lines).lstrip("\r\n")


def skill_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Distinguish the main procedure from its references without changing model text."""
    row = structured if isinstance(structured, Mapping) else {}
    skill_id = str(row.get("skill_id") or args.get("skill_id") or "")
    files = requested_skill_files(row if row.get("file") or row.get("files") else args)
    body = _body(result, skill_id)
    heading = next(
        (line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("# ")), ""
    )
    title = str(row.get("skill_title") or "")
    description = str(row.get("skill_description") or "")
    purpose = title if title and title != skill_id else description or heading or skill_id
    file_paths = row.get("file_paths") or {}
    blocks: list[dict[str, Any]] = []
    subject = "skill-purpose"
    if files:
        action = "Read skill reference" if len(files) == 1 else "Read skill references"
        subject = "skill-reference"
        label = files[0] if len(files) == 1 else f"{len(files)} reference files"
        blocks.append({"id": subject, "type": "text", "text": label, "label": label})
        summary = purpose
    else:
        action = "Load skill"
        blocks.append({"id": subject, "type": "text", "text": purpose, "label": purpose})
        summary = "Main procedure"
    if description:
        blocks.append(
            {"id": "skill-description", "type": "text", "label": "When to use", "text": description}
        )
    blocks.append({"id": "skill-identity", "type": "text", "label": "Skill", "text": skill_id})
    directory = row.get("skill_directory")
    if (
        not directory
        and isinstance(row.get("path"), str)
        and (PureWindowsPath(row["path"]).is_absolute() or row["path"].startswith("/"))
    ):
        directory = (
            str(PureWindowsPath(row["path"]).parent)
            if "\\" in row["path"]
            else row["path"].rsplit("/", 1)[0]
        )
    if directory:
        blocks.append(
            {"id": "skill-directory", "type": "text", "label": "Skill directory", "text": directory}
        )
    for index, filename in enumerate(files or ["SKILL.md"]):
        path = file_paths.get(filename) if isinstance(file_paths, Mapping) else None
        if not files:
            path = row.get("path")
        # Older records retained only SKILL.md's path even for a reference read.
        # Do not turn that into an incorrect link to a different file.
        blocks.append(
            {
                "id": f"skill-file-{index}",
                "type": "link",
                "target": "file",
                "uri": path,
                "label": filename,
            }
            if isinstance(path, str)
            and (PureWindowsPath(path).is_absolute() or path.startswith("/"))
            else {
                "id": f"skill-file-{index}",
                "type": "text",
                "label": "File read",
                "text": filename,
            }
        )
    if body:
        blocks.append({"id": "content", "type": "markdown", "text": body})
    return {"action": action, "subject": subject, "summary": summary, "blocks": blocks}


def legacy_skill_presentation(
    part: Mapping[str, Any], args: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Clarify historical reads using their retained inputs and content, never today's files."""
    presentation = part.get("presentation")
    if (
        part.get("tool_name") != "load_skill"
        or part.get("type") != "tool_result"
        or part.get("is_error")
        or not isinstance(presentation, Mapping)
        or presentation.get("subject") != "skill-name"
    ):
        return None
    metadata = part.get("metadata") or {}
    row = part.get("structured_content") or (
        metadata.get("result") if isinstance(metadata, Mapping) else None
    )
    body = next(
        (
            block.get("text", "")
            for block in presentation.get("blocks", [])
            if block.get("id") == "content"
        ),
        "",
    )
    return skill_presentation(args or {}, body, row)
