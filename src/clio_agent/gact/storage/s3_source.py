"""Amazon S3 and S3-compatible buckets as read-only linked sources.

A source is one bucket and a key prefix (``bucket/prefix`` in its root). Listing pages
through ``ListObjectsV2`` under the prefix and records each object's ETag as its
revision; nothing is downloaded to link it. Reads are byte ranges (``GetObject`` with
``Range`` and ``IfMatch``), so a reader can seek inside a large object and only the
ranges it reads move. When the object changes upstream after it was listed, the read
fails with ``source_revision_changed`` instead of mixing revisions.

Credentials stay on the host: anonymous access needs none, an access key pair is held
by :class:`~clio_agent.gact.storage.auth.StorageAuth`, and a named profile is resolved by
botocore from the host's own configuration. None of them reach agent code.
"""

from __future__ import annotations

import io
import posixpath
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, cast

from clio_schemas.connected_resources import SourceCapabilities

from clio_agent.gact.storage.models import FileEntry, SourceConfiguration
from clio_agent.gact.storage.names import listed_child

#: Typed reasons carried by the errors this adapter raises.
S3_LISTING_OVER_CAP = "s3_listing_over_cap"
SOURCE_REVISION_CHANGED = "source_revision_changed"
S3_ACCESS_DENIED = "s3_access_denied"
MAX_ENTRIES = 100_000
_READ_ONLY = "S3 sources are linked read-only; write results to the workspace instead."


@dataclass(frozen=True)
class S3Location:
    """A bucket and the key prefix the source is limited to (no leading or trailing ``/``)."""

    bucket: str
    prefix: str

    @classmethod
    def parse(cls, root: str) -> S3Location:
        """Read ``bucket/prefix`` or ``s3://bucket/prefix``; a bucket alone is refused."""
        text = root.strip()
        if text.startswith("s3://"):
            text = text[5:]
        bucket, _, prefix = text.strip("/").partition("/")
        prefix = prefix.strip("/")
        if not bucket or any(c in bucket for c in " \\?#"):
            raise ValueError("Enter a bucket name, for example my-bucket/data/2024")
        if not prefix:
            raise ValueError(
                "Choose a prefix inside the bucket; a whole bucket is too large to link"
            )
        if any(part in {"", ".", ".."} for part in prefix.split("/")):
            raise ValueError("The prefix cannot contain empty, . or .. parts")
        return cls(bucket, prefix)

    @property
    def root(self) -> str:
        return f"{self.bucket}/{self.prefix}"

    def key(self, remote: str) -> str:
        return f"{self.prefix}/{remote}" if remote else self.prefix


def s3_client(configuration: SourceConfiguration, keys: tuple[str, str] | None = None) -> Any:
    """A botocore S3 client for the source's access choice; credentials never leave it."""
    import botocore.session  # noqa: PLC0415 - loaded when an S3 source is used
    from botocore import UNSIGNED  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    region = configuration.s3_region or None
    endpoint = configuration.s3_endpoint_url or None
    retries = {"max_attempts": 4, "mode": "standard"}
    if configuration.s3_access == "anonymous":
        session = botocore.session.get_session()
        config = Config(signature_version=UNSIGNED, retries=retries, max_pool_connections=16)
        return session.create_client("s3", region_name=region, endpoint_url=endpoint, config=config)
    config = Config(retries=retries, max_pool_connections=16)
    if configuration.s3_access == "profile":
        session = botocore.session.Session(profile=configuration.s3_profile or None)
        return session.create_client("s3", region_name=region, endpoint_url=endpoint, config=config)
    if keys is None:
        raise PermissionError("Enter the access key for this S3 source again on the connected CLIO")
    session = botocore.session.get_session()
    return session.create_client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        config=config,
        aws_access_key_id=keys[0],
        aws_secret_access_key=keys[1],
    )


