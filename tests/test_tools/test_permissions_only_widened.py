"""An approved call survives grants added elsewhere, never a narrowing change."""

from clio_agent.tools.task_call_context import permissions_only_widened

BASE = {
    "mode": "default",
    "approval_mode": "ask",
    "approval_profile": None,
    "playbook_allowed": [],
    "policies": [{"scope": "global", "tool_name_pattern": "rm", "action": "deny"}],
}


def _with(**changes):
    return {**BASE, **changes}


def test_unchanged_holds():
    assert permissions_only_widened(BASE, dict(BASE))


def test_added_allow_rule_holds():
    grant = {"scope": "session", "scope_id": "s", "tool_name_pattern": "fetch", "action": "allow"}
    assert permissions_only_widened(BASE, _with(policies=[*BASE["policies"], grant]))


def test_added_deny_rule_narrows():
    deny = {"scope": "session", "scope_id": "s", "tool_name_pattern": "*", "action": "deny"}
    assert not permissions_only_widened(BASE, _with(policies=[*BASE["policies"], deny]))


def test_removed_rule_needs_fresh_approval():
    assert not permissions_only_widened(BASE, _with(policies=[]))


def test_posture_change_narrows():
    assert not permissions_only_widened(BASE, _with(mode="plan"))
