"""Translate installed package versions to the repository's release tags."""

from __future__ import annotations

import re


def release_tag(version: str) -> str:
    """Return the GitHub tag for a stable or beta CLIO package release.

    PyPI normalizes ``0.9.5-beta.1`` to ``0.9.5b1``. Keep the package
    version untouched for registry operations and translate only GitHub refs.
    """

    match = re.fullmatch(r"(\d+\.\d+\.\d+(?:\.\d+)?)(?:b(\d+)|-beta\.(\d+))?", version)
    if match is None:
        raise ValueError(f"CLIO {version!r} has no release tag; deploy from a released CLIO.")
    base, pep_beta, tag_beta = match.groups()
    beta = pep_beta or tag_beta
    return f"v{base}-beta.{beta}" if beta else f"v{base}"
