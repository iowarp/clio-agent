"""An agent's prompt keeps only the fragments its final tools need."""

from __future__ import annotations

from clio_agent.plugins.compose_final import final_prompt_text

_PROMPT = {
    "sections": [
        {"section": "identity", "fragments": [{"id": "a.identity", "text": "You are A."}]},
        {
            "section": "capabilities",
            "fragments": [
                {
                    "id": "d.parent",
                    "text": "Delegate wide sweeps.",
                    "when_tools": ["spawn_agent_task"],
                },
                {"id": "s.shell", "text": "Prefer gh.\n\nUse releases.", "when_tools": ["shell"]},
                {"id": "c.child", "text": "Report back.", "session": "delegated"},
                {"id": "c.person", "text": "Draw views.", "session": "root"},
            ],
        },
    ]
}
_FULL = "\n\n".join(
    [
        "You are A.",
        "Delegate wide sweeps.",
        "Prefer gh.\n\nUse releases.",
        "Report back.",
        "Draw views.",
    ]
)


def test_a_child_without_spawn_tools_is_not_told_to_delegate() -> None:
    text = final_prompt_text(_FULL, _PROMPT, ["shell_bash", "fs_read_file"], delegated=True)
    assert text == "You are A.\n\nPrefer gh.\n\nUse releases.\n\nReport back."


def test_a_namespace_entry_matches_its_tools_and_the_root_keeps_its_scope() -> None:
    text = final_prompt_text(_FULL, _PROMPT, ["spawn_agent_task", "shell_bash"], delegated=False)
    assert (
        text == "You are A.\n\nDelegate wide sweeps.\n\nPrefer gh.\n\nUse releases.\n\nDraw views."
    )


def test_a_text_with_more_around_it_loses_only_the_dropped_fragments() -> None:
    held = _FULL + "\n\nA note from an older basis."
    text = final_prompt_text(held, _PROMPT, [], delegated=False)
    assert "Delegate" not in text and "Prefer gh" not in text and "Use releases" not in text
    assert text.endswith("Draw views.\n\nA note from an older basis.")


def test_deferred_tools_count_as_callable() -> None:
    # Deferred tools are passed with the listed ones: they stay callable once searched.
    text = final_prompt_text(_FULL, _PROMPT, ["spawn_agent_task"], delegated=False)
    assert "Delegate wide sweeps." in text


def test_the_fragment_budget_binds_third_party_plugins_only() -> None:
    from clio_agent.plugins import service, store
    from clio_agent.plugins.compose import PRODUCT_MARKETPLACES, compose
    from clio_agent.plugins.records import FragmentRecord

    assert {store.BUILTIN_MARKETPLACE, service.DEFAULT_MARKETPLACE} == PRODUCT_MARKETPLACES
    long = FragmentRecord(id="x.long", section="capabilities", body="word " * 400, path="")
    composed = compose(
        identity={"id": "a", "name": "A"},
        fragments=[("core", long), ("third", long)],
        tools=[],
        plugins=["core", "third"],
        budget_exempt=frozenset({"core"}),
    )
    assert len(composed.warnings) == 1 and "x.long" in composed.warnings[0]
