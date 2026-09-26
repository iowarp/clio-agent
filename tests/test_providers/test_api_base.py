"""Unit tests for the shared ``api_base`` helpers: ``native_root`` (#1413) and
``normalize`` (model-capabilities brief Part 3).

Ollama's and LM Studio's handshakes, plus ``lm/factory.py``'s ``ollama_chat``
connection builder, all need the SAME "strip a trailing /v1" surgery. This
used to be two near-identical private staticmethods
(``OllamaHandshake._native_root`` / ``LMStudioHandshake._root``); this module
is the one shared implementation both handshakes and the factory now import.

``normalize`` is the second piece: canonicalizing an ``api_base`` before it
enters an identity key (:mod:`clio_agent.providers.identity`) so cosmetically
different spellings of the same endpoint collapse to one key.
"""

from __future__ import annotations

from clio_agent.providers.api_base import native_root, normalize


def test_native_root_strips_trailing_v1() -> None:
    assert native_root("http://h:1234/v1") == "http://h:1234"


def test_native_root_strips_trailing_v1_and_slash() -> None:
    assert native_root("http://h:1234/v1/") == "http://h:1234"


def test_native_root_is_identity_with_no_v1_suffix() -> None:
    assert native_root("http://h:1234") == "http://h:1234"


def test_native_root_strips_trailing_slash_with_no_v1() -> None:
    assert native_root("http://h:1234/") == "http://h:1234"


def test_native_root_of_empty_string_is_empty() -> None:
    assert native_root("") == ""


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #


def test_normalize_strips_a_trailing_slash() -> None:
    assert normalize("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434/v1"


def test_normalize_preserves_v1_as_a_real_path_segment() -> None:
    # A bare /v1 with no trailing slash must be left exactly as given.
    assert normalize("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"


def test_normalize_drops_the_default_http_port() -> None:
    assert normalize("http://example.com:80/v1") == "http://example.com/v1"


def test_normalize_drops_the_default_https_port() -> None:
    assert normalize("https://api.openai.com:443/v1") == "https://api.openai.com/v1"


def test_normalize_keeps_a_non_default_port() -> None:
    assert normalize("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"


def test_normalize_lowercases_scheme_and_host_but_not_path() -> None:
    assert normalize("HTTP://LocalHost:11434/V1") == "http://localhost:11434/V1"


def test_normalize_of_empty_string_is_empty() -> None:
    assert normalize("") == ""


def test_normalize_is_identity_for_an_sdk_marker() -> None:
    # codex://sdk / claude-code://sdk carry no host/port to canonicalize.
    assert normalize("codex://sdk") == "codex://sdk"
    assert normalize("claude-code://sdk") == "claude-code://sdk"


def test_normalize_equivalent_spellings_collapse_to_one_key() -> None:
    assert normalize("http://127.0.0.1:11434/v1/") == normalize("http://127.0.0.1:11434/v1")
    assert normalize("HTTP://127.0.0.1:11434/v1") == normalize("http://127.0.0.1:11434/v1")
    assert normalize("http://127.0.0.1:80/v1") == normalize("http://127.0.0.1/v1")
