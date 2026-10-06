"""The descriptor contract, the file locator/verifier and the SafeTensors reader (real data)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from clio_agent.gact.attention import files
from clio_agent.gact.attention.byte_source import LocalFile
from clio_agent.gact.attention.contract import base_request_id, parse_record
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.safetensors_file import SafeTensorsFile
from clio_agent.gact.attention.store import AttentionStore
from tests.test_gact.test_attention._support import (
    FIXTURES,
    FakeFlowcept,
    descriptor_doc,
    fixture,
    fixture_flowcept,
    request_id,
)

RESPONSE = "chatcmpl-9478ecf2002ea26e"
FILE = FIXTURES / "vllm-attn-9e11e1571c5f" / f"{RESPONSE}-9b6ebc3a_g0.safetensors"


def test_base_request_id_strips_the_vllm_suffix_and_group() -> None:
    assert base_request_id(f"{RESPONSE}-9b6ebc3a:g0") == RESPONSE
    assert base_request_id(f"{RESPONSE}-9b6ebc3a") == RESPONSE


def test_record_parses_the_real_descriptor() -> None:
    record = parse_record(descriptor_doc())
    assert record.request_id == request_id()
    assert record.prompt_tokens == 15514 and record.decode_steps == 329
    assert record.uri.startswith("file:///work/nvme/") and record.segment_mode == "fixed"
    assert record.clean and set(record.health) == {
        "decode_steps_dropped",
        "decode_steps_unscored",
        "decode_steps_nonfinite",
        "restarts",
    }


def test_failed_write_is_attention_capture_failed() -> None:
    doc = descriptor_doc(attention_stats={"uri": None, "error": "disk full"})
    with pytest.raises(AttentionUnavailable) as exc:
        parse_record(doc)
    assert exc.value.reason == "attention_capture_failed"
    assert "disk full" in exc.value.detail


def test_non_safetensors_format_is_malformed() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        parse_record(descriptor_doc(attention_stats={"format": "npz"}))
    assert exc.value.reason == "attention_record_malformed"


def test_reader_reads_whole_tensors_and_row_ranges_by_offset() -> None:
    st = SafeTensorsFile(LocalFile(FILE))
    assert st.metadata["request_id"] == request_id()
    assert st.metadata["head_code"] == "layer * 32 + head"
    ids = st.read("prompt_token_ids")
    assert ids.shape == (15514,) and ids.tolist() == fixture()["prompt"]["ids"]
    whole = st.read("topk_pos")
    rows = st.rows("topk_pos", 133, 146)
    assert rows.shape == (14, 64)
    assert np.array_equal(rows, whole[133:147])
    assert (np.diff(rows, axis=1) > 0).all(), "positions ascend within a row"
    assert (rows > 0).all(), "the sink (position 0) is never selected"


def test_rows_outside_the_tensor_are_malformed() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        SafeTensorsFile(LocalFile(FILE)).rows("topk_pos", 300, 329)
    assert exc.value.reason == "attention_record_malformed"


def test_truncated_file_is_malformed(tmp_path: Path) -> None:
    broken = tmp_path / "cut.safetensors"
    broken.write_bytes(FILE.read_bytes()[:50_000])
    with pytest.raises(AttentionUnavailable) as exc:
        SafeTensorsFile(LocalFile(broken)).read("val_all_avg")
    assert exc.value.reason == "attention_record_malformed"


def test_locate_reroots_the_node_path_under_the_files_dir(tmp_path: Path) -> None:
    record = parse_record(descriptor_doc())
    located = files.locate(record, str(FIXTURES))
    assert isinstance(located, LocalFile) and located.path == FILE
    with pytest.raises(AttentionUnavailable) as exc:
        files.locate(record, str(tmp_path))
    assert exc.value.reason == "attention_file_unavailable"
    assert str(tmp_path) in " ".join(exc.value.context["tried"])


def test_locate_without_a_mirror_says_how_to_configure_one() -> None:
    record = parse_record(descriptor_doc())
    with pytest.raises(AttentionUnavailable) as exc:
        files.locate(record, "")
    assert "provenance.attention.files_dir" in exc.value.detail


def test_a_different_file_is_attention_file_mismatch(tmp_path: Path) -> None:
    record = parse_record(descriptor_doc())
    copy = tmp_path / "vllm-attn-9e11e1571c5f" / FILE.name
    copy.parent.mkdir()
    data = bytearray(FILE.read_bytes())
    data[-1] ^= 0xFF
    copy.write_bytes(bytes(data))
    with pytest.raises(AttentionUnavailable) as exc:
        files.verify(record, LocalFile(copy))
    assert exc.value.reason == "attention_file_mismatch"
    files.verify(record, LocalFile(FILE))


def test_store_summary_cross_checks_record_and_file() -> None:
    store = AttentionStore(fixture_flowcept())
    summary = store.summary_for(RESPONSE)
    assert summary.request_id == request_id()
    assert summary.segments[0] == (0, 128) and summary.segments[-1][1] == 15514
    assert summary.attn_sum.shape == (15514,) and summary.top_pct == 10.0
    steps = store.steps(summary, 133, 135)
    assert [s.step for s in steps] == [133, 134, 135]
    assert [s.token_index for s in steps] == [133, 134, 135]
    assert all(0 < s.residual < 1 for s in steps)


def test_record_and_file_disagreeing_on_steps_is_malformed() -> None:
    doc = descriptor_doc()
    doc["used"] = {**doc["used"], "num_decode_tokens": 330}
    with pytest.raises(AttentionUnavailable) as exc:
        AttentionStore(FakeFlowcept([doc])).summary_for(RESPONSE)
    assert exc.value.reason == "attention_record_malformed"


def test_missing_record_and_failed_query_are_typed() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        AttentionStore(FakeFlowcept([])).summary_for(RESPONSE)
    assert exc.value.reason == "attention_record_not_found"
    with pytest.raises(AttentionUnavailable) as exc:
        AttentionStore(FakeFlowcept([], fail=True)).summary_for(RESPONSE)
    assert exc.value.reason == "attention_query_failed"


def test_workflow_tokenizer_comes_from_the_connector_workflow() -> None:
    store = AttentionStore(fixture_flowcept())
    assert store.workflow_tokenizer("vllm-attn-9e11e1571c5f") == "ibm-granite/granite-4.2-30b"
