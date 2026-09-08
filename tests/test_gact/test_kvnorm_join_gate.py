"""Stage-3 kvnorm crosslink gate + response-id capture.

The vLLM response id (``chatcmpl-*``) keys clio's ``ai_model_invocation``
provenance records to vllm-kvnorm's ``kv_token_importance`` stream in one
Flowcept store. The stamp is DOUBLY gated: the explicit ``provenance.kvnorm``
opt-in AND Flowcept among the configured provenance providers — either absent,
nothing is stamped.
"""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.lm.io_logging import _kvnorm_response_id
from clio_agent.provenance_config import kvnorm_join_enabled
from tests._config_layer import set_config


def test_gate_off_by_default() -> None:
    assert kvnorm_join_enabled() is False


def test_gate_requires_flowcept_backend() -> None:
    # kvnorm opted in, but Flowcept is NOT a configured provider -> off.
    set_config("provenance.kvnorm", True)
    set_config("provenance.agentic.providers", ["jsonl"])
    assert kvnorm_join_enabled() is False


def test_gate_on_with_kvnorm_and_flowcept() -> None:
    set_config("provenance.kvnorm", True)
    set_config("provenance.agentic.providers", ["jsonl", "flowcept"])
    assert kvnorm_join_enabled() is True


def test_flowcept_alone_is_not_enough() -> None:
    # Flowcept configured but kvnorm NOT opted in -> off.
    set_config("provenance.agentic.providers", ["flowcept"])
    assert kvnorm_join_enabled() is False


def test_response_id_survives_privacy_metadata() -> None:
    """The join key is correlation METADATA: privacy="metadata" (the default)
    drops the content payload, but the id must still reach the Flowcept record."""

    from clio_agent.gact.provenance.flowcept import (
        FlowceptProvenanceProvider,
        FlowceptProviderConfig,
    )
    from clio_agent.gact.semantic_events import SemanticEvent

    provider = object.__new__(FlowceptProvenanceProvider)
    provider.config = FlowceptProviderConfig(privacy="metadata")
    event = SemanticEvent(
        event_type="lm.call",
        session_id="sess_root",
        workspace_id="ws_science",
        trace_id="trace_1",
        turn_id="turn_1",
        status="completed",
        occurred_at="2026-09-07T12:00:00+00:00",
        actor={"agent_id": "earthscope"},
        payload={"content": "sensitive", "response_id": "chatcmpl-abc123"},
    )
    meta = provider._metadata(event, "wf_1", "camp_1")
    assert meta["clio"]["response_id"] == "chatcmpl-abc123"
    assert "payload" not in meta["clio"]  # content still dropped under privacy=metadata

    # Without a stamped id (kvnorm off), no key appears at all.
    import dataclasses

    event_plain = dataclasses.replace(event, payload={"content": "x"})
    meta_plain = provider._metadata(event_plain, "wf_1", "camp_1")
    assert "response_id" not in meta_plain["clio"]


def test_response_id_from_object_and_dict() -> None:
    resp = SimpleNamespace(id="chatcmpl-80fdc07ecb38f335-80b6501f")
    assert _kvnorm_response_id(resp) == "chatcmpl-80fdc07ecb38f335-80b6501f"
    assert _kvnorm_response_id({"id": "chatcmpl-x"}) == "chatcmpl-x"
    assert _kvnorm_response_id({}) == ""
    assert _kvnorm_response_id(None) == ""
