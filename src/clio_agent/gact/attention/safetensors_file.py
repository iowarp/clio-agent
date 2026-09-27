"""Read the connector's SafeTensors attention file, a tensor or a row range at a time.

The format is fixed and tiny: an 8-byte little-endian header length, a JSON
header ``{name: {dtype, shape, data_offsets: [begin, end]}, "__metadata__":
{str: str}}``, then the raw little-endian tensor bytes. A response's step
tensors are ``[G, k]`` (up to ~200 MB per file), and a selection needs only a
handful of rows, so rows are read by byte range instead of loading the file.

Every structural problem is ``attention_record_malformed``; an unreadable file
is ``attention_file_unavailable``. Nothing is guessed.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from clio_agent.gact.attention.reasons import AttentionUnavailable

_DTYPES = {
    "F64": "<f8",
    "F32": "<f4",
    "F16": "<f2",
    "I64": "<i8",
    "I32": "<i4",
    "I16": "<i2",
    "I8": "i1",
    "U8": "u1",
}
#: A header larger than this is not a connector file (the real ones are < 2 KB).
_MAX_HEADER = 16 * 1024 * 1024


@dataclass(frozen=True)
class TensorInfo:
    """One tensor's place in the file."""

    dtype: np.dtype
    shape: tuple[int, ...]
    begin: int
    end: int

    @property
    def row_bytes(self) -> int:
        """Bytes per leading-axis row."""
        inner = int(np.prod(self.shape[1:], dtype=np.int64)) if len(self.shape) > 1 else 1
        return inner * self.dtype.itemsize


def _malformed(path: Path, detail: str) -> AttentionUnavailable:
    return AttentionUnavailable(
        "attention_record_malformed", f"{path.name}: {detail}", {"path": str(path)}
    )


class SafeTensorsFile:
    """Header plus ranged reads over one SafeTensors file."""

    def __init__(self, path: Path) -> None:
        """Parse the header of ``path`` (raises a typed reason when unusable)."""
        self.path = path
        try:
            with path.open("rb") as fh:
                raw_len = fh.read(8)
                if len(raw_len) != 8:
                    raise _malformed(path, "shorter than a SafeTensors header")
                (header_len,) = struct.unpack("<Q", raw_len)
                if header_len <= 0 or header_len > _MAX_HEADER:
                    raise _malformed(path, f"implausible header length {header_len}")
                header_raw = fh.read(header_len)
        except OSError as exc:
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"cannot read {path}: {type(exc).__name__}: {exc}",
                {"path": str(path)},
            ) from exc
        try:
            header: dict[str, Any] = json.loads(header_raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _malformed(path, f"header is not JSON: {exc}") from exc
        meta = header.pop("__metadata__", None) or {}
        self.metadata: dict[str, str] = {str(k): str(v) for k, v in meta.items()}
        self._data_start = 8 + header_len
        self.tensors: dict[str, TensorInfo] = {}
        for name, spec in header.items():
            try:
                dtype = np.dtype(_DTYPES[spec["dtype"]])
                shape = tuple(int(d) for d in spec["shape"])
                begin, end = (int(v) for v in spec["data_offsets"])
            except (KeyError, TypeError, ValueError) as exc:
                raise _malformed(path, f"tensor {name!r} header unreadable: {exc}") from exc
            expected = int(np.prod(shape, dtype=np.int64)) * dtype.itemsize
            if end - begin != expected:
                raise _malformed(
                    path, f"tensor {name!r} spans {end - begin} bytes, shape needs {expected}"
                )
            self.tensors[name] = TensorInfo(dtype, shape, begin, end)

    def info(self, name: str) -> TensorInfo:
        """The header entry for ``name`` (typed when the tensor is absent)."""
        if name not in self.tensors:
            raise _malformed(self.path, f"no tensor {name!r}")
        return self.tensors[name]

    def read(self, name: str) -> np.ndarray:
        """The whole tensor (use for the ``[T]`` and segment tensors only)."""
        info = self.info(name)
        return self._read_bytes(info, info.begin, info.end - info.begin).reshape(info.shape)

    def rows(self, name: str, first: int, last: int) -> np.ndarray:
        """Leading-axis rows ``first..last`` inclusive of a ``[G, ...]`` tensor."""
        info = self.info(name)
        if not info.shape or first < 0 or last >= info.shape[0] or last < first:
            raise _malformed(self.path, f"rows {first}..{last} outside {name} shape {info.shape}")
        count = last - first + 1
        offset = info.begin + first * info.row_bytes
        arr = self._read_bytes(info, offset, count * info.row_bytes)
        return arr.reshape((count, *info.shape[1:]))

    def _read_bytes(self, info: TensorInfo, offset: int, size: int) -> np.ndarray:
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._data_start + offset)
                raw = fh.read(size)
        except OSError as exc:
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"cannot read {self.path}: {type(exc).__name__}: {exc}",
                {"path": str(self.path)},
            ) from exc
        if len(raw) != size:
            raise _malformed(self.path, f"truncated: wanted {size} bytes, got {len(raw)}")
        return np.frombuffer(raw, dtype=info.dtype)
