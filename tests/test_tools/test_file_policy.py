"""Tests for file access policy validation."""

from clio_agent import conf
from clio_agent.tools import file_policy
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError


def test_validate_read_allows_file_under_allowed_root(tmp_path):
    data_file = tmp_path / "data.h5"
    data_file.write_bytes(b"content")
    policy = FileAccessPolicy(allowed_roots=(tmp_path,))

    result = policy.validate_read(str(data_file))

    assert result == data_file.resolve()


def test_validate_read_defaults_relative_paths_to_the_active_workspace_root(tmp_path, monkeypatch):
    """C3: a relative fs_read/fs_write/fs_edit path must resolve against the
    SESSION'S active workspace root (bound per tool call via
    ``clio_agent.tools.execution.tool_workspace_context``), not the OS process's
    own cwd — the same class of bug fixed for the shell tool's default cwd."""
    from clio_agent.tools.execution import tool_workspace_context

    workspace_root = tmp_path / "workspace"
    process_cwd = tmp_path / "install-dir"
    workspace_root.mkdir()
    process_cwd.mkdir()
    (workspace_root / "notes.md").write_text("from workspace", encoding="utf-8")
    (process_cwd / "notes.md").write_text("from process cwd", encoding="utf-8")
    monkeypatch.chdir(process_cwd)
    policy = FileAccessPolicy(allowed_roots=(workspace_root, process_cwd))

    with tool_workspace_context(str(workspace_root)):
        resolved = policy.validate_read("notes.md")

    assert resolved == (workspace_root / "notes.md").resolve()


def test_validate_write_defaults_relative_paths_to_the_active_workspace_root(tmp_path, monkeypatch):
    from clio_agent.tools.execution import tool_workspace_context

    workspace_root = tmp_path / "workspace"
    process_cwd = tmp_path / "install-dir"
    workspace_root.mkdir()
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    policy = FileAccessPolicy(allowed_roots=(workspace_root, process_cwd))

    with tool_workspace_context(str(workspace_root)):
        resolved = policy.validate_write("output.md")

    assert resolved == (workspace_root / "output.md").resolve()


def test_coerce_path_falls_back_to_process_cwd_with_a_typed_reason_when_unbound(
    tmp_path, monkeypatch
):
    """No silent fallback: a relative path with NO active workspace root bound
    still resolves (the app-less CLI grounding path legitimately has none), but
    the fallback to the OS process's own cwd is recorded, not silent."""
    monkeypatch.chdir(tmp_path)
    events: list[tuple[object, ...]] = []
    orig_event = file_policy.trace.event

    def _spy(tag, fmt, *args):
        events.append((tag, fmt, *args))
        orig_event(tag, fmt, *args)

    monkeypatch.setattr(file_policy.trace, "event", _spy)
    policy = FileAccessPolicy(allowed_roots=(tmp_path,))
    (tmp_path / "notes.md").write_text("from cwd", encoding="utf-8")

    resolved = policy.validate_read("notes.md")

    assert resolved == (tmp_path / "notes.md").resolve()
    assert any("reason=no_active_workspace_root" in fmt for _tag, fmt, *_rest in events), events


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


def test_validate_read_rejects_symlink_by_default(tmp_path, monkeypatch):
    real_file = tmp_path / "real.h5"
    real_file.write_bytes(b"content")
    link = tmp_path / "link.h5"
    try:
        link.symlink_to(real_file)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            monkeypatch.setattr(file_policy, "_has_symlink", lambda _path: True)
        else:
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


def test_policy_from_env_prefers_workspace_config(tmp_path, monkeypatch):
    config_root = tmp_path / "from-config"
    env_root = tmp_path / "from-env"
    config_root.mkdir()
    env_root.mkdir()
    config_dir = tmp_path / ".clio"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "\n".join(
            [
                "tools:",
                "  file_policy:",
                "    allowed_roots:",
                f"      - {config_root.as_posix()}",
                "    max_file_size_bytes: 10GB",
                "    allow_symlinks: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(env_root))
    monkeypatch.setenv("CLIO_MAX_FILE_SIZE_BYTES", "1024")
    monkeypatch.setenv("CLIO_ALLOW_SYMLINKS", "false")
    conf.reload()
    try:
        policy = FileAccessPolicy.from_env()
    finally:
        conf.reload()

    assert policy.allowed_roots == (config_root.resolve(),)
    assert policy.max_file_size_bytes == 10_000_000_000
    assert policy.allow_symlinks is True


def test_default_allowed_roots_use_platform_tempdir(monkeypatch):
    """Default roots must use tempfile.gettempdir(), not a POSIX /tmp literal (#765)."""
    import tempfile
    from pathlib import Path

    monkeypatch.delenv("CLIO_ALLOWED_ROOTS", raising=False)

    roots = file_policy._default_allowed_roots()

    assert Path(tempfile.gettempdir()) in roots
    for root in roots:
        assert root in (Path.cwd(), Path(tempfile.gettempdir()))
