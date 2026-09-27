"""The typed query layer over Flowcept, against the proposed storage contract."""

from __future__ import annotations

import re

import pytest

from clio_agent.gact.attention.contract import base_request_id, parse_step, parse_summary
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.store import AttentionStore
from tests.test_gact.test_attention._support import (
    FakeFlowcept,
    fixture,
    fixture_flowcept,
    step_docs,
    summary_doc,
)

RESPONSE = "chatcmpl-a00d067a9ecb0bf5"


def test_summary_is_joined_by_response_id_prefix() -> None:
    fake = fixture_flowcept()
    summary = AttentionStore(fake).summary_for(RESPONSE)
    assert summary.request_id == fixture()["request_id"]
    assert base_request_id(summary.request_id) == RESPONSE
    assert fake.queries[-1]["used.request_id"] == {"$regex": f"^{re.escape(RESPONSE)}-"}


def test_a_different_response_is_not_found() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        AttentionStore(fixture_flowcept()).summary_for("chatcmpl-0000")
    assert exc.value.reason == "attention_record_not_found"


def test_missing_response_id_is_typed() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        AttentionStore(fixture_flowcept()).summary_for("")
    assert exc.value.reason == "response_id_missing"


def test_failed_query_is_attention_query_failed() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        AttentionStore(FakeFlowcept([], fail=True)).summary_for(RESPONSE)
    assert exc.value.reason == "attention_query_failed"


def test_file_only_record_is_attention_arrays_not_in_store() -> None:
    doc = summary_doc()
    doc["generated"] = {}
    doc["attention_stats"] = {"uri": "file:///scratch/prov/x_g0.safetensors"}
    with pytest.raises(AttentionUnavailable) as exc:
        parse_summary(doc)
    assert exc.value.reason == "attention_arrays_not_in_store"
    assert "file descriptor" in exc.value.detail


def test_capture_error_is_attention_capture_failed() -> None:
    doc = summary_doc()
    doc["generated"] = {}
    doc["attention_stats"] = {"uri": None, "error": "OSError: disk full"}
    with pytest.raises(AttentionUnavailable) as exc:
        parse_summary(doc)
    assert exc.value.reason == "attention_capture_failed"


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda s: s.update(segments=[[0, 10], [11, 13296]], segment_mean_mass=[0.1, 0.2]),
            "gap-free",
        ),
        (lambda s: s.update(token_index_base="sideways"), "token_index_base"),
        (lambda s: s.pop("residual_mean"), "residual_mean"),
        (lambda s: s.update(segment_mean_mass=[0.1]), "segment_mean_mass"),
    ],
)
def test_off_contract_summary_is_malformed(mutate, needle: str) -> None:  # noqa: ANN001
    doc = summary_doc()
    mutate(doc["generated"]["attention_summary"])
    with pytest.raises(AttentionUnavailable) as exc:
        parse_summary(doc)
    assert exc.value.reason == "attention_record_malformed"
    assert needle in exc.value.detail


def test_steps_come_back_in_order_with_real_values() -> None:
    store = AttentionStore(fixture_flowcept())
    steps = store.steps(fixture()["request_id"], 262, 265)
    assert [s.step for s in steps] == [262, 263, 264, 265]
    row = fixture()["steps"]["262"]
    assert steps[0].pos.tolist() == row["pos"]
    assert steps[0].mean.sum() + steps[0].residual == pytest.approx(1.0, abs=1e-3)
    assert steps[0].head is not None and len(steps[0].head) == len(row["pos"])


def test_missing_steps_are_typed() -> None:
    docs = [d for d in step_docs() if d["used"]["step"] != 263]
    store = AttentionStore(FakeFlowcept([summary_doc(), *docs]))
    with pytest.raises(AttentionUnavailable) as exc:
        store.steps(fixture()["request_id"], 262, 265)
    assert exc.value.reason == "attention_steps_missing"
    assert exc.value.context["first_missing"] == 263


def test_step_arrays_must_agree() -> None:
    doc = step_docs()[262]
    doc["generated"]["mean"] = doc["generated"]["mean"][:-4]
    with pytest.raises(AttentionUnavailable) as exc:
        parse_step(doc)
    assert exc.value.reason == "attention_record_malformed"


def test_step_tokens_and_workflow_tokenizer() -> None:
    store = AttentionStore(fixture_flowcept())
    tokens = store.step_tokens(fixture()["request_id"])
    assert len(tokens) == 435 and tokens[0][:2] == (0, 0)
    summary = store.summary_for(RESPONSE)
    assert store.workflow_tokenizer(summary.workflow_id) == "ibm-granite/granite-4.2-30b"
