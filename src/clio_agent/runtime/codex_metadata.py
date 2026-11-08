"""Host-side ownership of Codex's synthetic Linux metadata mount targets.

The fence argv is unchanged. A gated supervisor records ownership before Codex
starts and retains crash evidence in the Agent instance's protected config tree.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# codex-rs/protocol/src/permissions.rs, rust-v0.163.0.
PROTECTED_NAMES = (".git", ".agents", ".codex", ".aws")


def wrapper_argv(
    command: str, args: Sequence[str], roots: Sequence[Path | str], marker_dir: Path
) -> tuple[str, list[str]]:
    """Wrap an unchanged Linux fence, keeping its marker store outside child writes."""
    marker_dir = marker_dir.resolve()
    normalized = sorted({str(Path(root).resolve()) for root in roots})
    covering = [Path(root) for root in normalized if marker_dir.is_relative_to(root)]
    if covering:
        nearest = max(covering, key=lambda root: len(root.parts))
        relative = marker_dir.relative_to(nearest)
        if not relative.parts or relative.parts[0] not in PROTECTED_NAMES:
            raise ValueError("Codex metadata marker storage must remain read-only to the child")
    return sys.executable, [
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "--markers",
        str(marker_dir),
        "--roots",
        json.dumps(normalized),
        "--",
        command,
        *args,
    ]


def _process_identity(pid: int) -> list[int] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except FileNotFoundError:
        return None
    if fields[0] == "Z":
        return None
    return [pid, int(fields[19])]


def _valid_identity(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(type(part) is int and part >= 0 for part in value)
    )


def _alive(owner: dict[str, Any]) -> bool:
    for identity in (owner["supervisor"], owner["child"]):
        if identity is not None and _process_identity(identity[0]) == identity:
            return True
    return False


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Walk canonical absolute components without following replacement symlinks."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _anchor(root: Path) -> tuple[Path, list[int]]:
    candidate = root
    while True:
        try:
            descriptor = _open_directory(candidate)
        except FileNotFoundError:
            candidate = candidate.parent
            continue
        try:
            identity = os.fstat(descriptor)
            return candidate, [identity.st_dev, identity.st_ino]
        finally:
            os.close(descriptor)


class _Marker:
    def __init__(self, directory: int, root: Path) -> None:
        self.directory = directory
        self.root = root
        self.key = hashlib.sha256(str(root).encode()).hexdigest()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl  # noqa: PLC0415 - Linux supervisor only

        descriptor = os.open(
            f"{self.key}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read(self) -> dict[str, Any] | None:
        try:
            descriptor = os.open(
                f"{self.key}.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory
            )
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor) as stream:
            data = json.load(stream)
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
            or data.get("root") != str(self.root)
            or not isinstance(data.get("absent"), list)
            or any(name not in PROTECTED_NAMES for name in data["absent"])
            or not isinstance(data.get("anchor"), str)
            or not self.root.is_relative_to(data["anchor"])
            or not _valid_identity(data.get("identity"))
            or not isinstance(data.get("owners"), list)
        ):
            raise ValueError("Invalid Codex metadata ownership marker")
        for owner in data["owners"]:
            if (
                not isinstance(owner, dict)
                or not isinstance(owner.get("token"), str)
                or not _valid_identity(owner.get("supervisor"))
                or owner["supervisor"][0] == 0
                or "child" not in owner
                or (owner["child"] is not None and not _valid_identity(owner["child"]))
            ):
                raise ValueError("Invalid Codex metadata process ownership")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        temporary = f"{self.key}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory,
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(
                temporary,
                f"{self.key}.json",
                src_dir_fd=self.directory,
                dst_dir_fd=self.directory,
            )
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def _new(self) -> dict[str, Any]:
        anchor, identity = _anchor(self.root)
        return {
            "version": 1,
            "root": str(self.root),
            "anchor": str(anchor),
            "identity": identity,
            "absent": list(PROTECTED_NAMES) if anchor != self.root else [],
            "owners": [],
        }

    @contextmanager
    def _root(self, data: dict[str, Any]) -> Iterator[int | None]:
        anchor = Path(data["anchor"])
        try:
            descriptor = _open_directory(anchor)
        except FileNotFoundError:
            yield None
            return
        try:
            identity = os.fstat(descriptor)
            if [identity.st_dev, identity.st_ino] != data["identity"]:
                logger.warning("codex_metadata_root_replaced root=%s", self.root)
                yield None
                return
            for part in self.root.relative_to(anchor).parts:
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    yield None
                    return
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def _sweep(self, data: dict[str, Any]) -> None:
        with self._root(data) as descriptor:
            if descriptor is not None:
                for name in data["absent"]:
                    try:
                        identity = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        if stat.S_ISDIR(identity.st_mode):
                            os.rmdir(name, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                            raise
        data["absent"] = []

    def prepare(self, owner: dict[str, Any]) -> None:
        with self._locked():
            data = self._read()
            if data is not None:
                data["owners"] = [entry for entry in data["owners"] if _alive(entry)]
                if not data["owners"]:
                    self._sweep(data)
                    data = None
            if data is None:
                data = self._new()
            with self._root(data) as descriptor:
                for name in PROTECTED_NAMES:
                    if descriptor is None:
                        continue
                    try:
                        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        continue
                    except FileNotFoundError:
                        pass
                    if name not in data["absent"]:
                        data["absent"].append(name)
            data["owners"].append(dict(owner))
            self._write(data)

    def set_child(self, owner: dict[str, Any]) -> None:
        with self._locked():
            data = self._read()
            if data is None:
                raise ValueError("Codex metadata ownership disappeared before launch")
            entry = next((item for item in data["owners"] if item["token"] == owner["token"]), None)
            if entry is None:
                raise ValueError("Codex metadata process ownership disappeared before launch")
            entry["child"] = owner["child"]
            self._write(data)

    def finish(self, token: str) -> None:
        with self._locked():
            data = self._read()
            if data is None:
                raise ValueError("Codex metadata ownership disappeared before cleanup")
            data["owners"] = [
                entry for entry in data["owners"] if entry["token"] != token and _alive(entry)
            ]
            if data["owners"]:
                self._write(data)
            else:
                self._sweep(data)
                os.unlink(f"{self.key}.json", dir_fd=self.directory)
                os.fsync(self.directory)


def _exec_gate(parent: int, descriptor: int, argv: list[str]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Could not bind Codex to its metadata supervisor")
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGKILL)
    permitted = os.read(descriptor, 1)
    os.close(descriptor)
    if permitted != b"1":
        raise ValueError("Codex metadata launch gate closed before ownership was recorded")
    os.execvpe(argv[0], argv, os.environ)


def run(marker_dir: Path, roots: Sequence[Path], argv: list[str]) -> int:
    """Observe every exit; SIGKILL retains markers for the next launch to recover."""
    directory = _open_directory(marker_dir, create=True)
    owner = {"token": uuid.uuid4().hex, "supervisor": _process_identity(os.getpid()), "child": None}
    markers: list[_Marker] = []
    process: subprocess.Popen[bytes] | None = None
    pending_signal: int | None = None
    read_gate: int | None = None
    write_gate: int | None = None
    code: int | None = None

    def forward(signum: int, _frame: Any) -> None:
        nonlocal pending_signal
        pending_signal = signum
        if process is not None and process.poll() is None:
            process.send_signal(signum)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for root in roots:
            marker = _Marker(directory, root)
            marker.prepare(owner)
            markers.append(marker)
        if pending_signal is not None:
            return -pending_signal
        read_gate, write_gate = os.pipe()
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                "--exec",
                str(os.getpid()),
                str(read_gate),
                *argv,
            ],
            pass_fds=(read_gate,),
        )
        os.close(read_gate)
        read_gate = None
        owner["child"] = _process_identity(process.pid)
        if owner["child"] is None:
            raise ValueError("Codex exited before metadata ownership was recorded")
        for marker in markers:
            marker.set_child(owner)
        os.write(write_gate, b"1")
        os.close(write_gate)
        write_gate = None
        if pending_signal is not None and process.poll() is None:
            process.send_signal(pending_signal)
        code = process.wait()
        return code
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for descriptor in (read_gate, write_gate):
            if descriptor is not None:
                os.close(descriptor)
        failures = []
        for marker in markers:
            try:
                marker.finish(owner["token"])
            except (OSError, ValueError) as exc:
                failures.append(exc)
        os.close(directory)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if failures:
            logger.error(
                "codex_metadata_cleanup_failed child_exit_code=%s errors=%s", code, failures
            )
            if code is None or code == 0:
                raise ExceptionGroup("Codex metadata cleanup failed", failures)


def main() -> int:
    if sys.platform != "linux":
        raise RuntimeError("Codex synthetic mount cleanup is Linux-only")
    if sys.argv[1:2] == ["--exec"]:
        _exec_gate(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:])
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--markers", type=Path, required=True)
    parser.add_argument("--roots", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        code = run(args.markers, [Path(root) for root in json.loads(args.roots)], args.command[1:])
    except (OSError, ValueError, ExceptionGroup) as exc:
        logger.error("codex_metadata_cleanup_failed: %s", exc)
        return 1
    if code < 0:
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
