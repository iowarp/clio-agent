"""Lazy cl100k_base tiktoken proxy — defers litellm's ~40 MB eager load (#930).

The proxy must (a) defer the real rank-map build until first ``.encode()``,
(b) produce byte-identical token results to the eager encoding, and (c) be
idempotent + safe when tiktoken is patched more than once.
"""

from __future__ import annotations

import importlib

import pytest

tiktoken = pytest.importorskip("tiktoken")

from clio_agent.lm import lazy_tiktoken


@pytest.fixture(autouse=True)
def _restore_tiktoken():
    """Snapshot/restore the tiktoken entry points and the install latch so each
    test starts from the real (un-patched) tiktoken."""
    real_get = tiktoken.get_encoding
    reg = importlib.import_module("tiktoken.registry")
    real_reg_get = reg.get_encoding
    prev_installed = lazy_tiktoken._installed
    lazy_tiktoken._installed = False
    try:
        yield
    finally:
        tiktoken.get_encoding = real_get
        reg.get_encoding = real_reg_get
        lazy_tiktoken._installed = prev_installed


def test_defers_until_first_use():
    assert lazy_tiktoken.install_lazy_cl100k() is True
    enc = tiktoken.get_encoding("cl100k_base")
    # The proxy is in place and has NOT built the real encoding yet.
    assert isinstance(enc, lazy_tiktoken._LazyEncoding)
    assert enc.name == "cl100k_base"
    assert enc._real is None  # cheap name access never materialises
    # First real use materialises and delegates.
    toks = enc.encode("hello world")
    assert enc._real is not None
    assert isinstance(toks, list) and toks


def test_counts_match_eager_encoding():
    real = tiktoken.get_encoding("cl100k_base")  # real, before patching
    expected = real.encode("The quick brown fox jumps over 13 lazy dogs.")
    lazy_tiktoken.install_lazy_cl100k()
    lazy = tiktoken.get_encoding("cl100k_base")
    assert lazy.encode("The quick brown fox jumps over 13 lazy dogs.") == expected
    assert lazy.decode(expected) == real.decode(expected)


def test_other_encodings_pass_through_unproxied():
    lazy_tiktoken.install_lazy_cl100k()
    # A non-cl100k encoding is returned as the real object, not a lazy proxy.
    other = tiktoken.get_encoding("gpt2")
    assert not isinstance(other, lazy_tiktoken._LazyEncoding)


def test_idempotent_install():
    assert lazy_tiktoken.install_lazy_cl100k() is True
    patched = tiktoken.get_encoding
    # Second install must not double-wrap.
    assert lazy_tiktoken.install_lazy_cl100k() is True
    assert tiktoken.get_encoding is patched
    assert getattr(tiktoken.get_encoding, "_clio_lazy", False) is True


def test_cost_recount_disabled():
    """litellm's response-cost recount (the runtime cl100k trigger) is neutralised
    to return None, and clio's own token_counter is left intact."""
    litellm = pytest.importorskip("litellm")
    prev = lazy_tiktoken._cost_recount_disabled
    original = litellm.response_cost_calculator
    lazy_tiktoken._cost_recount_disabled = False
    try:
        assert lazy_tiktoken.disable_litellm_cost_recount() is True
        # Order-independent: an earlier test/boot may already have installed the
        # patch (making `original` the patched closure), so assert the MARKER +
        # behavior, never object identity against a possibly-patched original.
        assert litellm.response_cost_calculator(anything=1) is None
        assert getattr(litellm.response_cost_calculator, "_clio_no_recount", False) is True
        # token_counter itself is NOT patched — OpenAI counting still works.
        assert litellm.token_counter(model="gpt-3.5-turbo", text="hello world") > 0
        # Idempotent.
        assert lazy_tiktoken.disable_litellm_cost_recount() is True
    finally:
        litellm.response_cost_calculator = original
        lazy_tiktoken._cost_recount_disabled = prev
