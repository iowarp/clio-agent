"""Unit tests for the shared ``native_root`` api_base helper (#1413).

Ollama's and LM Studio's handshakes, plus ``lm/factory.py``'s ``ollama_chat``
connection builder, all need the SAME "strip a trailing /v1" surgery. This
used to be two near-identical private staticmethods
(``OllamaHandshake._native_root`` / ``LMStudioHandshake._root``); this module
is the one shared implementation both handshakes and the factory now import.
"""

from __future__ import annotations

from clio_agent.providers.api_base import native_root


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
