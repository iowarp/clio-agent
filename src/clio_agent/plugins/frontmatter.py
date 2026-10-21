"""YAML frontmatter for skills, fragments, commands and subagent prompts."""

from __future__ import annotations

from typing import Any

import yaml


class FrontmatterError(ValueError):
    """The frontmatter block is present but is not a YAML mapping."""


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return ``(metadata, body)``; ``metadata`` is ``None`` when there is no block."""

    if not text.startswith("---"):
        return None, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            block = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            try:
                data = yaml.safe_load(block) if block.strip() else {}
            except yaml.YAMLError as exc:
                raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise FrontmatterError("frontmatter must be a mapping")
            return data, body
    raise FrontmatterError("frontmatter block is not closed with ---")
