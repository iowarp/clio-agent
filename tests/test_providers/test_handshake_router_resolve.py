"""Capability (a.2): single-endpoint router model-availability resolution
(epic #667, #669).

``HandshakeReport.resolve_model`` is the fail-fast check for a router that serves
several models behind one ``api_base`` (llama.cpp native router, LM Studio JIT):
the declared model resolves to a served profile (exact or vendor-prefix-agnostic
basename), or ``None`` means the endpoint does not serve it. Offline + hermetic.
"""

from __future__ import annotations

from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)


def _router_report(*model_ids: str) -> HandshakeReport:
    return HandshakeReport(
        provider_id="lm_studio",
        provider_kind="lm_studio",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models=tuple(ModelProfile(id=m) for m in model_ids),
    )


def test_resolve_exact_match():
    rep = _router_report("gpt-oss-120b", "qwen2.5-coder-32b")
    prof = rep.resolve_model("gpt-oss-120b")
    assert prof is not None and prof.id == "gpt-oss-120b"


def test_resolve_basename_across_vendor_prefix():
    # declared "openai/gpt-oss-120b" must match a served bare "gpt-oss-120b"
    rep = _router_report("gpt-oss-120b")
    prof = rep.resolve_model("openai/gpt-oss-120b")
    assert prof is not None and prof.id == "gpt-oss-120b"


def test_resolve_basename_other_direction():
    # declared bare name matches a served vendor-prefixed id
    rep = _router_report("lmstudio-community/gpt-oss-120b")
    prof = rep.resolve_model("gpt-oss-120b")
    assert prof is not None


def test_unavailable_model_returns_none():
    rep = _router_report("gpt-oss-120b", "mistral-7b")
    assert rep.resolve_model("llama-3.1-405b") is None


def test_available_model_ids_for_failfast_message():
    rep = _router_report("gpt-oss-120b", "mistral-7b")
    assert rep.available_model_ids() == ("gpt-oss-120b", "mistral-7b")
    # a typo'd declaration surfaces what IS served, for a clear error
    assert rep.resolve_model("gpt-oss-12b") is None
    assert "gpt-oss-120b" in rep.available_model_ids()


def test_resolve_is_case_insensitive_on_basename():
    rep = _router_report("Gpt-OSS-120B")
    assert rep.resolve_model("gpt-oss-120b") is not None


def test_resolve_tolerates_whitespace():
    # a stray leading/trailing space (copy-paste / API quirk) must still match
    rep = _router_report("gpt-oss-120b")
    assert rep.resolve_model("  gpt-oss-120b  ") is not None
    assert rep.resolve_model("openai/gpt-oss-120b\n") is not None


def test_resolve_tolerates_trailing_slash():
    rep = _router_report("gpt-oss-120b")
    assert rep.resolve_model("gpt-oss-120b/") is not None


def test_ambiguous_basename_refuses_to_guess():
    # two served models share a basename -> a bare name is ambiguous; resolve returns
    # None rather than silently picking the first (the wrong-model bug)
    rep = _router_report("openai/gpt-oss-120b", "meta/gpt-oss-120b")
    assert rep.resolve_model("gpt-oss-120b") is None
    # but an exact id is still unambiguous
    assert rep.resolve_model("openai/gpt-oss-120b").id == "openai/gpt-oss-120b"
