"""Shipped network rules seed once, as ordinary rules the person can remove for good."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.runtime.default_network_rules import (
    DEFAULT_NETWORK_RULES,
    DefaultNetworkRule,
    seed_default_network_rules,
)
from clio_agent.gact.runtime.grant_resolver import next_append_priority
from clio_agent.gact.runtime.permission_policies import (
    _flush_permission_policies,
    _host_action_for,
    _load_permission_policies,
)


def _app(path) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            permission_policies_path=path, permission_policies=_load_permission_policies(path)
        )
    )


def test_defaults_seed_once_and_a_removed_default_stays_removed(tmp_path) -> None:
    path = tmp_path / "permission_policies.json"
    app = _app(path)
    assert seed_default_network_rules(app) == [rule.id for rule in DEFAULT_NETWORK_RULES]
    assert _host_action_for(app, workspace_id="ws_any", host="github.com", port=22) == "allow"
    assert _host_action_for(app, workspace_id="ws_any", host="github.com", port=80) == ""
    assert _host_action_for(app, workspace_id="ws_any", host="example.test", port=443) == ""

    # The person removes the SSH default in Settings (PUT replaces the list, then flushes).
    app.state.permission_policies = [
        p for p in app.state.permission_policies if p["metadata"]["default_id"] != "github-ssh"
    ]
    _flush_permission_policies(app)

    restarted = _app(path)
    assert seed_default_network_rules(restarted) == []
    assert _host_action_for(restarted, workspace_id="w", host="github.com", port=22) == ""
    assert len(restarted.state.permission_policies) == len(DEFAULT_NETWORK_RULES) - 1

    newer = (*DEFAULT_NETWORK_RULES, DefaultNetworkRule("new", "new.test:443", "https", "x"))
    assert seed_default_network_rules(_app(path), newer) == ["new"]


def test_the_persons_later_answer_outranks_a_shipped_rule(tmp_path) -> None:
    app = _app(tmp_path / "permission_policies.json")
    seed_default_network_rules(app)
    deny: dict[str, Any] = {
        "scope": "workspace",
        "scope_id": "w",
        "action": "deny",
        "host_pattern": "github.com",
    }
    deny["priority"] = next_append_priority(app.state.permission_policies)  # an ask's answer
    app.state.permission_policies.append(deny)
    assert _host_action_for(app, workspace_id="w", host="github.com", port=22) == "deny"
    assert _host_action_for(app, workspace_id="other", host="github.com", port=22) == "allow"


def test_an_unreadable_policy_file_is_not_overwritten(tmp_path) -> None:
    path = tmp_path / "permission_policies.json"
    path.write_text("{not json", encoding="utf-8")
    assert seed_default_network_rules(_app(path)) == []
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_seeded_record_survives_a_save_by_a_fresh_app(tmp_path) -> None:
    path = tmp_path / "permission_policies.json"
    seed_default_network_rules(_app(path))
    fresh = _app(path)  # a later process that never seeded
    fresh.state.permission_policies = []
    _flush_permission_policies(fresh)
    assert set(json.loads(path.read_text(encoding="utf-8"))["seeded_default_rules"]) == {
        rule.id for rule in DEFAULT_NETWORK_RULES
    }
