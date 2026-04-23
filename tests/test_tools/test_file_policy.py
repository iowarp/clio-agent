"""Tests for file access policy validation."""

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
    link.symlink_to(real_file)
    policy = FileAccessPolicy(allowed_roots=(tmp_path,))

    try:
        policy.validate_read(str(link))
    except FilePolicyError as exc:
        result = exc.to_result()
    else:
        raise AssertionError("Expected FilePolicyError")

    assert result["error"]["code"] == "symlink_denied"
