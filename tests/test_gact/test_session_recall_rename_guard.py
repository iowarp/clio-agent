"""Transcript recall was renamed ``session_*`` with no aliases (long-term memory plan).

``memory_*`` now means long-term memory, so the old transcript-recall names must not
come back in code, tests, scripts or the site. History (``docs/``) keeps them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REMOVED = (
    "memory_search_sessions",
    "memory_read_session_summary",
    "memory_read_context_frame",
    "/memory/tools/",
    "clio_agent.gact.memory_tools",
    "clio_agent.gact.runtime.memory_search",
)
SUFFIXES = {".py", ".ts", ".tsx", ".md", ".mdx", ".json", ".go", ".mjs"}


def _files() -> list[Path]:
    found: list[Path] = []
    for folder in ("src", "tests", "scripts", "site"):
        for path in (ROOT / folder).rglob("*"):
            if (
                path.suffix in SUFFIXES
                and path.is_file()
                and "node_modules" not in path.parts
                and "__pycache__" not in path.parts
                and path != Path(__file__).resolve()
            ):
                found.append(path)
    return found


@pytest.mark.parametrize("name", REMOVED)
def test_the_old_transcript_recall_name_is_gone(name: str) -> None:
    hits = [str(p.relative_to(ROOT)) for p in _files() if name in p.read_text(errors="ignore")]
    assert hits == [], f"{name} is back in {hits}"
