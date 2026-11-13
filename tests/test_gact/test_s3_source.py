"""S3 as a read-only linked source: prefix listing, ranged reads, and host-held keys."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import botocore.session
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber

from clio_agent.gact.storage import s3_keys
from clio_agent.gact.storage.models import CreateSource, FileEntry, SourceConfiguration
from clio_agent.gact.storage.s3_source import (
    SOURCE_REVISION_CHANGED,
    S3Folder,
    S3Location,
    S3RangeReader,
)
from clio_agent.gact.storage.service import provider_capabilities


def _client():
    session = botocore.session.get_session()
    return session.create_client(
        "s3",
        region_name="us-west-2",
        config=Config(signature_version=UNSIGNED),
    )


def _object(key: str, size: int, etag: str) -> dict:
    return {
        "Key": key,
        "Size": size,
        "ETag": f'"{etag}"',
        "LastModified": datetime(2024, 9, 30, tzinfo=timezone.utc),
    }


def test_a_location_needs_a_bucket_and_a_prefix() -> None:
    assert S3Location.parse("s3://era/pl/202409/").root == "era/pl/202409"
    with pytest.raises(ValueError, match="prefix"):
        S3Location.parse("era")
    with pytest.raises(ValueError):
        S3Location.parse("era/../x")


def test_listing_pages_through_the_prefix_and_implies_folders() -> None:
    client = _client()
    folder = S3Folder(client, S3Location("era", "pl"))
    with Stubber(client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [_object("pl/202409/a.nc", 10, "e1"), _object("pl/202409/", 0, "d")],
                "IsTruncated": True,
                "NextContinuationToken": "t",
                "KeyCount": 2,
            },
            {"Bucket": "era", "Prefix": "pl/"},
        )
        stub.add_response(
            "list_objects_v2",
            {"Contents": [_object("pl/b.nc", 20, "e2-3")], "IsTruncated": False, "KeyCount": 1},
            {"Bucket": "era", "Prefix": "pl/", "ContinuationToken": "t"},
        )
        rows = folder.entries()
    by_path = {row.path: row for row in rows}
    assert set(by_path) == {"202409", "202409/a.nc", "b.nc"}
    assert by_path["202409"].kind == "directory"
    assert by_path["202409/a.nc"].revision == "e1" and by_path["b.nc"].size == 20
    assert by_path["202409/a.nc"].locator == "pl/202409/a.nc"


def test_browsing_one_folder_uses_the_delimiter() -> None:
    client = _client()
    folder = S3Folder(client, S3Location("era", "pl"))
    with Stubber(client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "CommonPrefixes": [{"Prefix": "pl/202409/x/"}],
                "Contents": [_object("pl/202409/a.nc", 10, "e1")],
                "IsTruncated": False,
            },
            {"Bucket": "era", "Prefix": "pl/202409/", "Delimiter": "/"},
        )
        rows = folder.entries("202409", recursive=False)
    assert {(r.path, r.kind) for r in rows} == {("202409/x", "directory"), ("202409/a.nc", "file")}


def test_a_read_is_a_range_checked_against_the_listed_etag() -> None:
    client = _client()
    reader = S3RangeReader(client, "era", "pl/a.nc", 100, '"e1"')
    with Stubber(client) as stub:
        stub.add_response(
            "get_object",
            {"Body": io.BytesIO(b"\x89HDF"), "ContentRange": "bytes 10-13/100"},
            {"Bucket": "era", "Key": "pl/a.nc", "Range": "bytes=10-13", "IfMatch": '"e1"'},
        )
        stub.add_client_error(
            "get_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        reader.seek(10)
        assert reader.read(4) == b"\x89HDF"
        assert reader.tell() == 14 and reader.bytes_read == 4
        with pytest.raises(ValueError, match=SOURCE_REVISION_CHANGED):
            reader.read(4)


def test_reads_stay_inside_the_linked_prefix() -> None:
    folder = S3Folder(_client(), S3Location("era", "pl"))
    with pytest.raises(PermissionError):
        folder.open_read(FileEntry(path="x.nc", kind="file", size=1, locator="other/x.nc"))


def test_s3_is_read_only_and_keys_never_serialize() -> None:
    capabilities = provider_capabilities("s3")
    assert capabilities.supported_modes == ["read_only"] and capabilities.link_folder
    body = CreateSource.model_validate(
        {
            "provider": "s3",
            "root": "bucket/prefix",
            "label": "Private bucket",
            "configuration": {"s3_access": "keys", "s3_region": "us-east-1"},
            "s3_credentials": {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3cr3t"},
        }
    )
    dumped = body.model_dump_json()
    assert "s3cr3t" not in dumped and "AKIAEXAMPLE" not in dumped
    assert "s3cr3t" not in repr(body)
    with pytest.raises(ValueError):
        SourceConfiguration(s3_endpoint_url="https://user:pw@minio.example")


def test_keys_are_stored_host_side_bound_to_their_source(tmp_path: Path) -> None:
    from clio_agent.gact.storage.service import StorageService

    service = StorageService(tmp_path / "sources", tmp_path / "auth" / "credentials.json")
    body = CreateSource.model_validate(
        {
            "provider": "s3",
            "root": "s3://bucket/prefix",
            "label": "Private bucket",
            "configuration": {"s3_access": "keys", "s3_region": "us-east-1"},
            "s3_credentials": {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3cr3t"},
        }
    )
    checked: list[str] = []
    service._check_s3 = lambda record: checked.append(record.source.root)  # type: ignore[method-assign]
    record = service.create("ws", body)
    assert checked == ["bucket/prefix"] and record.source.mode == "read_only"
    assert service.auth.connected(record)
    assert s3_keys.keys(service.auth, record) == ("AKIAEXAMPLE", "s3cr3t")
    assert "s3cr3t" not in record.model_dump_json()
    service.auth.disconnect(record.source.id)
    with pytest.raises(PermissionError):
        s3_keys.keys(service.auth, record)
    with pytest.raises(ValueError, match="access key"):
        service.create(
            "ws",
            CreateSource.model_validate(
                {**body.model_dump(), "s3_credentials": None, "root": "bucket/prefix"}
            ),
        )
