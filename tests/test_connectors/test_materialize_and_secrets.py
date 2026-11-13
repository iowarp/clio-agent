"""Saved connections, secrets and the person's choices (CN2, CN8, CN10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent import paths
from clio_agent.connectors import entries, materialize, secrets, state
from clio_agent.tools.mcp_config import spec_from_declaration


def test_a_secret_stays_a_placeholder_until_the_server_starts(fixture_catalog: Path) -> None:
    found = entries.load_catalog().entries["fixture-secret"]
    declared = materialize.declaration(found, {"where": "node08"})
    assert declared["connection_only"] is True and declared["connector"] == "fixture-secret"
    assert declared["env"] == {"FIXTURE_TOKEN": "${need.token}", "FIXTURE_WHERE": "node08"}
    secrets.save("fixture-secret", {"token": "tok-${odd}-value"})
    spec = spec_from_declaration("fixture-secret", declared)
    assert spec.usable and spec.connector == "fixture-secret"
    assert spec.env["FIXTURE_TOKEN"] == "tok-${odd}-value"
    assert "tok-" not in repr(spec)
    store = paths.user_config_dir() / "mcp_oauth_tokens.json"
    assert (store.stat().st_mode & 0o777) == 0o600
    assert "tok-${odd}-value" in json.dumps(json.loads(store.read_text()))
    secrets.save("fixture-secret", {"token": None})
    assert spec_from_declaration("fixture-secret", declared).env["FIXTURE_TOKEN"] == ""


def test_a_sign_in_need_adds_managed_oauth_for_the_connection(fixture_catalog: Path) -> None:
    found = entries.load_catalog().entries["fixture-signin"]
    declared = materialize.declaration(found, {"address": "http://127.0.0.1:9/mcp"})
    assert declared["url"] == "http://127.0.0.1:9/mcp"
    assert declared["auth"]["connection_id"] == "fixture-signin"
    assert declared["auth"]["require_existing_tokens"] is True


def test_choices_and_scope(fixture_catalog: Path) -> None:
    assert state.choice("fixture-plain") == state.Choice()
    state.update("fixture-plain", enabled=True, scope=state.normalize_scope("all"))
    state.update("fixture-secret", enabled=True, scope={"agents": ["clio/standard"]})
    state.update("fixture-secret", values={"where": "h", "other": None})
    assert state.choice("fixture-secret").values == {"where": "h"}
    assert state.choice("fixture-plain").applies_to(("user/mine",))
    assert state.choice("fixture-secret").applies_to(("clio/standard",))
    assert state.choice("fixture-secret").applies_to(("standard",))
    assert not state.choice("fixture-secret").applies_to(("clio/other",))
    with pytest.raises(state.ConnectorStateError):
        state.normalize_scope({"agents": []})
    state.save_custom(
        {
            "id": "custom-one",
            "name": "Custom",
            "description": "d",
            "server": {"command": "srv"},
            "needs": [],
        }
    )
    assert state.all_entries()["custom-one"].source == "custom"
    state.forget("custom-one")
    assert "custom-one" not in state.all_entries()


def test_a_rebound_spec_takes_the_new_values_and_keeps_the_plugin_folders(
    fixture_catalog: Path,
) -> None:
    from clio_agent.gact.connector_rebind import rebound_spec

    found = entries.load_catalog().entries["fixture-secret"]
    before = spec_from_declaration(
        "fixture-secret",
        {
            **materialize.declaration(found, {"where": "here"}),
            "env": {"FIXTURE_WHERE": "here", "PLUGIN_ROOT": "/p", "PLUGIN_DATA": "/d"},
        },
        source="plugin:fixture-secret-skills",
    )
    after = rebound_spec(
        before, materialize.declaration(found, {"where": "node08"}), "fixture-secret"
    )
    assert after.env["FIXTURE_WHERE"] == "node08"
    assert after.env["PLUGIN_ROOT"] == "/p" and after.env["PLUGIN_DATA"] == "/d"
    assert after.source == "plugin:fixture-secret-skills" and after.connector == "fixture-secret"
