"""Unit tests for the unified LM token highway field extractor (#693)."""

from __future__ import annotations

from clio_agent.runtime.lm_stream import (
    AnswerFieldExtractor,
    extract_delta,
    normalize_escaped_section_boundaries,
)


def _chunked(text: str, sizes: list[int]) -> list[str]:
    out, i, k = [], 0, 0
    while i < len(text):
        n = sizes[k % len(sizes)]
        out.append(text[i : i + n])
        i += n
        k += 1
    return out


def test_extractor_pulls_only_answer_field_across_chunk_splits():
    full = (
        "[[ ## reasoning ## ]]\nthinking hard\n"
        "[[ ## answer ## ]]\nHello world, the result.\n"
        '[[ ## workflow_state ## ]]\n{"a": 1}\n[[ ## completed ## ]]'
    )
    ex = AnswerFieldExtractor("answer")
    out = "".join(ex.feed(c) for c in _chunked(full, [1, 4, 9, 2, 7, 3])) + ex.flush()
    assert out.strip() == "Hello world, the result."
    assert "[[" not in out and "##" not in out  # no markers leaked
    assert "thinking" not in out  # reasoning not leaked


def test_extractor_handles_all_contract_fields_without_marker_leaks():
    full = (
        "[[ ## reasoning ## ]]\nroute to geo\n"
        "[[ ## next_thought ## ]]\nneed coordinates\n"
        "[[ ## next_expert ## ]]\ngeospatial\n"
        "[[ ## next_task ## ]]\nResolve Los Angeles.\n"
        "[[ ## answer ## ]]\nDone.\n"
        '[[ ## workflow_state ## ]]\n{"region":{"status":"ready"}}\n'
        "[[ ## completed ## ]]"
    )
    chunks = _chunked(full, [2, 5, 1, 8, 3])

    outputs = {}
    for field in (
        "reasoning",
        "next_thought",
        "next_expert",
        "next_task",
        "answer",
        "workflow_state",
    ):
        extractor = AnswerFieldExtractor(field)
        outputs[field] = "".join(extractor.feed(chunk) for chunk in chunks) + extractor.flush()

    assert outputs["reasoning"].strip() == "route to geo"
    assert outputs["next_thought"].strip() == "need coordinates"
    assert outputs["next_expert"].strip() == "geospatial"
    assert outputs["next_task"].strip() == "Resolve Los Angeles."
    assert outputs["answer"].strip() == "Done."
    assert outputs["workflow_state"].strip() == '{"region":{"status":"ready"}}'
    assert all("[[ ##" not in value for value in outputs.values())


def test_field_quoting_its_own_marker_inline_is_not_a_boundary():
    # A next_thought whose prose QUOTES the ChatAdapter markers (the model explaining
    # the output format) must NOT be truncated: only a marker ALONE on its own line is
    # a real field boundary. Before the line-anchored _SECTION regex, this thought was
    # extracted as the garbage between the two quoted markers -> "`, then `".
    full = (
        "[[ ## next_thought ## ]]\n"
        "I must respond in the format strictly, starting with `[[ ## next_thought ## ]]`, "
        "then `[[ ## next_tool_name ## ]]`, then the args. Now I profile "
        "`MTA1.CI.LY_.30.csv`.\n"
        "[[ ## next_tool_name ## ]]\n"
        "pandas_profile_csv\n"
        "[[ ## completed ## ]]"
    )
    for sizes in ([1], [7], [3, 11, 2], [500]):
        extractor = AnswerFieldExtractor("next_thought")
        out = "".join(extractor.feed(c) for c in _chunked(full, sizes)) + extractor.flush()
        assert out.strip().startswith("I must respond in the format strictly")
        assert "`MTA1.CI.LY_.30.csv`" in out
        # the quoted markers survive verbatim INSIDE the thought (not treated as bounds)
        assert "`[[ ## next_thought ## ]]`" in out
        # the real trailing tool-name marker is NOT leaked into the field
        assert "pandas_profile_csv" not in out


def test_escaped_section_boundary_does_not_leak_into_visible_field() -> None:
    """A provider-escaped structural separator ends the visible thought field."""

    full = (
        "[[ ## next_thought ## ]]\nCreate the plot now."
        r"\n[[ ## tool_calls ## ]]\n"
        '{"tool_calls":[{"name":"plot_plot_timeseries","args":{}}]}'
    )
    extractor = AnswerFieldExtractor("next_thought")
    out = "".join(extractor.feed(chunk) for chunk in _chunked(full, [1, 3, 8, 2]))
    out += extractor.flush()

    assert out.strip() == "Create the plot now."
    assert "tool_calls" not in out


def test_only_framed_escaped_section_markers_are_normalized() -> None:
    quoted = r"Explain `[[ ## tool_calls ## ]]` and preserve this\nordinary newline."
    assert normalize_escaped_section_boundaries(quoted) == quoted

    encoded = r"thought\n[[ ## tool_calls ## ]]\n{\"tool_calls\": []}"
    assert normalize_escaped_section_boundaries(encoded) == (
        'thought\n[[ ## tool_calls ## ]]\n{\\"tool_calls\\": []}'
    )


def test_a_value_framing_a_marker_bounds_the_field_in_both_encodings() -> None:
    """The escaped form is byte-identical to the real form once decoded.

    A model that frames a marker with newlines INSIDE its own answer bounds the
    field there -- for ``_SECTION``, for the escaped form, and for DSPy's own
    ChatAdapter parser alike. Nothing local can separate a framed quotation from
    a real separator, so this pins the limitation rather than hiding it: the
    escaped lane must behave exactly like the real-newline lane, never worse.
    """

    body = "The adapter writes{nl}[[ ## answer ## ]]{nl}before each field."

    real = AnswerFieldExtractor("answer")
    real_out = real.feed("[[ ## answer ## ]]\n" + body.format(nl="\n")) + real.flush()

    escaped = AnswerFieldExtractor("answer")
    escaped_out = escaped.feed("[[ ## answer ## ]]\n" + body.format(nl=r"\n")) + escaped.flush()

    assert real_out == escaped_out == "The adapter writes\n"
    # An INLINE quotation (no newline framing) is still prose in both lanes.
    inline = AnswerFieldExtractor("answer")
    inline_out = inline.feed("[[ ## answer ## ]]\nUse `[[ ## answer ## ]]` to open it.") + (
        inline.flush()
    )
    assert inline_out == "Use `[[ ## answer ## ]]` to open it."


def test_extractor_flags_structured_answer_vs_prose():
    prose = AnswerFieldExtractor("answer")
    prose.feed("[[ ## answer ## ]]\n## Region\nReno, Nevada")
    assert prose.is_structured() is False

    js = AnswerFieldExtractor("answer")
    js.feed('[[ ## answer ## ]]\n{\n  "region_name": "Reno"')
    assert js.is_structured() is True


def test_extract_delta_handles_objects_dicts_and_junk():
    class _D:
        def __init__(self, content=None, reasoning_content=None):
            self.content = content
            self.reasoning_content = reasoning_content

    class _C:
        def __init__(self, delta):
            self.delta = delta

    class _Chunk:
        def __init__(self, choices):
            self.choices = choices

    assert extract_delta(_Chunk([_C(_D(content="hi"))])) == ("hi", "")
    assert extract_delta({"choices": [{"delta": {"reasoning_content": "rc"}}]}) == ("", "rc")
    assert extract_delta({"choices": [{"delta": {"reasoning": "vllm"}}]}) == (
        "",
        "vllm",
    )
    assert extract_delta(object()) == ("", "")
    assert extract_delta({"choices": []}) == ("", "")
