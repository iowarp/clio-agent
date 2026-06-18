"""Unit tests for the unified LM token highway field extractor (#693)."""

from __future__ import annotations

from clio_agent.runtime.lm_stream import AnswerFieldExtractor, extract_delta


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
        "[[ ## workflow_state ## ]]\n{\"a\": 1}\n[[ ## completed ## ]]"
    )
    ex = AnswerFieldExtractor("answer")
    out = "".join(ex.feed(c) for c in _chunked(full, [1, 4, 9, 2, 7, 3])) + ex.flush()
    assert out.strip() == "Hello world, the result."
    assert "[[" not in out and "##" not in out  # no markers leaked
    assert "thinking" not in out  # reasoning not leaked


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
    assert extract_delta(object()) == ("", "")
    assert extract_delta({"choices": []}) == ("", "")
