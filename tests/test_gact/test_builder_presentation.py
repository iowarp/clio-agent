"""What a reader sees for make_plugin and for a spawn that changes a child's plugins."""

from __future__ import annotations

from clio_agent.gact.agents.native_presenters_tasks import present_task_family
from clio_agent.gact.builder.presentation import make_plugin_presentation

DIFF = (
    "--- a/skills/x/SKILL.md\n+++ b/skills/x/SKILL.md\n@@ -1 +1 @@\n-Count once.\n+Count twice.\n"
)


def test_a_first_version_names_its_parts_and_trust() -> None:
    result = {
        "installed": True,
        "plugin": "hdf5-units",
        "version": 1,
        "tools": ["to_kelvin"],
        "flows": [],
        "hooks": ["protect_raw"],
        "executable": True,
        "trusted": False,
        "warnings": [
            {"code": "fragment_budget", "message": "about 190 tokens", "severity": "warning"}
        ],
    }
    view = make_plugin_presentation({"name": "hdf5-units"}, result, None)
    assert view["action"] == "Write plugin" and view["summary"] == "Version 1, check passed"
    texts = {b["id"]: b for b in view["blocks"]}
    assert texts["plugin"]["text"] == "hdf5-units"
    assert texts["parts"]["text"] == "Tool to_kelvin\nHook protect_raw"
    assert texts["trust"]["text"] == "Asked about once, when first used"
    assert (
        texts["warning-0"]["type"] == "check" and texts["warning-0"]["label"] == "fragment_budget"
    )


def test_a_new_version_shows_what_changed_as_a_diff() -> None:
    result = {
        "installed": True,
        "plugin": "hdf5-units",
        "version": 2,
        "changed": ["skills/x/SKILL.md"],
        "diff": DIFF,
        "executable": False,
        "trusted": True,
    }
    view = make_plugin_presentation({}, result, None)
    blocks = {b["id"]: b for b in view["blocks"]}
    assert blocks["changes"]["label"] == "Version 2"
    assert blocks["changes"]["text"] == "Changed skills/x/SKILL.md"
    assert blocks["diff"] == {"id": "diff", "type": "diff", "text": DIFF}
    assert blocks["trust"]["text"] == "Text only: no trust step"


def test_a_failed_check_lists_each_diagnostic_as_a_failed_check() -> None:
    result = {
        "installed": False,
        "reason": "check_failed",
        "version": 1,
        "check": {
            "diagnostics": [
                {"code": "prompt_file_missing", "message": "prompt/x.md", "severity": "error"}
            ]
        },
    }
    view = make_plugin_presentation({"name": "x"}, result, None)
    assert view["status"] == "failed" and view["summary"] == "Check failed"
    check = next(b for b in view["blocks"] if b["type"] == "check")
    assert check["status"] == "failed" and check["label"] == "prompt_file_missing"


def test_a_refused_name_says_why() -> None:
    result = {"installed": False, "reason": "name_taken_by_person", "error": "'notes' is theirs"}
    view = make_plugin_presentation({"name": "notes"}, result, None)
    assert view["status"] == "failed" and view["summary"] == "Name taken by person"
    assert view["blocks"][-1]["text"] == "'notes' is theirs"


def test_a_spawn_names_the_plugins_it_gave_the_child() -> None:
    args = {"task": "count", "name": "trial with", "plugins": {"add": ["private-local/tally"]}}
    result = {"accepted": True, "handle": "task_1", "kind": "Subagent", "status": "running"}
    _, blocks, *_ = present_task_family("tasks", args, result, result)
    plugin = next(b for b in blocks if b["id"].startswith("plugins-"))
    assert plugin["label"] == "Plugins for trial with"
    assert plugin["text"] == "Added private-local/tally"


def test_a_parallel_spawn_names_each_childs_plugins() -> None:
    args = {
        "spawns": [
            {"task": "a", "name": "with", "plugins": {"add": ["private-local/tally"]}},
            {"task": "a", "name": "without"},
            {"task": "a", "name": "old", "plugins": {"remove": ["tally"]}},
        ]
    }
    result = {"spawned": [], "outcomes": []}
    _, blocks, *_ = present_task_family("tasks", args, result, result)
    rows = [(b["label"], b["text"]) for b in blocks if b["id"].startswith("plugins-")]
    assert rows == [
        ("Plugins for with", "Added private-local/tally"),
        ("Plugins for old", "Removed tally"),
    ]
