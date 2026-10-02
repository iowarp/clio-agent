"""Tests for the #893 memory profiler's fail-loud backend assertion.

The owner completion requirement on iowarp/clio-agent#893 is that a clio-core
measurement must never run on another store. The profiler enforces this with
:func:`assert_backend`, which reads the concrete store class the server actually built
(``ARCMemory._store``) and refuses to serve on a mismatch. These tests pin that
assertion with a stand-in ``ARCMemory`` holding a store of a given class name.
"""

from __future__ import annotations

import types

import pytest

from scripts.profile_session_memory import _EXPECTED_STORE_CLASS, assert_backend


def _fake_arc(store_class_name: str) -> types.SimpleNamespace:
    """A stand-in ARCMemory exposing ``_store`` of a class named ``store_class_name``."""
    store_cls = type(store_class_name, (), {})
    return types.SimpleNamespace(_store=store_cls())


def test_clio_core_backend_matches_clio_core_store() -> None:
    arc = _fake_arc("ClioCoreStore")
    assert assert_backend(arc, "cte") == "ClioCoreStore"


def test_clio_core_requested_but_another_store_built_raises() -> None:
    arc = _fake_arc("SomeOtherStore")
    with pytest.raises(RuntimeError, match="backend mismatch"):
        assert_backend(arc, "cte")


def test_local_is_no_longer_a_backend() -> None:
    arc = _fake_arc("ClioCoreStore")
    with pytest.raises(RuntimeError, match="unknown"):
        assert_backend(arc, "local")


def test_missing_store_raises() -> None:
    arc = types.SimpleNamespace()  # no _store attribute at all
    with pytest.raises(RuntimeError, match="backend mismatch"):
        assert_backend(arc, "cte")


def test_expected_store_class_map_is_the_contract() -> None:
    """The map the assertion reads names exactly the one supported store."""
    assert _EXPECTED_STORE_CLASS == {"cte": "ClioCoreStore"}
