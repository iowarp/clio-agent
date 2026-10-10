"""Tests never resolve an exported live CLIO home (F015)."""

from __future__ import annotations

import os
from pathlib import Path

from clio_agent import paths


def test_an_exported_clio_agent_home_is_not_used_by_tests(tmp_path: Path) -> None:
    assert "CLIO_AGENT_HOME" not in os.environ
    assert not [k for k in os.environ if k.startswith("CLIO_AGENT_") and k.endswith("_DIR")]
    assert paths.user_config_dir().is_relative_to(tmp_path.parent)
