#!/usr/bin/env python3
"""Copy a reference store (``scripts/create_reference_store.py --out``) so the copy is independent.

A plain ``cp -a`` is NOT an independent store. clio-core keeps the absolute paths of its
storage in three places besides ``cte.yaml``, and a daemon started on the copy follows
them back to the source, so it reads and WRITES the source's ``storage.bin``:

* ``home/data/cte/hosts/<host>/conf/wal/pools.<n>.bin``: the pool write-ahead log, a
  binary log whose records embed each pool's compose YAML (storage and metadata-log
  paths) behind length fields;
* ``conf/models/clio_bdev.<path with / as _>.0.0.yaml``: one model file per file bdev,
  named after its path and naming it inside;
* text files: ``cte.yaml``, ``env.sh``, ``summary.json``, ``runtime/clio-runtime.version``,
  the plugin state and the workspace registry.

This copies ``SRC`` to ``DST`` keeping sparse files sparse, rewrites every absolute
``SRC`` path in all of them (the WAL record by record, fixing its length fields, so the
two paths need not be the same length), clears the source's runtime client registry,
optionally moves the copy to its own clio-core port, then reads every byte of the copy
and refuses (exit 1, the copy removed) if any still names ``SRC``.

It refuses before copying when:

* a process holds the source store open (a live daemon: the copy would be torn);
* ``DST`` exists (pass ``--replace`` to remove it first);
* a binary file other than the WAL names ``SRC`` and the paths differ in length (its
  format is unknown here, so it can only be rewritten in place, byte for byte).

Usage::

    python scripts/copy_reference_store.py SRC DST [--port 29071] [--replace]
    source DST/env.sh   # the copy's environment (CLIO_AGENT_HOME, ..., CLIO_CORE_PORT)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from clio_agent.arc import clio_core_relocate as _relocate  # noqa: E402

# The pool WAL's record layout and its rewrite are the product's
# (:mod:`clio_agent.arc.clio_core_relocate`, which relocates a moved data folder at start).
_WAL_GLOB = "home/data/cte/hosts/*/conf/wal/pools.*.bin"
_MODEL_GLOB = "home/data/cte/hosts/*/conf/models/*"
_PORT_LINE = re.compile(rb"^(  port: )(\d+)$", re.MULTILINE)
_ENV_PORT = re.compile(rb"^(export CLIO_CORE_PORT=)(\d+)$", re.MULTILINE)


class CopyRefused(Exception):
    """The copy cannot be made independent; the message is the cause."""


# ---- the pool WAL ---------------------------------------------------------------------


def _wal_records(data: bytes, name: str) -> list[tuple[bytes, list[bytes]]]:
    """Split the WAL into records (:func:`clio_core_relocate.wal_records`)."""
    try:
        return _relocate.wal_records(data, name)
    except _relocate.WalFormatError as exc:
        raise CopyRefused(str(exc)) from exc


def rewrite_wal(data: bytes, src: bytes, dst: bytes, name: str = "wal") -> bytes:
    """The WAL with every ``src`` path replaced by ``dst`` and every length field fixed.

    Raises:
        CopyRefused: The WAL does not parse, or a path sits where it cannot be rewritten.
    """
    old, new = src.decode("utf-8"), dst.decode("utf-8")
    try:
        rewritten = _relocate.rewrite_wal(data, src, lambda text: text.replace(old, new), name)
    except _relocate.WalFormatError as exc:
        raise CopyRefused(str(exc)) from exc
    if src in rewritten:
        raise CopyRefused(f"{name}: a path survived the rewrite")
    return rewritten


# ---- the copy -------------------------------------------------------------------------


def _holders(src: Path) -> list[int]:
    """Processes with a file under ``src`` open (Linux ``/proc``; empty elsewhere)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    prefix = str(src) + os.sep
    pids: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            targets = [os.readlink(fd) for fd in (entry / "fd").iterdir()]
        except OSError:
            continue
        if any(target.startswith(prefix) for target in targets):
            pids.append(int(entry.name))
    return pids


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _mangled(path: bytes) -> bytes:
    """A path as clio-core names a file bdev's model file (``/`` as ``_``)."""
    return _relocate.mangled(path.decode("utf-8")).encode("utf-8")


def _files(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()]


