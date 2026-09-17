"""The 43 official A2UI 0.9.1 Basic-catalog examples validate end to end.

Vendored from clio-schemas' own corpus (``tests/a2ui_corpus/v0_9_1/examples/``,
upstream ``specification/v0_9_1/catalogs/basic/examples/``) into
``tests/fixtures/a2ui_corpus/v0_9_1/``, with a ``SOURCE.json`` subset (the
same commit + sha256 entries clio-schemas records) so a copy-paste drift is
caught by a hash check rather than silently diverging. clio-schemas already
proves these 43 files validate against its OWN Draft202012Validator
machinery (S1); this suite proves the SAME 126 messages fold cleanly through
THIS server's production path (``apply_batch`` -> ``validate_server_message``
-> the catalog-aware safety walk), one createSurface-rooted batch per file,
exactly as a real producer would send them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.a2ui import apply_batch
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_corpus" / "v0_9_1"
EXAMPLES_ROOT = CORPUS_ROOT / "examples"
SOURCE = json.loads((CORPUS_ROOT / "SOURCE.json").read_text(encoding="utf-8"))
EXAMPLE_FILES = sorted(EXAMPLES_ROOT.glob("*.json"))


def test_source_manifest_lists_exactly_43_examples() -> None:
    assert len(SOURCE["files"]) == 43
    assert len(EXAMPLE_FILES) == 43


@pytest.mark.parametrize("relpath", sorted(SOURCE["files"]), ids=lambda p: p.rsplit("/", 1)[-1])
def test_vendored_example_hash_matches_source_manifest(relpath: str) -> None:
    """A copy-paste drift from the upstream corpus is a hash mismatch, not a silent skip."""

    expected = SOURCE["files"][relpath]["sha256"]
    actual = hashlib.sha256((CORPUS_ROOT / relpath).read_bytes()).hexdigest()
    assert actual == expected


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("example_path", EXAMPLE_FILES, ids=lambda p: p.name)
def test_official_example_batch_folds_through_apply_batch(example_path: Path) -> None:
    """Every message in one official example applies as one ordered batch."""

    payload = _load(example_path)
    registry = CatalogRegistry()
    apply_batch({}, "corpus", payload["messages"], catalogs=registry)


def test_all_126_official_messages_validate() -> None:
    """The literal count the campaign issue asks for: 126 messages, 0 rejected."""

    registry = CatalogRegistry()
    total = 0
    for example_path in EXAMPLE_FILES:
        payload = _load(example_path)
        total += len(payload["messages"])
        apply_batch({}, f"corpus-{example_path.stem}", payload["messages"], catalogs=registry)
    assert total == 126
