"""``@file.h5::/entry/data``: a workspace-file reference to one object inside an HDF5 file.

The locator rides in the reference id, after ``::`` (the silx/NeXus file-plus-object
URL convention; a single ``:`` would clash with Windows drive letters). A name that
itself contains ``::`` still resolves as a plain file: the split applies only when
the whole id is not an existing file and the part after ``::`` is an absolute HDF5
path.

Admission resolves the object through :mod:`clio_agent.gact.hdf5_view`, so the same
containment applies as for the viewer (links and virtual datasets never leave the
workspace), and the agent gets a short summary of what was referenced. h5py loads on
the first such reference, not with the app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

LOCATOR_SEPARATOR = "::"


class LocatorError(ValueError):
    """The object a locator names cannot be referenced; ``code`` is the hdf5 refusal."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def split_locator(root: Path, ref_id: str) -> tuple[str, str]:
    """Split ``data/run.h5::/entry/data`` into the file id and the object path."""

    file_id, separator, locator = ref_id.partition(LOCATOR_SEPARATOR)
    if not separator or not locator.startswith("/") or (root / ref_id).is_file():
        return ref_id, ""
    return file_id, locator


def locator_summary(root: Path, path: Path, locator: str) -> dict[str, Any]:
    """The summary of ``locator`` in ``path``; raises :class:`LocatorError` when unusable."""

    from clio_agent.gact import hdf5_view  # noqa: PLC0415 - h5py loads per reference

    try:
        return hdf5_view.object_summary(path, hdf5_view.Roots((root, path.parent)), locator)
    except hdf5_view.Hdf5ViewError as exc:
        raise LocatorError(exc.status, exc.code, exc.message) from exc
