"""The public SDK: agent specs, plugin references and flow selection."""

from __future__ import annotations

import pytest

from clio_agent.sdk import (
    Agent,
    AgentDefinitionError,
    AgentSpec,
    Flow,
    Pack,
    Plugin,
    Requires,
    hook,
)
from clio_agent.sdk.versions import PluginRef, satisfies


class Science(Agent):
    id = "science"
    version = "1.0.0"
    role = "a science agent"
    plugins = Agent.plugins + ["documents", "shell"]


class Factorio(Science):
    id = "factorio"
    role = "a manufacturing and topology optimization agent"
    plugins = Science.plugins + ["manufacturing@^1"]
    flow = Flow("campaign", max_cycles=5)
    requires = Requires(tool_calling=True)


def test_spec_flattens_class_chain_with_layers() -> None:
    spec = Factorio.spec()
    assert [(str(p.ref), p.layer) for p in spec.plugins] == [
        ("core", "agent"),
        ("documents", "science"),
        ("shell", "science"),
        ("manufacturing@^1", "factorio"),
    ]
    assert spec.layers == ("agent", "science", "factorio")
    assert spec.flow == {"id": "campaign", "options": {"max_cycles": 5}}
    assert spec.requires == {"tool_calling": True}
    assert AgentSpec.from_dict(spec.to_dict()) == spec


def test_spec_checksum_is_stable_and_tamper_evident() -> None:
    data = Factorio.spec().to_dict()
    assert data["checksum"] == Factorio.spec().checksum
    data["role"] = "tampered"
    with pytest.raises(AgentDefinitionError, match="checksum"):
        AgentSpec.from_dict(data)


def test_agent_must_carry_core_and_have_an_id() -> None:
    class NoCore(Agent):
        id = "nocore"
        plugins = ["shell"]

    class NoId(Agent):
        pass

    with pytest.raises(AgentDefinitionError, match="core"):
        NoCore.spec()
    with pytest.raises(AgentDefinitionError, match="id"):
        NoId.spec()


def test_plugin_and_pack_declarations() -> None:
    documents = Pack("documents", ["pdf", "docx", "xlsx@^1"])
    assert documents.describe()["members"] == ["pdf", "docx", "xlsx@^1"]

    class Manufacturing(Plugin):
        id = "manufacturing"
        version = "1.2.0"
        requires = ["shell", "a2a"]

    assert Manufacturing().describe()["requires"] == ["shell", "a2a"]


def test_version_ranges() -> None:
    assert satisfies("1.4.2", "^1") and not satisfies("2.0.0", "^1")
    assert satisfies("0.3.9", "^0.3") and not satisfies("0.4.0", "^0.3")
    assert satisfies("1.2.9", "~1.2") and not satisfies("1.3.0", "~1.2")
    assert satisfies("1.5.0", ">=1.2, <2")
    assert PluginRef.parse("manufacturing@^1") == PluginRef("manufacturing", "^1")
    with pytest.raises(ValueError):
        PluginRef.parse("Bad")


def test_hook_rejects_unknown_events() -> None:
    with pytest.raises(ValueError, match="unknown hook event"):
        hook("on_everything")
    spec = hook("tool_result", after="core.audit")(lambda ev: None)
    assert spec.after == ("core.audit",) and spec.event == "tool_result"
