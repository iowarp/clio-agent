"""Corpus governance acceptance (design §4.1.C).

Proves the redaction pass is (a) SHAPE-preserving under every surface normalizer —
so the committed redacted corpus is a faithful stand-in for the gate — and (b)
leak-free: no non-structural content survives, and the redacted files DO differ from
the originals in content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.equivalence import corpus as C
from tests.equivalence import normalizers as N

_REAL_STORE = Path("D:/Libraries/Documents/projects/clio-agent/.clio/agent/messages")


def test_committed_corpus_is_present_and_parses() -> None:
    ledgers = C.committed_corpus()
    assert len(ledgers) >= 3, "expected 3-5 committed redacted ledgers"
    for _sid, rows in ledgers:
        # every committed row must re-parse to the served projection cleanly
        N.normalize_persistence(rows)


def test_committed_corpus_has_no_content_leaks() -> None:
    """Every non-structural string leaf in the committed corpus is redaction bytes."""

    allowed = set("x \n\r\t")
    leaks: list[str] = []

    def scan(obj: object, key: str | None = None) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                scan(v, k)
        elif isinstance(obj, list):
            for v in obj:
                scan(v, key)
        elif isinstance(obj, str) and obj and key not in C._STRUCTURAL_KEYS:
            if set(obj) - allowed:
                leaks.append(f"{key}={obj[:40]!r}")

    for _sid, rows in C.committed_corpus():
        scan(rows)
    assert not leaks, f"committed corpus leaked non-structural content: {leaks[:5]}"


def test_redaction_is_shape_preserving_under_persistence_normalizer() -> None:
    """``normalized_shape(normalize(original)) == normalized_shape(normalize(redacted))``.

    Runs against the REAL local ledgers when present (the strongest check), else
    against the committed redacted corpus re-redacted (idempotence: redacting an
    already-redacted ledger keeps the same shape)."""

    if _REAL_STORE.is_dir():
        sources = [
            (fp.stem, [r for r in json.loads(fp.read_text("utf-8")) if isinstance(r, dict)])
            for fp in sorted(_REAL_STORE.glob("sess_*.json"))[:8]
        ]
    else:
        sources = C.committed_corpus()

    assert sources, "no source ledgers available"
    for sid, rows in sources:
        if not rows:
            continue
        orig_shape = C.normalized_shape(N.normalize_persistence(rows))
        red_shape = C.normalized_shape(N.normalize_persistence(C.redact_ledger(rows)))
        assert orig_shape == red_shape, f"redaction changed SHAPE for {sid}"


def test_redaction_actually_changes_content() -> None:
    """Redaction is not a no-op: at least one text-bearing ledger differs post-redaction."""

    if not _REAL_STORE.is_dir():
        pytest.skip("real local store not available")
    changed_any = False
    for fp in sorted(_REAL_STORE.glob("sess_*.json"))[:8]:
        rows = [r for r in json.loads(fp.read_text("utf-8")) if isinstance(r, dict)]
        if not rows:
            continue
        before = json.dumps(N.normalize_persistence(rows))
        after = json.dumps(N.normalize_persistence(C.redact_ledger(rows)))
        if before != after:
            changed_any = True
            break
    assert changed_any, "redaction changed nothing on any real ledger"


def test_local_corpus_loader_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(C.LOCAL_CORPUS_ENV, raising=False)
    assert C.local_corpus() is None
    # sweep falls back to the committed corpus when the env var is unset
    assert C.sweep_corpus() == C.committed_corpus()
