"""Read the connector's SafeTensors attention file, a tensor or a row range at a time.

The format is fixed and tiny: an 8-byte little-endian header length, a JSON
header ``{name: {dtype, shape, data_offsets: [begin, end]}, "__metadata__":
{str: str}}``, then the raw little-endian tensor bytes. A response's step
tensors are ``[G, k]`` (up to ~200 MB per file), and a selection needs only a
handful of rows, so rows are read by byte range instead of loading the file --
batched per call, because the bytes may come over a shell to the GPU node
(:mod:`.byte_source`).

Every structural problem is ``attention_record_malformed``; an unreadable file
is ``attention_file_unavailable``. Nothing is guessed.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from clio_agent.gact.attention.byte_source import ByteSource
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


class SafeTensorsFile:
    """Header plus batched ranged reads over one SafeTensors file."""

    def __init__(self, source: ByteSource) -> None:
        """Parse the header from ``source`` (raises a typed reason when unusable)."""
        self.source = source
        (raw_len,) = source.read_many([(0, 8)])
        if len(raw_len) != 8:
            raise self._malformed("shorter than a SafeTensors header")
        (header_len,) = struct.unpack("<Q", raw_len)
        if header_len <= 0 or header_len > _MAX_HEADER:
            raise self._malformed(f"implausible header length {header_len}")
        (header_raw,) = source.read_many([(8, header_len)])
        try:
            header: dict[str, Any] = json.loads(header_raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise self._malformed(f"header is not JSON: {exc}") from exc
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
                raise self._malformed(f"tensor {name!r} header unreadable: {exc}") from exc
            expected = int(np.prod(shape, dtype=np.int64)) * dtype.itemsize
            if end - begin != expected:
                raise self._malformed(
                    f"tensor {name!r} spans {end - begin} bytes, shape needs {expected}"
                )
            self.tensors[name] = TensorInfo(dtype, shape, begin, end)

    def _malformed(self, detail: str) -> AttentionUnavailable:
        return AttentionUnavailable(
            "attention_record_malformed",
            f"{self.source.label}: {detail}",
            {"path": self.source.label},
        )

    def info(self, name: str) -> TensorInfo:
        """The header entry for ``name`` (typed when the tensor is absent)."""
        if name not in self.tensors:
            raise self._malformed(f"no tensor {name!r}")
        return self.tensors[name]

    def read_tensors(self, names: list[str]) -> dict[str, np.ndarray]:
        """Whole tensors (the ``[T]`` and segment tensors), one batched read."""
        infos = [self.info(n) for n in names]
        blobs = self.source.read_many(
            [(self._data_start + i.begin, i.end - i.begin) for i in infos]
        )
        return {
            n: self._array(i, blob, i.end - i.begin).reshape(i.shape)
            for n, i, blob in zip(names, infos, blobs, strict=True)
        }

    def read(self, name: str) -> np.ndarray:
        """One whole tensor."""
        return self.read_tensors([name])[name]

    def rows_many(self, names: list[str], first: int, last: int) -> dict[str, np.ndarray]:
        """Leading-axis rows ``first..last`` inclusive of several ``[G, ...]`` tensors."""
        infos = [self.info(n) for n in names]
        for name, info in zip(names, infos, strict=True):
            if not info.shape or first < 0 or last >= info.shape[0] or last < first:
                raise self._malformed(f"rows {first}..{last} outside {name} shape {info.shape}")
        count = last - first + 1
        blobs = self.source.read_many(
            [(self._data_start + i.begin + first * i.row_bytes, count * i.row_bytes) for i in infos]
        )
        return {
            n: self._array(i, blob, count * i.row_bytes).reshape((count, *i.shape[1:]))
            for n, i, blob in zip(names, infos, blobs, strict=True)
        }

    def rows(self, name: str, first: int, last: int) -> np.ndarray:
        """Rows of one tensor."""
        return self.rows_many([name], first, last)[name]

    def _array(self, info: TensorInfo, blob: bytes, size: int) -> np.ndarray:
        if len(blob) != size:
            raise self._malformed(f"truncated: wanted {size} bytes, got {len(blob)}")
        return np.frombuffer(blob, dtype=info.dtype)