def _denied(exc: Exception) -> Exception:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    if code in {"AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
        return PermissionError(f"{S3_ACCESS_DENIED}: the bucket refused this access ({code})")
    if code in {"NoSuchBucket", "404", "NoSuchKey"}:
        return FileNotFoundError(f"The bucket or object was not found ({code})")
    return exc


def _etag(revision: str) -> str:
    """The ``IfMatch`` value for a listed revision; empty when it is not an ETag."""
    text = revision.strip('"')
    if text and all(c in "0123456789abcdefABCDEF-" for c in text):
        return f'"{text}"'
    return ""


class S3RangeReader(io.RawIOBase):
    """A seekable reader over one object: each read is one ranged ``GetObject``."""

    def __init__(self, client: Any, bucket: str, key: str, size: int, etag: str) -> None:
        super().__init__()
        self.client, self.bucket, self.key = client, bucket, key
        self.size, self.etag = size, etag
        self.position = 0
        self.requests = 0
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = max(0, base + offset)
        return self.position

    def read_range(self, start: int, length: int) -> bytes:
        """``length`` bytes from ``start`` (fewer at the end), checked against the ETag."""
        if length <= 0 or start >= self.size:
            return b""
        end = min(self.size, start + length) - 1
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self.key,
            "Range": f"bytes={start}-{end}",
        }
        if self.etag:
            args["IfMatch"] = self.etag
        try:
            response = self.client.get_object(**args)
        except Exception as exc:  # botocore ClientError carries the S3 error code
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"PreconditionFailed", "412"}:
                raise ValueError(
                    f"{SOURCE_REVISION_CHANGED}: {self.key} changed upstream after it was "
                    "linked; refresh the link before reading it again"
                ) from None
            raise _denied(exc) from exc
        data = cast(bytes, response["Body"].read())
        self.requests += 1
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        data = self.read_range(self.position, len(view))
        view[: len(data)] = data
        self.position += len(data)
        return len(data)


class S3Folder:
    """One bucket prefix, listed without downloading and read by byte range."""

    capabilities = SourceCapabilities(
        link_folder=True,
        search=True,
        revision_check=True,
        supported_modes=["read_only"],
        unavailable_reasons={"working_copy": _READ_ONLY, "write_enabled": _READ_ONLY},
    )

    def __init__(self, client: Any, location: S3Location) -> None:
        self.client, self.location = client, location
        self.root = location.root
        self.writable = False
        self.skipped = 0

    def entries(
        self,
        folder: str = "",
        *,
        recursive: bool = True,
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[FileEntry]:
        """Objects (and the folders their keys imply) under the prefix, with ETags."""
        if folder:
            FileEntry(path=folder, kind="directory")
        base = self.location.key(folder) + "/"
        paginate: dict[str, Any] = {"Bucket": self.location.bucket, "Prefix": base}
        if not recursive:
            paginate["Delimiter"] = "/"
        rows: list[FileEntry] = []
        folders: set[str] = set()
        self.skipped = 0
        try:
            pages = self.client.get_paginator("list_objects_v2").paginate(**paginate)
            for page in pages:
                if cancelled is not None and cancelled():
                    raise InterruptedError("Folder indexing cancelled")
                for common in page.get("CommonPrefixes", []):
                    leaf = common["Prefix"][len(base) :].strip("/")
                    if leaf:
                        folders.add(posixpath.join(folder, leaf) if folder else leaf)
                for item in page.get("Contents", []):
                    key = item["Key"]
                    relative = key[len(base) :]
                    if not relative or relative.endswith("/"):
                        continue  # a folder marker object
                    parent, _, leaf = relative.rpartition("/")
                    remote_parent = posixpath.join(folder, parent) if folder else parent
                    child = listed_child(remote_parent, remote_parent, leaf)
                    if child is None:
                        self.skipped += 1
                        continue
                    path, remote = child
                    rows.append(
                        FileEntry(
                            path=path,
                            remote_path=remote if remote != path else "",
                            kind="file",
                            size=int(item.get("Size", 0)),
                            revision=str(item.get("ETag", "")).strip('"')
                            or str(item.get("LastModified", "")),
                            locator=key,
                        )
                    )
                    while parent:
                        folders.add(posixpath.join(folder, parent) if folder else parent)
                        parent = parent.rpartition("/")[0]
                    if len(rows) + len(folders) > MAX_ENTRIES:
                        raise ValueError(
                            f"{S3_LISTING_OVER_CAP}: more than {MAX_ENTRIES:,} objects under this "
                            "prefix; choose a longer prefix"
                        )
                    if progress is not None:
                        progress(len(rows))
        except (ValueError, InterruptedError):
            raise
        except Exception as exc:
            raise _denied(exc) from exc
        rows.extend(FileEntry(path=name, kind="directory", size=0) for name in sorted(folders))
        return sorted(rows, key=lambda row: row.path)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """A seekable byte-range reader for one listed object (buffered, 1 MiB reads)."""
        if not entry.locator.startswith(self.location.prefix + "/"):
            raise PermissionError("This object is outside the linked prefix")
        raw = S3RangeReader(
            self.client,
            self.location.bucket,
            entry.locator,
            int(entry.size or 0),
            _etag(entry.revision),
        )
        return cast(BinaryIO, io.BufferedReader(raw, buffer_size=1024 * 1024))

    def range_reader(self, entry: FileEntry) -> S3RangeReader:
        """The unbuffered reader, for callers that serve exact byte ranges."""
        return S3RangeReader(
            self.client,
            self.location.bucket,
            entry.locator,
            int(entry.size or 0),
            _etag(entry.revision),
        )

    def apply(self, path: str, content: Any, expected: str | None) -> str | None:
        raise PermissionError(_READ_ONLY)
