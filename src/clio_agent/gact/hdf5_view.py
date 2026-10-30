"""HDF5 content for the workspace file viewer, resolved inside the workspace.

The gact-tui viewer (h5web) reads a file through h5grove's HTTP API, one request
per group listing, attribute set or array slice. h5grove's content classes do the
encoding; this module owns how a path inside a file is resolved, because HDF5
would otherwise follow links on its own:

* an external link names another file, and HDF5 searches several places for it
  (the parent file's folder, the current directory, ``HDF5_EXT_PREFIX``);
* a soft link can point back through an external link;
* a virtual dataset reads its values from source files named in the dataset.

Every hop is therefore walked here, one link at a time. An external link is
followed only when its target, taken relative to the file that holds the link,
is an existing file inside the allowed roots, and this module opens that file
itself. A request that would leave the roots is refused with a typed error; a
group listing reports such a child as an unresolved link instead of failing.

A response is bounded by size, not the file: a slice larger than ``max_bytes`` is
refused before it is read, so a 100 GB file opens as easily as a small one.
"""

from __future__ import annotations

import contextlib
import posixpath
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np
from h5grove.content import (
    DatasetContent,
    DatatypeContent,
    ExternalLinkContent,
    GroupContent,
    ResolvedEntityContent,
    SoftLinkContent,
)
from h5grove.encoders import encode
from h5grove.utils import NotFoundError, QueryArgumentError, hdf_path_join, parse_slice

MAX_LINK_HOPS = 32


class Hdf5ViewError(Exception):
    """A typed refusal: ``status`` and ``code`` become the HTTP error envelope."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Roots:
    """Folders a link may lead into: the workspace and the opened file's own folder."""

    folders: tuple[Path, ...]

    def contains(self, target: Path) -> bool:
        return any(target == root or target.is_relative_to(root) for root in self.folders)


def _parts(path: str) -> list[str]:
    return [part for part in posixpath.normpath(path).split("/") if part not in ("", ".")]


class _Walk:
    """Resolve display paths to h5py objects, opening linked files on an exit stack."""

    def __init__(self, stack: contextlib.ExitStack, top: h5py.File, roots: Roots) -> None:
        self._stack = stack
        self._top = top
        self._roots = roots

    def entity(self, path: str) -> Any:
        """The object at ``path``.

        A link at the end of ``path`` that cannot be followed (dangling, or into a file
        outside the roots) is returned unresolved, as h5grove reports it. The same link
        part-way along ``path`` is a typed refusal.
        """

        return self._resolve(self._top, _parts(path), path, 0)

    def _resolve(self, file: h5py.File, parts: list[str], path: str, hops: int) -> Any:
        # ``inner`` is always reached by hard links in ``file``, so asking for the next
        # link with ``getlink=True`` never makes HDF5 follow anything by itself.
        inner = "/"
        while parts:
            name = parts.pop(0)
            candidate = posixpath.join(inner, name)
            link = file.get(candidate, getlink=True)
            if link is None:
                raise Hdf5ViewError(404, "hdf5_path_not_found", f"{path} is not in the file")
            if isinstance(link, h5py.HardLink):
                inner = candidate
                continue
            if hops >= MAX_LINK_HOPS:
                raise Hdf5ViewError(422, "hdf5_link_loop", f"too many links resolving {path}")
            if isinstance(link, h5py.SoftLink):
                target = _parts(posixpath.join(inner, link.path))
                next_file: h5py.File | None = file
            else:
                next_file = self._linked_file(file, link.filename, candidate, final=not parts)
                target = _parts(link.path)
            if next_file is None:
                return link
            try:
                return self._resolve(next_file, target + parts, path, hops + 1)
            except Hdf5ViewError as exc:
                if parts or exc.code != "hdf5_path_not_found":
                    raise
                return link
        return file[inner]

    def _linked_file(
        self, holder: h5py.File, filename: str, where: str, *, final: bool
    ) -> h5py.File | None:
        """Open an external link's target when it is an existing file inside the roots."""

        target = (Path(holder.filename).parent / filename).resolve()
        if self._roots.contains(target) and target.is_file():
            return self._stack.enter_context(h5py.File(target, "r"))
        if final:
            return None
        if not self._roots.contains(target):
            raise Hdf5ViewError(
                403, "hdf5_link_outside_workspace", f"{where} links to a file outside the workspace"
            )
        raise Hdf5ViewError(404, "hdf5_path_not_found", f"{where} links to a missing file")

    def content(self, path: str) -> Any:
        """h5grove content for ``path``; groups list their children through this walk."""

        entity = self.entity(path)
        if isinstance(entity, h5py.ExternalLink):
            return ExternalLinkContent(path, entity)
        if isinstance(entity, h5py.SoftLink):
            return SoftLinkContent(path, entity)
        if isinstance(entity, h5py.Dataset):
            return DatasetContent(path, entity)
        if isinstance(entity, h5py.Group):
            return _Group(path, entity, self)
        if isinstance(entity, h5py.Datatype):
            return DatatypeContent(path, entity)
        raise Hdf5ViewError(422, "hdf5_unsupported_object", f"{path} is not a supported object")


