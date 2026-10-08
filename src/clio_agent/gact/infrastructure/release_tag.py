"""Translate installed package versions to the repository's release tags."""

from __future__ import annotations

import re


def release_tag(version: str) -> str:
    """Return the GitHub tag for a stable or beta CLIO package release.

    PyPI normalizes ``0.9.5-beta.1`` to ``0.9.5b1``. Keep the package
    version untouched for registry operations and translate only GitHub refs.
    """

    match = re.fullmatch(
        r"(\d+\.\d+\.\d+(?:\.\d+)?)(?:b(\d+)(?:\.post(\d+))?|-beta\.(\d+)(?:\.(\d+))?)?",
        version,
    )
    if match is None:
        raise ValueError(f"CLIO {version!r} has no release tag; deploy from a released CLIO.")
    base, pep_beta, pep_hotfix, tag_beta, tag_hotfix = match.groups()
    beta = pep_beta or tag_beta
    hotfix = pep_hotfix or tag_hotfix
    suffix = f".{hotfix}" if hotfix is not None else ""
    return f"v{base}-beta.{beta}{suffix}" if beta else f"v{base}"
