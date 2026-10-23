"""Section estimates from the actual last request, separate from provider usage."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field


class PromptSectionItem(BaseModel):
    """A named prompt contribution; never a second copy of its private body."""

    label: str
    tokens_estimated: int = Field(ge=0)


class PromptSection(BaseModel):
    """One stable display category with inspectable contributions."""

    id: str
    label: str
    tokens_estimated: int = Field(ge=0)
    count: int = Field(ge=0)
    items: list[PromptSectionItem] = Field(default_factory=list)


class PromptBreakdown(BaseModel):
    """Attributed text/schema estimates, not purported provider-measured sections."""

    method: Literal["characters_div_4"] = "characters_div_4"
    tokens_estimated: int = Field(ge=0)
    media_items: int = Field(default=0, ge=0)
    sections: list[PromptSection] = Field(default_factory=list)


LABELS = {
    "system": "System prompt",
    "tools": "Tool definitions",
    "skills": "Skills",
    "messages": "Messages",
    "results": "Tool results",
    "reasoning": "Reasoning",
}


def _value(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, Mapping) and value.get("type") in {
        "image_url",
        "input_image",
        "image",
        "document",
        "input_audio",
        "audio",
        "video",
    }:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, Mapping) and value.get("text") is not None:
        return str(value["text"])
    text = getattr(value, "text", None)
    return str(text) if text is not None else json.dumps(value, default=str, ensure_ascii=False)


def _system_sections(text: str) -> list[tuple[str, str]]:
    """Split real headings without treating headings inside fenced code as sections."""
    sections: list[tuple[str, str]] = []
    title, fence = "Agent instructions", ""
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            kind = marker[1][0]
            fence = "" if fence == kind else kind if not fence else fence
        heading = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", line) if not fence else None
        if heading:
            if lines:
                sections.append((title, "".join(lines)))
            title, lines = heading[1][:140], []
        lines.append(line)
    if lines:
        sections.append((title, "".join(lines)))
    return sections


def request_breakdown(entry: Mapping[str, Any]) -> PromptBreakdown | None:
    """Count the actual request's system sections, tools, messages and loaded skills.

    The total stays independent of provider usage. Missing attribution is never
    invented, and attached media is counted but has no fabricated token cost.
    """
    request = entry.get("request")
    messages = _value(request, "messages") if request is not None else entry.get("messages")
    if not isinstance(messages, (list, tuple)):
        return None
    groups: dict[str, list[PromptSectionItem]] = {key: [] for key in LABELS}
    media = 0
    calls: dict[str, Any] = {}

    def add(category: str, label: str, text: str) -> None:
        if text:
            groups[category].append(
                PromptSectionItem(label=label, tokens_estimated=max(1, (len(text) + 3) // 4))
            )

    def system(text: str) -> None:
        for label, content in _system_sections(text):
            category = (
                "skills"
                if label
                in {
                    "Skills available to you",
                    "Skills declared by this expert (follow these procedures)",
                }
                else "system"
            )
            add(category, "Skill descriptions" if category == "skills" else label, content)

    if request is not None:
        system(_text(_value(request, "system", "")))
    tools = (
        _value(request, "tools", ())
        if request is not None
        else (entry.get("kwargs") or {}).get("tools", ())
    )
    for tool in tools or ():
        function = _value(tool, "function", tool)
        name = str(_value(function, "name", "Tool"))
        schema = {key: _value(function, key) for key in ("name", "description", "parameters")}
        add("tools", name, json.dumps(schema, ensure_ascii=False, default=str))
    for message in messages:
        role = str(_value(message, "role", "user"))
        parts = _value(message, "parts")
        if parts is None:
            content = _value(message, "content", "")
            if isinstance(content, (list, tuple)):
                media += sum(
                    isinstance(item, Mapping)
                    and item.get("type")
                    in {
                        "image_url",
                        "input_image",
                        "image",
                        "document",
                        "input_audio",
                        "audio",
                        "video",
                    }
                    for item in content
                )
            if role in {"system", "developer"}:
                system(_text(content))
            else:
                add(
                    "results" if role == "tool" else "messages",
                    "Tool response"
                    if role == "tool"
                    else {"user": "User messages", "assistant": "Assistant messages"}.get(
                        role, "Messages"
                    ),
                    _text(content),
                )
            continue
        for part in parts:
            kind = _value(part, "type", "text")
            if kind in {"image", "document", "audio", "video"}:
                media += 1
                continue
            if kind == "tool_call":
                calls[str(_value(part, "id", ""))] = part
                add(
                    "messages",
                    "Tool calls",
                    json.dumps(
                        {"name": _value(part, "name"), "input": _value(part, "input", {})},
                        default=str,
                        ensure_ascii=False,
                    ),
                )
            elif kind == "tool_result":
                name = str(_value(part, "name", "Tool response"))
                text_parts = _value(part, "content", ())
                media += sum(
                    _value(item, "type") in {"image", "document", "audio", "video"}
                    for item in text_parts
                )
                text = "\n".join(
                    _text(item)
                    for item in text_parts
                    if _value(item, "type") not in {"image", "document", "audio", "video"}
                )
                if name == "load_skill":
                    args = _value(calls.get(str(_value(part, "id", ""))), "input", {})
                    args = args.get("kwargs", args) if isinstance(args, Mapping) else {}
                    label = str(args.get("skill_id") or "Loaded skill")
                    references = [str(args["file"])] if args.get("file") else []
                    if isinstance(args.get("files"), list):
                        references.extend(str(path) for path in args["files"] if path)
                    if references:
                        label += " / " + ", ".join(dict.fromkeys(references))
                    add("skills", label[:240], text)
                else:
                    add("results", name, text)
            elif kind == "thinking":
                add("reasoning", "Retained reasoning", _text(part))
            elif role in {"system", "developer"}:
                system(_text(part))
            else:
                add(
                    "messages",
                    "User messages" if role == "user" else "Assistant messages",
                    _text(part),
                )
    sections = []
    for key, items in groups.items():
        if not items:
            continue
        merged: dict[str, int] = {}
        for item in items:
            merged[item.label] = merged.get(item.label, 0) + item.tokens_estimated
        sections.append(
            PromptSection(
                id=key,
                label=LABELS[key],
                tokens_estimated=sum(merged.values()),
                count=len(items),
                items=[
                    PromptSectionItem(label=label, tokens_estimated=tokens)
                    for label, tokens in sorted(merged.items(), key=lambda pair: -pair[1])
                ],
            )
        )
    return PromptBreakdown(
        tokens_estimated=sum(section.tokens_estimated for section in sections),
        media_items=media,
        sections=sections,
    )
