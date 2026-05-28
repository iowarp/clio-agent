from __future__ import annotations

from pathlib import Path

from clio_agent.prompts import (
    PromptDefinition,
    PromptProfile,
    PromptRegistry,
    PromptSource,
    builtin_prompt_definitions,
    parse_prompt_file,
)


def _builtin(text: str = "builtin text") -> dict[str, PromptDefinition]:
    return {
        "clio.chat": PromptDefinition(
            id="clio.chat",
            title="Chat",
            default_profile="default",
            profiles={
                "default": PromptProfile(
                    name="default",
                    text=text,
                    scope="builtin",
                    checksum="builtin",
                )
            },
        )
    }


def test_external_prompt_overrides_builtin_profile(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    root.mkdir()
    (root / "chat-heavy.md").write_text(
        """---
id: clio.chat
title: Tuned chat
profile: heavy
provider: openai
model: gpt-5.1
---
external heavy prompt
""",
        encoding="utf-8",
    )
    registry = PromptRegistry(
        sources=[PromptSource("global", root)],
        builtins=_builtin(),
        write_root=root,
    )

    resolved = registry.resolve("clio.chat", profile="heavy")

    assert resolved is not None
    assert resolved.text == "external heavy prompt"
    assert resolved.title == "Tuned chat"
    assert resolved.profile == "heavy"
    assert resolved.scope == "global"
    assert resolved.provider == "openai"
    assert resolved.model == "gpt-5.1"
    assert resolved.source_path.endswith("chat-heavy.md")


def test_prompt_resolution_falls_back_to_default_profile(tmp_path: Path) -> None:
    registry = PromptRegistry(
        sources=[PromptSource("global", tmp_path / "missing")],
        builtins=_builtin(),
        write_root=tmp_path,
    )

    resolved = registry.resolve("clio.chat", profile="small_model")

    assert resolved is not None
    assert resolved.profile == "default"
    assert resolved.fallback_profile == "default"
    assert resolved.text == "builtin text"


def test_save_prompt_persists_markdown_and_reloads(tmp_path: Path) -> None:
    registry = PromptRegistry(
        sources=[PromptSource("global", tmp_path)],
        builtins=_builtin(),
        write_root=tmp_path,
    )

    row = registry.save(
        "clio.chat",
        profile="light",
        title="Light chat",
        text="short prompt",
        provider="anthropic",
        model="claude-haiku",
        metadata={"owner": "test"},
    )

    assert (tmp_path / "clio.chat--light.md").exists()
    assert row.profiles["light"].text == "short prompt"
    resolved = registry.resolve("clio.chat", profile="light")
    assert resolved is not None
    assert resolved.text == "short prompt"
    assert resolved.metadata["owner"] == "test"


def test_invalid_prompt_file_is_disabled_not_silently_used(tmp_path: Path) -> None:
    path = tmp_path / "broken.md"
    path.write_text(
        """---
title: Broken
profile: bad/profile
---
""",
        encoding="utf-8",
    )

    row = parse_prompt_file(path, scope="workspace")

    assert row.enabled is False
    assert "missing required frontmatter field: id" in row.validation_errors
    assert "invalid profile; use letters, numbers, underscores, and hyphens" in row.validation_errors
    assert "prompt body is empty" in row.validation_errors


def test_invalid_override_does_not_replace_builtin_prompt(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    root.mkdir()
    (root / "bad-chat.md").write_text(
        """---
id: clio.chat
profile: default
---
""",
        encoding="utf-8",
    )
    registry = PromptRegistry(
        sources=[PromptSource("global", root)],
        builtins=_builtin(),
        write_root=root,
    )

    resolved = registry.resolve("clio.chat")

    assert resolved is not None
    assert resolved.text == "builtin text"
    assert "prompt body is empty" in resolved.validation_errors
    assert resolved.metadata["invalid_sources"][0].endswith("bad-chat.md")


def test_builtin_prompt_profiles_encode_alignment_requirements() -> None:
    builtins = builtin_prompt_definitions()

    for prompt_id in (
        "clio.main.planner",
        "clio.main.answer",
        "clio.chat",
        "clio.expert.data",
        "clio.expert.analysis",
        "clio.expert.visualization",
    ):
        row = builtins[prompt_id]
        assert {
            "default",
            "heavy",
            "light",
            "small_model",
            "fine_tuned",
            "debug",
        }.issubset(row.profiles)
        assert row.metadata["alignment"] == "public_reference_matrix"
        assert row.metadata["requirements"]
        default = row.profiles["default"].text
        heavy = row.profiles["heavy"].text
        small = row.profiles["small_model"].text
        debug = row.profiles["debug"].text
        assert "tool telemetry" in default
        assert "Never claim a tool call" in default
        assert "delegate to scoped child experts" in heavy
        assert "model/provider fallback" in heavy
        assert "valid JSON" in small or "explicit schemas" in small
        assert "prompt id/profile" in debug


def test_prompt_alignment_profile_resolution_keeps_builtin_provenance(tmp_path: Path) -> None:
    registry = PromptRegistry(
        sources=[PromptSource("global", tmp_path / "missing")],
        write_root=tmp_path,
    )

    resolved = registry.resolve("clio.main.planner", profile="small_model")

    assert resolved is not None
    assert resolved.profile == "small_model"
    assert resolved.scope == "builtin"
    assert resolved.metadata["alignment"] == "public_reference_matrix"
    assert resolved.metadata["behavior_profile"] == "small_model"
    assert "Return exactly one JSON object" in resolved.text
    assert "declared tools and experts" in resolved.text
