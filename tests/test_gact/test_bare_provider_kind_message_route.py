"""#1418 cause B: a message route naming a bare provider KIND gets a typed 400.

``_bare_provider_kind_error`` is the guard ``message_submission.py`` runs
before the (pre-existing) active-LM-mismatch 501 check. With the frontend
identity fix (#1418 cause A) landed, a real client never sends a bare kind as
``provider_id`` again -- these tests drive the guard directly, the same way
this suite already tests other typed routing gates
(``test_vision_capability_gate.py``).
"""

from __future__ import annotations

from clio_agent.gact.providers.config import _bare_provider_kind_error
from clio_agent.gact.types import ModelRef


def test_empty_provider_id_is_not_flagged() -> None:
    assert _bare_provider_kind_error(ModelRef(), session_id="s1", source="session") is None


def test_a_real_specific_provider_id_is_not_flagged() -> None:
    """``llama_cpp`` is a real preset id -- never a bare kind."""

    ref = ModelRef(provider_id="llama_cpp", model_id="qwen3-4b-instruct-gguf")
    assert _bare_provider_kind_error(ref, session_id="s1", source="session") is None


def test_a_kind_that_is_also_a_real_provider_id_is_not_flagged() -> None:
    """``ollama``/``anthropic``/the direct ``openai`` preset: kind == id, ambiguous.

    These must NOT be rejected -- they are indistinguishable from a genuine
    selection of that exact preset.
    """

    for provider_id in ("ollama", "anthropic", "openai", "lm_studio", "codex", "claude_code"):
        ref = ModelRef(provider_id=provider_id, model_id="x")
        assert _bare_provider_kind_error(ref, session_id="s1", source="session") is None


def test_a_bare_kind_with_no_matching_provider_id_is_rejected() -> None:
    """``argonne`` is a kind with NO preset sharing its id (only argonne_sophia /
    argonne_metis exist) -- the one unambiguous case."""

    ref = ModelRef(provider_id="argonne", model_id="openai/gpt-oss-120b")
    error = _bare_provider_kind_error(ref, session_id="s1", source="per_message")
    assert error is not None
    payload = error.model_dump(exclude_none=True)["error"]
    assert payload["error"] == "provider_kind_is_not_a_provider_id"
    assert "argonne" in payload["message"]
    assert payload["details"]["session_id"] == "s1"
    assert payload["details"]["source"] == "per_message"


def test_an_unknown_provider_id_that_names_no_kind_either_is_not_flagged() -> None:
    """A typo/unknown id is a different failure mode (handled elsewhere), not this one."""

    ref = ModelRef(provider_id="not-a-real-provider", model_id="x")
    assert _bare_provider_kind_error(ref, session_id="s1", source="session") is None