class _Group(GroupContent):
    """A group whose children resolve through the same contained walk."""

    def __init__(self, path: str, group: h5py.Group, walk: _Walk) -> None:
        super().__init__(path, group, group.file)
        self._walk = walk

    def _get_child_metadata_content(self, depth: int = 0) -> list[Any]:
        return [
            self._walk.content(posixpath.join(self._path, child)).metadata(depth)
            for child in self._h5py_entity.keys()
        ]


@contextlib.contextmanager
def open_view(path: Path, roots: Roots) -> Iterator[_Walk]:
    """Open ``path`` read-only; linked files close with it."""

    with contextlib.ExitStack() as stack:
        try:
            top = stack.enter_context(h5py.File(path, "r"))
        except OSError as exc:
            raise Hdf5ViewError(
                422, "hdf5_unreadable", f"{path.name} is not a readable HDF5 file"
            ) from exc
        yield _Walk(stack, top, roots)


def selection_bytes(dataset: h5py.Dataset, selection: str | None) -> int:
    """Bytes a ``selection`` of ``dataset`` would read, without reading it."""

    shape: Sequence[int] = dataset.shape or ()
    if selection:
        try:
            members = parse_slice(selection)
        except ValueError as exc:
            raise Hdf5ViewError(422, "hdf5_invalid_selection", str(exc)) from exc
        if len(members) > len(shape):
            raise Hdf5ViewError(
                422, "hdf5_invalid_selection", f"{selection} has more members than dimensions"
            )
        counts = [
            len(range(*member.indices(size))) if isinstance(member, slice) else 1
            for member, size in zip(members, shape, strict=False)
        ]
        shape = [*counts, *shape[len(members) :]]
    return int(np.prod(shape, dtype=np.int64)) * dataset.dtype.itemsize


def check_readable(dataset: h5py.Dataset, selection: str | None, roots: Roots, max_bytes: int):
    """Refuse a slice over ``max_bytes`` or a virtual dataset reading outside the roots."""

    size = selection_bytes(dataset, selection)
    if size > max_bytes:
        raise Hdf5ViewError(
            413,
            "hdf5_selection_too_large",
            f"the selection is {size} bytes; the limit is {max_bytes}. Select a smaller slice.",
        )
    if not dataset.is_virtual:
        return
    base = Path(dataset.file.filename).parent
    for source in dataset.virtual_sources():
        if source.file_name == ".":
            continue
        target = (base / source.file_name).resolve()
        if not roots.contains(target) or not target.is_file():
            raise Hdf5ViewError(
                403,
                "hdf5_virtual_source_outside_workspace",
                f"{dataset.name} reads from a file outside the workspace",
            )


def respond(
    target: Path, roots: Roots, endpoint: str, params: dict[str, Any], max_bytes: int
) -> tuple[bytes, dict[str, str]]:
    """Answer one h5grove ``endpoint`` for ``target``: the encoded body and its headers."""

    path = params.get("path") or "/"
    try:
        with open_view(target, roots) as walk:
            if endpoint == "meta":
                return _encoded(walk.content(path).metadata(), "json")
            if endpoint == "attr":
                return _encoded(_resolved(walk, path).attributes(params.get("attr_keys")), "json")
            if endpoint == "paths":
                return _encoded(_paths(walk, path), "json")
            selection = params.get("selection")
            content = _resolved(walk, path)
            if not isinstance(content, DatasetContent):
                raise Hdf5ViewError(422, "hdf5_not_a_dataset", f"{path} is not a dataset")
            check_readable(content._h5py_entity, selection, roots, max_bytes)
            if endpoint == "stats":
                return _encoded(content.data_stats(selection), "json")
            data = content.data(selection, params.get("flatten", False), params.get("dtype"))
            return _encoded(data, params.get("format", "json"))
    except NotFoundError as exc:
        raise Hdf5ViewError(404, "hdf5_path_not_found", str(exc)) from exc
    except (QueryArgumentError, ValueError, TypeError) as exc:
        raise Hdf5ViewError(422, "hdf5_invalid_query", str(exc)) from exc


def _encoded(content: Any, encoding: str | None) -> tuple[bytes, dict[str, str]]:
    encoded = encode(content, encoding)
    return encoded.content, dict(encoded.headers)


def _resolved(walk: _Walk, path: str) -> ResolvedEntityContent:
    content = walk.content(path)
    if not isinstance(content, ResolvedEntityContent):
        raise Hdf5ViewError(
            403, "hdf5_link_outside_workspace", f"{path} is a link that cannot be followed"
        )
    return content


def _paths(walk: _Walk, path: str) -> list[str]:
    group = _resolved(walk, path)._h5py_entity
    if not isinstance(group, h5py.Group):
        raise Hdf5ViewError(422, "hdf5_not_a_group", f"{path} is not a group")
    names = [path]
    # Link names only: listing never follows a link, so nothing leaves the file.
    group.id.links.visit(lambda name: names.append(hdf_path_join(path, name.decode())))
    return names
