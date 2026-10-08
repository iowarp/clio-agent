"""Target-side read of what context sizing needs; runs on the execution host.

Standalone (stdlib only) so the host need not have CLIO installed: it is sent
as ``python3 -c <this file>`` with a JSON request on stdin, like
:mod:`clio_agent.gact.infrastructure.node_models`. For a Hugging Face model
directory it reads ``config.json`` and sums the weight files; for a GGUF file it
reads the metadata header (never the tensors) and the file size(s); in both
cases it reports each GPU's total and free memory (``nvidia-smi``, else
``rocm-smi``). The answer is one line, ``clio-context-probe <json>``, kept small:
only the model's own ``<arch>.*`` scalars and short numeric arrays.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO

MARKER = "clio-context-probe "
GGUF_MAGIC = b"GGUF"
#: Arrays longer than this (token tables) are skipped, not reported.
MAX_ARRAY = 4096
_SCALARS = {
    0: "<B",
    1: "<b",
    2: "<H",
    3: "<h",
    4: "<I",
    5: "<i",
    6: "<f",
    7: "<?",
    10: "<Q",
    11: "<q",
    12: "<d",
}
_STRING, _ARRAY = 8, 9
_NUMBERS = (bool, int, float)
#: A config.json entry larger than this (a quantization table) is not reported.
MAX_CONFIG_VALUE = 2000
_SPLIT = re.compile(r"^(?P<stem>.*)-(?P<index>\d{5})-of-(?P<count>\d{5})\.gguf$")


def _read(handle: BinaryIO, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("truncated GGUF header")
    return data


def _string(handle: BinaryIO, version: int) -> str:
    fmt = "<I" if version == 1 else "<Q"
    (length,) = struct.unpack(fmt, _read(handle, struct.calcsize(fmt)))
    return _read(handle, length).decode("utf-8", "replace")


def _value(handle: BinaryIO, kind: int, version: int) -> Any:
    if kind in _SCALARS:
        fmt = _SCALARS[kind]
        return struct.unpack(fmt, _read(handle, struct.calcsize(fmt)))[0]
    if kind == _STRING:
        return _string(handle, version)
    if kind == _ARRAY:
        (item_kind,) = struct.unpack("<I", _read(handle, 4))
        count_fmt = "<I" if version == 1 else "<Q"
        (count,) = struct.unpack(count_fmt, _read(handle, struct.calcsize(count_fmt)))
        if item_kind in _SCALARS and count > MAX_ARRAY:
            handle.seek(count * struct.calcsize(_SCALARS[item_kind]), 1)
            return None
        items = [_value(handle, item_kind, version) for _ in range(count)]
        return None if item_kind in (_STRING, _ARRAY) else items
    raise ValueError(f"unknown GGUF value type {kind}")


def gguf_metadata(handle: BinaryIO) -> dict[str, Any]:
    """The GGUF key/value metadata of an open model file (header only)."""

    if _read(handle, 4) != GGUF_MAGIC:
        raise ValueError("not a GGUF file")
    (version,) = struct.unpack("<I", _read(handle, 4))
    count_fmt = "<I" if version == 1 else "<Q"
    size = struct.calcsize(count_fmt)
    _tensors = struct.unpack(count_fmt, _read(handle, size))[0]
    (pairs,) = struct.unpack(count_fmt, _read(handle, size))
    metadata: dict[str, Any] = {}
    for _ in range(pairs):
        key = _string(handle, version)
        (kind,) = struct.unpack("<I", _read(handle, 4))
        metadata[key] = _value(handle, kind, version)
    return metadata


def relevant(metadata: dict[str, Any]) -> dict[str, Any]:
    """Only what sizing reads: the architecture and its own numeric keys."""

    architecture = metadata.get("general.architecture")
    kept: dict[str, Any] = {"general.architecture": architecture}
    for key, value in metadata.items():
        if not architecture or not key.startswith(f"{architecture}."):
            continue
        if isinstance(value, _NUMBERS) or (
            isinstance(value, list) and all(isinstance(v, _NUMBERS) for v in value)
        ):
            kept[key] = value
    return kept


def small_config(config: dict[str, Any]) -> dict[str, Any]:
    """``config.json`` without oversized entries (the answer's tail is bounded)."""

    kept = {}
    for key, value in config.items():
        if isinstance(value, dict):
            value = small_config(value)
        if len(json.dumps(value)) <= MAX_CONFIG_VALUE:
            kept[key] = value
    return kept


def gguf_files(path: Path) -> list[Path]:
    """The file and, for a split model (``-00001-of-00003.gguf``), its siblings."""

    match = _SPLIT.match(path.name)
    if match is None:
        return [path]
    count = int(match.group("count"))
    return [
        path.with_name(f"{match.group('stem')}-{index:05d}-of-{count:05d}.gguf")
        for index in range(1, count + 1)
    ]


def weights_bytes(path: Path) -> int:
    """Bytes of the model's weights: GGUF file(s), or a directory's weight files."""

    if path.is_file():
        return sum(item.stat().st_size for item in gguf_files(path) if item.is_file())
    for pattern in ("*.safetensors", "*.bin", "*.pt"):
        found = [item for item in path.glob(pattern) if item.is_file()]
        if found:
            return sum(item.stat().st_size for item in found)
    return 0


def _run(command: list[str]) -> str:
    if shutil.which(command[0]) is None:
        return ""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def parse_nvidia_smi(text: str) -> list[dict[str, int]]:
    """``memory.total,memory.free`` in MiB per line -> bytes per GPU."""

    gpus = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            gpus.append({"total": int(parts[0]) << 20, "free": int(parts[1]) << 20})
    return gpus


def parse_rocm_smi(text: str) -> list[dict[str, int]]:
    """``rocm-smi --showmeminfo vram --json`` -> bytes per GPU."""

    try:
        payload = json.loads(text)
    except ValueError:
        return []
    gpus = []
    for card in payload.values() if isinstance(payload, dict) else []:
        if not isinstance(card, dict):
            continue
        total = next((v for k, v in card.items() if "Total Memory" in k and "Used" not in k), None)
        used = next((v for k, v in card.items() if "Total Used Memory" in k), None)
        if str(total).isdigit() and str(used).isdigit():
            gpus.append({"total": int(total), "free": int(total) - int(used)})
    return gpus


def gpu_memory() -> list[dict[str, int]]:
    """Total and free memory of every visible GPU, empty when none is reported."""

    nvidia = _run(
        [
            "nvidia-smi",
            "--query-gpu=memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ]
    )
    return parse_nvidia_smi(nvidia) or parse_rocm_smi(
        _run(["rocm-smi", "--showmeminfo", "vram", "--json"])
    )


def probe(request: dict[str, Any]) -> dict[str, Any]:
    """Answer one request ``{"kind": "hf" | "gguf", "path": ...}``."""

    result: dict[str, Any] = {"gpus": gpu_memory()}
    path = Path(str(request.get("path") or ""))
    if not path.is_absolute() or not path.exists():
        result["error"] = "model not found on this host"
        return result
    result["weights_bytes"] = weights_bytes(path)
    try:
        if request.get("kind") == "gguf":
            with path.open("rb") as handle:
                result["gguf"] = relevant(gguf_metadata(handle))
        else:
            config = json.loads((path / "config.json").read_text(encoding="utf-8"))
            result["config"] = small_config(config if isinstance(config, dict) else {})
    except (OSError, ValueError, struct.error) as exc:
        result["error"] = f"model layout not readable: {exc}"
    return result


def main() -> None:
    """Read the JSON request from stdin and print the marked answer."""

    print(MARKER + json.dumps(probe(json.loads(sys.stdin.read() or "{}"))))


if __name__ == "__main__":
    main()
