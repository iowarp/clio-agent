"""Single source of truth for the scientific file-suffix vocabulary.

Three hand-synced copies of this vocabulary used to drift independently
(issue #765): a suffix set in ``agent.py``, a regex alternation in
``harness.py``, and inline patterns in ``gact/delegation.py``. Both the
set-based checks and the regex alternations now derive from
:data:`SCIENTIFIC_SUFFIXES` below.

This vocabulary is structural grounding only (does the text mention a
file at all), NOT keyword->format inference — nothing branches on which
suffix matched. Keep it a minimal, case-agnostic list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Canonical vocabulary, without leading dots. Order here is irrelevant:
# regex alternations are built longest-first so variants that share a
# prefix/suffix ("hdf5" vs "h5", "fasta" vs "fa", "tgz" vs "gz") always
# match the longer form.
SCIENTIFIC_SUFFIXES: tuple[str, ...] = (
    "h5",
    "hdf5",
    "parquet",
    "csv",
    "bp",
    "bp4",
    "bp5",
    "sac",
    "tar",
    "tgz",
    "gz",
    "fa",
    "fasta",
    "fna",
    "vcf",
    "cif",
    "geojson",
    "png",
    "mzml",
)

# Dotted, lowercase form for ``Path(...).suffix.lower() in ...`` checks.
SCIENTIFIC_FILE_SUFFIXES: frozenset[str] = frozenset(f".{name}" for name in SCIENTIFIC_SUFFIXES)


def scientific_suffix_alternation(extra: Iterable[str] = ()) -> str:
    """Non-capturing regex alternation over the vocabulary, longest-first.

    Args:
        extra: Additional suffixes (no leading dot) a call site needs on
            top of the shared vocabulary. Kept explicit so local
            extensions stay visible at the call site.

    Returns:
        A ``(?:name|name|...)`` alternation, safe to embed after ``\\.``
        in a larger pattern (compile with ``re.IGNORECASE``).
    """
    names = sorted({*SCIENTIFIC_SUFFIXES, *extra}, key=lambda name: (-len(name), name))
    return "(?:" + "|".join(re.escape(name) for name in names) + ")"
