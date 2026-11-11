"""Whether live LaTeX builds can run here, for the tests that build a real paper.

A live build needs the pinned Tectonic: ``CLIO_DOCUMENT_TECTONIC`` or a managed install
(``clio_agent.runtime.tectonic``). Without one the live tests skip, except where
``CLIO_REQUIRE_TECTONIC=1`` says the machine was provisioned for them (the document-runtime
workflow on Linux, macOS and Windows): there a missing Tectonic fails the build instead of
skipping it, so a platform row cannot pass without building a paper.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def tectonic_available() -> bool:
    """True when a pinned Tectonic is installed, or the run requires one."""
    from clio_agent.runtime.tectonic import installed_tectonic  # noqa: PLC0415

    if os.environ.get("CLIO_REQUIRE_TECTONIC", "").strip() == "1":
        return True
    override = os.environ.get("CLIO_DOCUMENT_TECTONIC", "").strip()
    return bool(override and Path(override).is_file()) or installed_tectonic() is not None


#: Marks a test that builds with the real Tectonic.
live_tectonic = pytest.mark.skipif(
    not tectonic_available(),
    reason="no pinned Tectonic is installed (CLIO_DOCUMENT_TECTONIC)",
)
