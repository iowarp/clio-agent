"""Regression tests for :func:`tests.env_isolation.isolated_environ` (#902).

These pin the invariant that makes the provider-config env isolation safe: the
process-global ``PATH`` (and other OS-essential vars) is never removed, so a
concurrent ``shutil.which()`` cannot spuriously fail with 'not found on PATH'
while a test is inside an isolated-environment window.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests.env_isolation import isolated_environ


def test_isolated_environ_preserves_path_and_clears_app_vars() -> None:
    """Clears application vars but keeps PATH intact and restores everything on exit.

    Regression for #902: a bare ``patch.dict("os.environ", {}, clear=True)``
    removed PATH process-wide, so a concurrent ``shutil.which("sh")`` returned
    ``None``. Preserving PATH closes that window while still isolating the
    ``CLIO_*`` variables the provider-config tests care about.
    """
    os.environ["CLIO_SENTINEL_902"] = "ambient"
    original_path = os.environ.get("PATH")
    assert original_path, "test precondition: PATH must be set in the environment"

    try:
        with isolated_environ({"CLIO_LM_PROVIDER": "ollama"}):
            # The override is applied and ambient application vars are cleared...
            assert os.environ["CLIO_LM_PROVIDER"] == "ollama"
            assert "CLIO_SENTINEL_902" not in os.environ
            # ...but PATH is preserved verbatim, so launcher lookup still works.
            assert os.environ.get("PATH") == original_path

        # Fully restored after the window.
        assert os.environ.get("CLIO_SENTINEL_902") == "ambient"
        assert os.environ.get("PATH") == original_path
    finally:
        os.environ.pop("CLIO_SENTINEL_902", None)


def test_isolated_environ_keeps_launcher_resolvable() -> None:
    """``shutil.which`` of a real launcher still resolves inside the window.

    This is the exact call ``clio_agent.tools.mcp_config.transport_for`` makes;
    under the old ``clear=True`` it returned ``None`` because PATH was gone.
    """
    launcher = next(
        (name for name in ("sh", "cmd", "python") if shutil.which(name)), None
    )
    if launcher is None:
        pytest.skip("no probe launcher resolvable on PATH")

    with isolated_environ({}):
        assert shutil.which(launcher) is not None
