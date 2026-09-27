"""Tests for ``scripts/upload_release_assets.sh`` (draft-release uploader).

Runs the real script under bash with a stub ``gh`` first on ``PATH`` that
records its argv and fails a configurable number of times -- the only fake is
the GitHub CLI transport.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "upload_release_assets.sh"

_STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_STUB_LOG"
count=$(cat "$GH_STUB_COUNT" 2>/dev/null || echo 0)
count=$((count + 1))
printf '%s' "$count" > "$GH_STUB_COUNT"
if [ "$count" -le "${GH_STUB_FAILS:-0}" ]; then
  echo "HTTP 502: upstream hiccup" >&2
  exit 1
fi
exit 0
"""


def _bash() -> str:
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to test the release upload script"
    return bash


def _run(
    tmp_path: Path, *args: str, fails: int = 0, max_attempts: int = 3
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    gh = stub_dir / "gh"
    gh.write_bytes(_STUB_GH.encode("utf-8"))
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    env = {
        **os.environ,
        "PATH": f"{stub_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_STUB_LOG": log.as_posix(),
        "GH_STUB_COUNT": (tmp_path / "gh.count").as_posix(),
        "GH_STUB_FAILS": str(fails),
        "GITHUB_REPOSITORY": "iowarp/clio-agent",
        "UPLOAD_MAX_ATTEMPTS": str(max_attempts),
        "UPLOAD_RETRY_BASE_S": "0",
    }
    result = subprocess.run(
        [_bash(), SCRIPT.as_posix(), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def _files(tmp_path: Path, *names: str) -> list[str]:
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")
    return list(names)


def test_uploads_every_file_to_the_tag_with_clobber(tmp_path: Path) -> None:
    files = _files(tmp_path, "a.zip", "SHA256SUMS.web.txt")

    result, calls = _run(tmp_path, "v0.9.4.20", *files)

    assert result.returncode == 0, result.stderr
    assert calls == [
        "release upload v0.9.4.20 a.zip SHA256SUMS.web.txt --clobber --repo iowarp/clio-agent"
    ]


def test_missing_file_fails_before_any_upload(tmp_path: Path) -> None:
    """An unmatched glob stays literal in bash; it must fail loud, not upload a partial set."""
    files = _files(tmp_path, "a.zip")

    result, calls = _run(tmp_path, "v0.9.4.20", *files, "clio-web-*.zip")

    assert result.returncode == 1
    assert "clio-web-*.zip' does not exist" in result.stderr
    assert calls == []


def test_transient_failure_is_retried_with_a_visible_warning(tmp_path: Path) -> None:
    files = _files(tmp_path, "a.zip")

    result, calls = _run(tmp_path, "v0.9.4.20", *files, fails=2)

    assert result.returncode == 0, result.stderr
    assert len(calls) == 3
    assert result.stdout.count("::warning::release upload to v0.9.4.20 failed") == 2


def test_persistent_failure_exhausts_retries_and_fails(tmp_path: Path) -> None:
    files = _files(tmp_path, "a.zip")

    result, calls = _run(tmp_path, "v0.9.4.20", *files, fails=99, max_attempts=2)

    assert result.returncode == 1
    assert len(calls) == 2
    assert "failed after 2 attempts" in result.stderr


def test_usage_error_without_files(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "v0.9.4.20")

    assert result.returncode == 2
    assert calls == []


def test_requires_github_repository(tmp_path: Path) -> None:
    files = _files(tmp_path, "a.zip")
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_REPOSITORY"}
    result = subprocess.run(
        [_bash(), SCRIPT.as_posix(), "v0.9.4.20", *files],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "GITHUB_REPOSITORY must be set" in result.stderr
