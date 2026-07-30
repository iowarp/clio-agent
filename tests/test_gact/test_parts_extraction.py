"""Compatibility coverage for the extracted GACT part grammar."""

from importlib import import_module

import pytest


@pytest.mark.parametrize("symbol", ["CapabilityFlags", "Part"])
def test_part_grammar_symbol_identity_across_import_paths(symbol: str) -> None:
    """The owner module and compatibility shim expose identical class objects."""
    compatibility_module = import_module("clio_agent.gact.types")
    owner_module = import_module("clio_agent.gact.parts")

    assert getattr(compatibility_module, symbol) is getattr(owner_module, symbol)
