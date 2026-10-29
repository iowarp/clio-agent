"""The SPOTTER per-item view equals the reference scripts on the real capture."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from clio_schemas.attention import UNIFORM_MEAN

from clio_agent.gact.attention.contract import AttentionStep
from clio_agent.gact.attention.items import (
    CSV_COLUMNS,
    items_csv,
    items_payload,
    reduce_items,
    split_spans,
)
from clio_agent.gact.attention.profiles import ATTN_ANALYTICS, DEFAULT_PROFILE, reduce_steps
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.service import SelectionRequest
from clio_agent.gact.attention.store import AttentionStore
from tests.test_gact.test_attention import spotter_reference as ref
from tests.test_gact.test_attention._support import (
    FIXTURES,
    FixtureRenderer,
    fixture,
    fixture_flowcept,
    fixture_thought,
    fixture_transcript,
)
from tests.test_gact.test_attention.test_service_real_run import _call

CAPTURE = next((FIXTURES / "vllm-attn-9e11e1571c5f").glob("*.safetensors"))


def _view(delimiter: str = ".", end: int | None = None, profile: Any = DEFAULT_PROFILE) -> Any:
    renderer = FixtureRenderer()
    return reduce_items(
        messages=fixture_transcript(),
        calls=[_call()],
        store=AttentionStore(fixture_flowcept()),
        renderer_for=lambda identity: renderer,
        requests=[
            SelectionRequest(
                message_id="msg_asst_1",
                part_id="call_sel",
                field="thought",
                start=0,
                end=end if end is not None else len(fixture_thought()),
            )
        ],
        delimiter=delimiter,
        profile=profile,
    )


def test_default_profile_is_the_spotter_formula() -> None:
    assert DEFAULT_PROFILE is ATTN_ANALYTICS
    assert (ATTN_ANALYTICS.metric, ATTN_ANALYTICS.weighting) == ("max", "exponential")
    assert (ATTN_ANALYTICS.decay_base, ATTN_ANALYTICS.direction) == (0.5, "forward")
    assert ATTN_ANALYTICS.weight_normalization == "none"


def test_each_item_matches_the_reference_aggregate_on_the_real_capture() -> None:
    view = _view()
    assert [item.selection.text[item.selection.start : item.selection.end] for item in view.items][
        :2
    ] == [
        "We have registered the MTA1 time series CSV as an artifact",
        "Now we create an interactive time series surface using the artifact URI and the "
        "confirmed column names (time, east, north, up)",
    ]
    total = view.capture.summary.prompt_tokens
    for item in view.items:
        expected = ref.aggregate(CAPTURE, item.steps, total)
        assert item.steps == list(range(item.steps[0], item.steps[-1] + 1))
        # Exact at the published (CSV) precision: what attn_analytics.py writes.
        assert [f"{x:.6g}" for x in item.scores] == [f"{x:.6g}" for x in expected]
        np.testing.assert_allclose(item.scores, expected, rtol=5e-6, atol=0)
        assert item.scores.max() > 0
    # Items are reduced on their own, not as one union: the first item's first
    # step carries weight 1, which a union over both items would not give it.
    first, second = view.items[0], view.items[1]
    union = ref.aggregate(CAPTURE, first.steps + second.steps, total)
    assert not np.allclose(union, first.scores + second.scores)


def test_lines_match_the_reference_heat_map_levels() -> None:
    view = _view()
    payload = items_payload(view)
    rows = list(csv.DictReader(io.StringIO(items_csv(view, chain_id="c", round_no="1"))))
    encoded = view.capture.encoded
    pieces = iter(encoded.text[a:b] for a, b in encoded.offsets)

    class Tok:
        """Decodes the fixture prompt: whole for the line split, then token by token."""

        def decode(self, ids: list[int]) -> str:
            return encoded.text if len(ids) == len(encoded.ids) else next(pieces)

    reference = ref.render(rows, Tok(), width=88).split("\n")
    body = reference[-len(payload["lines"]) :]
    assert len(payload["lines"]) == len(body)
    for line, drawn in zip(payload["lines"], body, strict=True):
        cells = "".join(ref.RAMP[level] for level in line["levels"])
        assert drawn.startswith(cells + " "), (line["index"], cells, drawn[:12])
    assert any(level == 9 for line in payload["lines"] for level in line["levels"])


def test_csv_has_the_reference_columns_and_one_row_per_item() -> None:
    view = _view()
    rows = list(
        csv.DictReader(
            io.StringIO(items_csv(view, chain_id="exp32", round_no="3", task_type="peak"))
        )
    )
    assert tuple(rows[0]) == CSV_COLUMNS
    assert len(rows) == len(view.items)
    ids = fixture()["prompt"]["ids"]
    for index, row in enumerate(rows):
        assert row["segment_index"] == str(index)
        assert row["prompt_token_ids"].split() == [str(t) for t in ids]
        assert len(row["scores"].split()) == len(ids) == int(row["n_prompt_tokens"])
        assert row["model"] == "ibm-granite/granite-4.2-30b"
        assert (row["chain_id"], row["round_no"], row["task_type"]) == ("exp32", "3", "peak")


def test_payload_marks_sections_and_keeps_items_separate() -> None:
    payload = items_payload(_view(), include_scores=True)
    assert payload["schema"] == "clio.attention.items.v1"
    assert payload["profile"]["name"] == "attn-analytics"
    assert payload["ramp"] == " .:-=+*#%@"
    assert all(len(line["levels"]) == len(payload["items"]) for line in payload["lines"])
    assert {line["domain"] for line in payload["lines"]} >= {"system", "tool_definitions"}
    assert len(payload["items"][0]["scores"]) == payload["prompt_tokens"]
    assert len(payload["prompt_token_ids"]) == payload["prompt_tokens"]


def test_profile_choice_still_applies_per_item() -> None:
    uniform = _view(profile=UNIFORM_MEAN)
    default = _view()
    assert not np.allclose(uniform.items[0].scores, default.items[0].scores)


def test_split_spans_trims_and_drops_empty_pieces() -> None:
    text = "ANSWER: NIL 373; DIVERGES;  ;"
    start = len("ANSWER: ")
    spans = split_spans(text, start, len(text), ";")
    assert [text[a:b] for a, b in spans] == ["NIL 373", "DIVERGES"]
    assert split_spans(text, 0, 7, "") == [(0, 7)]


def test_items_from_one_selection_without_delimiter_is_one_item() -> None:
    view = _view(delimiter="", end=59)
    assert len(view.items) == 1
    assert view.items[0].steps[0] == 133 and view.items[0].steps[-1] == 146


def test_no_entry_slots_are_dropped_not_refused() -> None:
    step = AttentionStep(
        step=0,
        token_index=0,
        pos=np.array([3, -1, -1, 5], dtype=np.int32),
        max=np.array([0.9, 0.0, 0.0, 0.4], dtype=np.float32),
        mean=np.array([0.2, 0.0, 0.0, 0.1], dtype=np.float32),
        residual=0.7,
    )
    reduction = reduce_steps([step], 8, ATTN_ANALYTICS)
    assert reduction.scores[3] == pytest.approx(0.9) and reduction.scores[5] == pytest.approx(0.4)
    assert sum(reduction.retained_steps) == 2
    bad = AttentionStep(**{**step.__dict__, "pos": np.array([3, -2, -1, 5], dtype=np.int32)})
    with pytest.raises(AttentionUnavailable):
        reduce_steps([bad], 8, ATTN_ANALYTICS)


def test_items_route_and_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_gact.test_attention.test_routes import _client, _journal

    readers = {"jsonl": SimpleNamespace(path=_journal(tmp_path)), "flowcept": fixture_flowcept()}
    client = _client(readers, monkeypatch)
    body = {"items": [{"part_id": "call_sel", "field": "thought", "start": 0, "end": 120}]}
    base = "/v1/sessions/sess_fixture/messages/msg_asst_1/attention/items"
    view = client.post(base, json={**body, "split": "."}).json()
    assert view["available"] is True and len(view["items"]) == 2
    assert view["profile"]["name"] == "attn-analytics"
    exported = client.post(f"{base}/export", json={**body, "split": ".", "chain_id": "x"}).json()
    assert exported["rows"] == 2 and exported["csv"].startswith(",".join(CSV_COLUMNS))
    assert exported["csv"].splitlines()[1].startswith("x,")
    refused = client.post(base, json={"items": [{"text": "not in the answer"}]}).json()
    assert refused["available"] is False
    assert client.post(base, json={"items": []}).status_code == 422
