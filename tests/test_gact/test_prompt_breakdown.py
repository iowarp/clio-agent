"""Actual native request attribution stays separate from provider-measured usage."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from dspy.lm15 import (
    FunctionTool,
    ImagePart,
    Message,
    Request,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

from clio_agent.gact.prompt_breakdown import request_breakdown


def test_native_sections_include_named_tools_and_exact_skill_reference() -> None:
    request = Request(
        model="test",
        system="Agent rules.\n## Skills available to you\n- work-with-ndp: Scientific data\n## Environment\nWindows",
        tools=(FunctionTool(name="load_skill", description="Load a skill"),),
        messages=(
            Message(role="user", parts=(TextPart(text="Analyze these measurements"),)),
            Message(
                role="assistant",
                parts=(
                    ToolCallPart(
                        id="call",
                        name="load_skill",
                        input={
                            "kwargs": {"skill_id": "work-with-ndp", "file": "references/catalog.md"}
                        },
                    ),
                ),
            ),
            Message(
                role="tool",
                parts=(
                    ToolResultPart(
                        id="call", name="load_skill", content=(TextPart(text="Private skill body"),)
                    ),
                ),
            ),
        ),
    )
    result = request_breakdown({"request": request})
    assert result is not None
    by_id = {section.id: section for section in result.sections}
    assert by_id["tools"].items[0].label == "load_skill"
    assert [item.label for item in by_id["skills"].items] == [
        "Skill descriptions",
        "work-with-ndp / references/catalog.md",
    ]
    assert result.tokens_estimated == sum(section.tokens_estimated for section in result.sections)
    assert "Private skill body" not in result.model_dump_json()
    assert {item.label for item in by_id["system"].items} == {"Agent instructions", "Environment"}


def test_media_is_reported_without_counting_base64_as_text() -> None:
    request = Request(
        model="test",
        messages=(
            Message(
                role="user",
                parts=(ImagePart(data=base64.b64encode(b"private-pixels" * 10000).decode()),),
            ),
        ),
    )
    result = request_breakdown({"request": request})
    assert result is not None and result.media_items == 1
    assert result.tokens_estimated == 0
    assert "private-pixels" not in result.model_dump_json()


def test_headings_in_fenced_code_stay_in_their_real_section() -> None:
    result = request_breakdown(
        {
            "messages": [
                {
                    "role": "system",
                    "content": "## Instructions\n```markdown\n## Skills available to you\nnot a catalog\n```\n## Environment\nLinux",
                }
            ]
        }
    )
    assert result is not None
    assert [section.id for section in result.sections] == ["system"]
    assert {item.label for item in result.sections[0].items} == {"Instructions", "Environment"}


def test_absent_request_has_no_fabricated_breakdown() -> None:
    assert request_breakdown({"usage": {"prompt_tokens": 12345}}) is None


def test_legacy_media_is_not_misreported_as_a_large_text_section() -> None:
    result = request_breakdown(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + "A" * 10000},
                        },
                        {"type": "text", "text": "Caption"},
                    ],
                }
            ]
        }
    )
    assert result is not None and result.media_items == 1
    assert result.tokens_estimated == 2


def test_skill_file_lists_identify_each_loaded_reference() -> None:
    request = Request(
        model="test",
        messages=(
            Message(
                role="assistant",
                parts=(
                    ToolCallPart(
                        id="a",
                        name="load_skill",
                        input={
                            "skill_id": "science",
                            "files": ["references/catalog.md", "references/streams.md"],
                        },
                    ),
                ),
            ),
            Message(
                role="tool",
                parts=(
                    ToolResultPart(
                        id="a", name="load_skill", content=(TextPart(text="Loaded procedures"),)
                    ),
                ),
            ),
        ),
    )
    result = request_breakdown({"request": request})
    assert result is not None
    skill = next(section for section in result.sections if section.id == "skills")
    assert skill.items[0].label == "science / references/catalog.md, references/streams.md"


def test_usage_keeps_provider_total_when_section_estimates_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import usage

    lm = SimpleNamespace(
        model="test",
        history=[
            {
                "timestamp": "now",
                "model": "test",
                "usage": {"prompt_tokens": 9000},
                "request": Request(
                    model="test",
                    messages=(Message(role="user", parts=(TextPart(text="Short prompt"),)),),
                ),
            }
        ],
    )
    monkeypatch.setattr(usage, "_all_known_lms", lambda app: [lm])
    result = usage._last_prompt_usage_from_history_slice({id(lm): 0}, SimpleNamespace())
    assert result["used_tokens"] == 9000 and result["source"] == "provider"
    assert result["prompt_breakdown"]["tokens_estimated"] < 9000


def test_usage_metadata_retains_only_the_matching_models_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.turn_usage import context_usage_metadata_patch

    monkeypatch.setattr(
        "clio_agent.gact.runtime.context_tokens._resolve_expert_context_window", lambda cfg: 12345
    )
    state = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                agent=SimpleNamespace(_provider_config=SimpleNamespace(model="chosen"))
            )
        ),
        last_prompt_usage={"model": "provider/chosen", "used_tokens": 100},
        invocation_agent_id="main",
        active_agent_id="main",
        turn_id="turn",
    )
    patch = context_usage_metadata_patch(
        state, SimpleNamespace(metadata={}, agent={}), SimpleNamespace(updated_at="now")
    )
    assert patch is not None and patch["context_usage_by_scope"]["main"]["window_tokens"] == 12345
    state.last_prompt_usage["model"] = "provider/other"
    patch = context_usage_metadata_patch(
        state, SimpleNamespace(metadata={}, agent={}), SimpleNamespace(updated_at="now")
    )
    assert patch is not None and "window_tokens" not in patch["context_usage_by_scope"]["main"]
