"""SSH-profile SFTP file access with OpenSSH routing and strict host-key checks."""

from __future__ import annotations

import getpass
import hashlib
import posixpath
import re
import shlex
import stat
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast

import paramiko
from clio_schemas.connected_resources import SourceCapabilities
from fsspec.implementations.sftp import SFTPFileSystem

from clio_agent.gact.storage.models import FileEntry


class _VerifiedSFTP(SFTPFileSystem):
    """Reuse an already verified client instead of fsspec's automatic key acceptance."""

    def _connect(self) -> None:
        self.client = self.ssh_kwargs.pop("verified_client")
        self.ftp = self.client.open_sftp()


def connect_profile(profile: str) -> paramiko.SSHClient:
    """Resolve an existing OpenSSH profile and verify every SSH connection's host key."""
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,159}", profile):
        raise ValueError("Select an existing SSH profile name")
    result = subprocess.run(
        ["ssh", "-G", profile], capture_output=True, text=True, timeout=15, check=False
    )
    if result.returncode:
        raise ValueError("The SSH profile could not be resolved on this CLIO")
    settings: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        settings.setdefault(key, []).append(value)
    host = settings.get("hostname", [profile])[0]
    port = int(settings.get("port", ["22"])[0])
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known = Path.home() / ".ssh" / "known_hosts"
    if known.exists():
        client.load_host_keys(str(known))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    jump = settings.get("proxyjump", [""])[0]
    proxy = settings.get("proxycommand", [""])[0]
    socket = None
    if jump and jump != "none":
        # OpenSSH interprets the trusted user's hop route and performs its own
        # strict verification; the final SFTP host is checked by Paramiko too.
        hops = jump.split(",")
        socket = paramiko.ProxyCommand(
            shlex.join(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    *(["-J", ",".join(hops[:-1])] if len(hops) > 1 else []),
                    "-W",
                    f"{host}:{port}",
                    hops[-1],
                ]
            )
        )
    elif proxy and proxy != "none":
        socket = paramiko.ProxyCommand(
            proxy.replace("%h", host)
            .replace("%p", str(port))
            .replace("%r", settings.get("user", [getpass.getuser()])[0])
        )
    identities = [
        str(Path(path).expanduser())
        for path in settings.get("identityfile", [])
        if Path(path).expanduser().is_file()
    ]
    try:
        client.connect(
            host,
            port=port,
            username=settings.get("user", [getpass.getuser()])[0],
            key_filename=cast(Any, identities or None),  # Paramiko accepts a string or list.
            sock=socket,
            timeout=20,
            auth_timeout=20,
            banner_timeout=20,
        )
    except (OSError, paramiko.SSHException) as exc:
        client.close()
        if socket is not None:
            socket.close()
        raise ValueError(
            "SSH sign-in or host verification failed. Connect this profile in your terminal first."
        ) from exc
    return client


class SftpSource:
    """Materialized SFTP access: read-only or reviewed copy, never a pretend mount."""

    def __init__(self, profile: str, root: str, *, writable: bool = False) -> None:
        if not PurePosixPath(root).is_absolute() or ".." in PurePosixPath(root).parts:
            raise ValueError("Select an absolute folder on the SFTP host")
        self.fs = _VerifiedSFTP(
            profile, verified_client=connect_profile(profile), skip_instance_cache=True
        )
        self.root = self.fs.ftp.normalize(root)
        if not stat.S_ISDIR(self.fs.ftp.stat(self.root).st_mode):
            self.close()
            raise ValueError("Select an existing SFTP directory")
        self.writable = writable
        self.capabilities = SourceCapabilities(
            search=True,
            revision_check=True,
            conditional_write=False,
            supported_modes=["read_only", "working_copy"],
            unavailable_reasons={
                "write_enabled": "SFTP provides file transfers, not a writable operating-system folder on this CLIO."
            },
        )

    def close(self) -> None:
        """Release the source transport and its forwarding channel."""
        self.fs.ftp.close()
        self.fs.client.close()

    def _path(self, relative: str) -> str:
        FileEntry(path=relative, kind="file")
        current = self.root
        for part in PurePosixPath(relative).parts:
            current = posixpath.join(current, part)
            try:
                mode = self.fs.ftp.lstat(current).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError("SFTP symbolic links require an explicit source selection")
        return current

    def _hash(self, path: str) -> str:
        with self.fs.open(path, "rb") as reader:
            digest = hashlib.sha256()
            while chunk := reader.read(1024 * 1024):
                digest.update(cast(bytes, chunk))
            return digest.hexdigest()

    def entries(self) -> list[FileEntry]:
        """Read content hashes, so same-size and coarse-mtime edits still conflict."""
        rows: list[FileEntry] = []
        pending = [""]
        while pending:
            relative = pending.pop()
            for item in self.fs.ftp.listdir_attr(posixpath.join(self.root, relative)):
                name = posixpath.join(relative, item.filename)
                path = self._path(name)
                if len(rows) >= 100_000:
                    raise ValueError("Select a smaller source folder (limit 100,000 entries)")
                if stat.S_ISDIR(item.st_mode):
                    rows.append(FileEntry(path=name, kind="directory"))
                    pending.append(name)
                elif stat.S_ISREG(item.st_mode):
                    digest = self._hash(path)
                    rows.append(
                        FileEntry(
                            path=name,
                            kind="file",
                            size=item.st_size,
                            revision=digest,
                            sha256=digest,
                        )
                    )
                else:
                    raise ValueError("SFTP sources may contain only regular files and directories")
        return sorted(rows, key=lambda row: row.path)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Stream through fsspec on the verified SSH connection."""
        return cast(BinaryIO, self.fs.open(self._path(entry.path), "rb"))

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Apply one selected change with optimistic hash checks and a sibling stage."""
        if not self.writable:
            raise PermissionError("This SFTP source has not been approved for writes")
        destination = self._path(path)
        actual = self._hash(destination) if self.fs.isfile(destination) else None
        if actual != expected:
            raise ValueError(f"Upstream changed after review: {path}")
        if content is None:
            if actual is not None:
                self.fs.rm_file(destination)
            return None
        self.fs.makedirs(posixpath.dirname(destination), exist_ok=True)
        stage = posixpath.join(posixpath.dirname(destination), ".clio-transfer-" + uuid.uuid4().hex)
        try:
            self.fs.put_file(str(content), stage)
            actual = self._hash(destination) if self.fs.isfile(destination) else None
            if actual != expected:
                raise ValueError(f"Upstream changed after review: {path}")
            # Refuse servers without atomic replacement support instead of
            # deleting the previous file before a potentially failing rename.
            self.fs.ftp.posix_rename(stage, destination)
        finally:
            if self.fs.exists(stage):
                self.fs.rm_file(stage)
        return self._hash(destination)