def _contains(path: Path, needle: bytes) -> bool:
    """Whether ``path`` holds ``needle``, read in blocks (stores hold GB-sized files)."""
    block = 1 << 22
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(block):
            if needle in tail + chunk:
                return True
            tail = chunk[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return False


def rewrite_copy(dst: Path, src: bytes, new: bytes, port: int | None) -> list[str]:
    """Rewrite every reference to ``src`` under ``dst``; return what changed."""
    changed: list[str] = []
    wal = {path.resolve() for path in dst.glob(_WAL_GLOB)}
    for path in _files(dst):
        if path.resolve() in wal:
            data = path.read_bytes()
            if src in data:
                path.write_bytes(rewrite_wal(data, src, new, str(path.relative_to(dst))))
                changed.append(f"wal {path.relative_to(dst)}")
            continue
        if path.stat().st_size > (64 << 20):  # stores: checked after, never rewritten
            continue
        data = path.read_bytes()
        if src not in data:
            continue
        if _is_text(data):
            path.write_bytes(data.replace(src, new))
            changed.append(f"text {path.relative_to(dst)}")
        elif len(src) == len(new):
            path.write_bytes(data.replace(src, new))
            changed.append(f"binary-same-length {path.relative_to(dst)}")
        else:
            raise CopyRefused(
                f"{path.relative_to(dst)} is a binary file of unknown format naming the "
                f"source path, and the paths differ in length ({len(src)} vs {len(new)}): "
                "choose a destination path of the same length"
            )
    for path in sorted(dst.glob(_MODEL_GLOB)):
        if _mangled(src) in path.name.encode():
            target = path.with_name(
                path.name.replace(_mangled(src).decode(), _mangled(new).decode())
            )
            path.rename(target)
            changed.append(f"rename {path.relative_to(dst)} -> {target.name}")
    clients = dst / "runtime" / "clio-runtime.clients"
    if clients.is_dir():
        for entry in clients.iterdir():
            entry.unlink()
        changed.append("cleared runtime/clio-runtime.clients")
    if port is not None:
        changed += _set_port(dst, port)
    return changed


def _set_port(dst: Path, port: int) -> list[str]:
    changed: list[str] = []
    for path in dst.glob("home/data/cte/hosts/*/cte.yaml"):
        data, count = _PORT_LINE.subn(rb"\g<1>" + str(port).encode(), path.read_bytes())
        if count != 1:
            raise CopyRefused(f"{path.relative_to(dst)}: expected one networking port line")
        path.write_bytes(data)
        changed.append(f"port {port} in {path.relative_to(dst)}")
    env = dst / "env.sh"
    if env.is_file():
        data = env.read_bytes()
        data, count = _ENV_PORT.subn(rb"\g<1>" + str(port).encode(), data)
        if count == 0:
            data += f"export CLIO_CORE_PORT={port}\n".encode()
        env.write_bytes(data)
        changed.append(f"port {port} in env.sh")
    return changed


def verify(dst: Path, src: bytes) -> list[str]:
    """Every file (content or name) under ``dst`` that still names ``src``."""
    left: list[str] = []
    for path in _files(dst):
        name = str(path.relative_to(dst)).encode()
        if src in name or _mangled(src) in name or _contains(path, src):
            left.append(str(path.relative_to(dst)))
    return left


def copy_store(src: Path, dst: Path, *, port: int | None, replace: bool) -> list[str]:
    """Copy ``src`` to ``dst`` as an independent store; return the changes made."""
    if not (src / "home").is_dir():
        raise CopyRefused(f"{src} has no home/: not a reference store")
    holders = _holders(src)
    if holders:
        raise CopyRefused(f"processes {holders} hold {src} open: stop them before copying")
    if dst.exists():
        if not replace:
            raise CopyRefused(f"{dst} exists (pass --replace to remove it first)")
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-a", "--sparse=always", str(src), str(dst)], check=True)
    try:
        changed = rewrite_copy(dst, os.fsencode(src), os.fsencode(dst), port)
        left = verify(dst, os.fsencode(src))
        if left:
            raise CopyRefused("the copy still names the source in: " + ", ".join(left))
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("src", type=Path, help="the reference store to copy")
    parser.add_argument("dst", type=Path, help="where the independent copy goes")
    parser.add_argument("--port", type=int, help="clio-core port of the copy")
    parser.add_argument("--replace", action="store_true", help="remove an existing DST first")
    args = parser.parse_args(argv)
    src, dst = args.src.resolve(), args.dst.resolve()
    try:
        changed = copy_store(src, dst, port=args.port, replace=args.replace)
    except CopyRefused as refused:
        print(f"copy_reference_store refused: {refused}", file=sys.stderr)
        return 1
    for line in changed:
        print(line)
    print(f"copied {src} -> {dst}; no file of the copy names the source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
