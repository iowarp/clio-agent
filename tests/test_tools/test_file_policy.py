"""Tests for file access policy validation."""

import pytest

from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError


def test_validate_read_allows_file_under_allowed_root(tmp_path):
    data_file = tmp_path / "data.h5"
    data_file.write_bytes(b"content")
    policy = FileAccessPolicy(allowed_roots=(tmp_path,))

    result = policy.validate_read(str(data_file))

    assert result == data_file.resolve()


def test_validate_read_rejects_outside_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.h5"
    outside.write_bytes(b"content")
    policy = FileAccessPolicy(allowed_roots=(allowed.resolve(),))

    try:
        policy.validate_read(str(outside))
    except FilePolicyError as exc:
        result = exc.to_result()
    else:
        raise AssertionError("Expected FilePolicyError")

    assert result["error"]["type"] == "file_policy"
    assert result["error"]["code"] == "outside_allowed_roots"
    assert result["error"]["field"] == "filepath"


def test_validate_read_rejects_large_file(tmp_path):
    data_file = tmp_path / "large.parquet"
    data_file.write_bytes(b"0123456789")
    policy = FileAccessPolicy(allowed_roots=(tmp_path,), max_file_size_bytes=4)

    try:
        policy.validate_read(str(data_file))
    except FilePolicyError as exc:
        result = exc.to_result()
    else:
        raise AssertionError("Expected FilePolicyError")

    assert result["error"]["code"] == "file_too_large"
    assert result["error"]["details"]["size_bytes"] == 10


def test_validate_read_rejects_symlink_by_default(tmp_path):
    real_file = tmp_path / "real.h5"
    real_file.write_bytes(b"content")
    link = tmp_path / "link.h5"
    try:
        link.symlink_to(real_file)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is not available")
        raise
    policy = FileAccessPolicy(allowed_roots=(tmp_path,))

    try:
        policy.validate_read(str(link))
    except FilePolicyError as exc:
        result = exc.to_result()
    else:
        raise AssertionError("Expected FilePolicyError")

    assert result["error"]["code"] == "symlink_denied"


def test_policy_from_mapping_reports_effective_settings(tmp_path):
    policy = FileAccessPolicy.from_mapping(
        {
            "CLIO_ALLOWED_ROOTS": str(tmp_path),
            "CLIO_MAX_FILE_SIZE_BYTES": "4096",
            "CLIO_ALLOW_SYMLINKS": "true",
        }
    )

    result = policy.to_dict()

    assert result["allowed_roots"] == [str(tmp_path.resolve())]
    assert result["max_file_size_bytes"] == 4096
    assert result["allow_symlinks"] is True
    assert "read_mode" in result
    assert "write_mode" in result
