"""Every attention test: capture on, Flowcept configured, files from the fixture dir."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from clio_agent.gact.attention import files
from tests.test_gact.test_attention._support import FIXTURES


@pytest.fixture(autouse=True)
def _fixture_files_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_FILES_DIR", str(FIXTURES))
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "1")
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl,flowcept")
    files.clear_cache()
    yield
    files.clear_cache()
