"""Unit + route tests for the read-only marketplace update check (slice A4).

Exercises :mod:`clio_agent.gact.blueprint_update_check` against REAL local git
repos -- a working checkout plus a genuine bare clone used as the "remote" --
so ``git ls-remote`` actually runs, per house rule (live execution over
mocked plumbing). The genuinely-unreachable-without-mocking outcomes (``git``
missing, a bare-Popen-level hang) monkeypatch ``subprocess.Popen``; the
process-tree-kill guarantee itself (#A5 review) is instead proven against a
REAL spawned grandchild process, never a mock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprint_sources import upsert_agent_blueprint_source
from clio_agent.gact.app import build_app
from clio_agent.gact.blueprint_update_check import (
    check_source_update,
    remote_head_commit,
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "clio-test",
    "GIT_AUTHOR_EMAIL": "clio-test@example.com",
    "GIT_COMMITTER_NAME": "clio-test",
    "GIT_COMMITTER_EMAIL": "clio-test@example.com",
}


def _init_work_repo(work: Path, *, content: str = "v1") -> str:
    """Create a real, committed git checkout at ``work``; return its HEAD sha."""

    work.mkdir(parents=True, exist_ok=True)
    (work / "AGENT.md").write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main", str(work)], env=_GIT_ENV, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "add", "-A"], env=_GIT_ENV, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "initial"],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()


def _make_bare_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a work checkout plus a real bare clone of it (the "remote").

    Returns ``(work, bare, initial_commit)``.
    """

    work = tmp_path / "work"
    commit1 = _init_work_repo(work)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    return work, bare, commit1


def _advance_remote(work: Path, bare: Path) -> str:
    """Commit a second change in ``work`` and push it to the bare ``remote``."""

    (work / "AGENT.md").write_text("v2", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work), "add", "-A"], env=_GIT_ENV, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "second"],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", str(bare), "main"],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()


def _remote_source(bare: Path) -> str:
    """A ``file://`` URI for ``bare`` -- forces the ls-remote branch.

    A plain filesystem path to the bare repo would satisfy ``Path.exists()``
    and be probed via the local-rev-parse branch instead; the URI form is not
    a literal path, so :func:`remote_head_commit` takes the ls-remote branch
    while still pointing at a real, on-disk bare repo.
    """

    return bare.as_uri()


def test_update_available_when_remote_head_differs(tmp_path: Path) -> None:
    work, bare, commit1 = _make_bare_remote(tmp_path)
    commit2 = _advance_remote(work, bare)

    row = {"id": "src_a", "source": _remote_source(bare), "ref": "main", "commit": commit1}
    status = check_source_update(row, timeout_s=10.0)

    assert status.reason == "update_available"
    assert status.update_available is True
    assert status.installed_commit == commit1
    assert status.remote_commit == commit2


def test_up_to_date_when_commits_match(tmp_path: Path) -> None:
    _work, bare, commit1 = _make_bare_remote(tmp_path)

    row = {"id": "src_a", "source": _remote_source(bare), "ref": "main", "commit": commit1}
    status = check_source_update(row, timeout_s=10.0)

    assert status.reason == "up_to_date"
    assert status.update_available is False
    assert status.remote_commit == commit1


def test_ls_remote_failure_is_typed_not_blank(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.git"
    commit, reason, detail = remote_head_commit(missing.as_uri(), "main", timeout_s=10.0)

    assert commit == ""
    assert reason == "ls_remote_failed"
    assert detail != ""


def test_ref_not_found_reason(tmp_path: Path) -> None:
    _work, bare, _commit1 = _make_bare_remote(tmp_path)

    commit, reason, detail = remote_head_commit(
        _remote_source(bare), "no-such-branch", timeout_s=10.0
    )

    assert commit == ""
    assert reason == "ref_not_found"
    assert detail != ""


def test_timeout_reason(monkeypatch) -> None:
    """``remote_head_commit`` probes with ``subprocess.Popen`` + ``communicate(timeout=)``
    (not ``subprocess.run``, since a plain ``run(..., timeout=)`` only signals the immediate
    PID on Windows and can still block on a hung transport grandchild -- see
    ``test_timeout_kills_full_process_tree_not_just_the_immediate_child`` below for the live,
    real-process version of this same guarantee)."""

    class _HangingPopen:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            # A syntactically valid but never-real PID (real Popen PIDs are always
            # positive) -- proves the kill-tree path degrades to a no-op via
            # psutil.NoSuchProcess rather than raising out of remote_head_commit.
            self.pid = 999_999_999

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=timeout or 0.0)

    monkeypatch.setattr(subprocess, "Popen", _HangingPopen)

    commit, reason, detail = remote_head_commit(
        "https://example.invalid/repo.git", "main", timeout_s=0.01
    )

    assert commit == ""
    assert reason == "timeout"
    assert detail != ""


def test_git_unavailable_reason(monkeypatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> subprocess.Popen[str]:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "Popen", _boom)

    commit, reason, detail = remote_head_commit(
        "https://example.invalid/repo.git", "main", timeout_s=10.0
    )

    assert commit == ""
    assert reason == "git_unavailable"
    assert detail != ""


def test_timeout_kills_full_process_tree_not_just_the_immediate_child(
    tmp_path: Path, monkeypatch
) -> None:
    """A hung transport GRANDCHILD that inherited the probe's stdout/stderr pipes must be
    killed too -- killing only the immediate ``git.exe``-equivalent PID leaves those pipes
    open and ``communicate()`` blocks well past the caller's timeout waiting for EOF that
    never comes (proven live: a 2s timeout took ~25s to actually return before this fix).

    Replaces the resolved ``git`` command with a REAL script that spawns a genuine, long-lived
    grandchild inheriting its own stdout/stderr handles -- the exact ``git-remote-https.exe`` /
    ``ssh.exe`` inheritance shape -- so this exercises real OS process management, not a mock.
    """

    marker = tmp_path / "grandchild_started.marker"
    script = tmp_path / "fake_git_hangs.py"
    script.write_text(
        "import subprocess, sys\n"
        f"open({str(marker)!r}, 'w').close()\n"
        "grandchild = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdout=sys.stdout,\n"
        "    stderr=sys.stderr,\n"
        ")\n"
        "grandchild.wait()\n",
        encoding="utf-8",
    )

    real_popen = subprocess.Popen

    def _fake_popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        assert command[0] == "git"
        return real_popen([sys.executable, str(script)], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    started = time.monotonic()
    commit, reason, detail = remote_head_commit(
        "https://example.invalid/repo.git", "main", timeout_s=1.0
    )
    elapsed = time.monotonic() - started

    assert commit == ""
    assert reason == "timeout"
    assert detail != ""
    assert marker.exists(), "the fake git script never actually started"
    # The whole point of the fix: return close to the 1s timeout, never blocked for the
    # grandchild's full (60s) lifetime.
    assert elapsed < 20.0, f"took {elapsed:.1f}s -- the grandchild was never actually killed"


def test_path_source_uses_local_head(tmp_path: Path) -> None:
    work = tmp_path / "work"
    commit1 = _init_work_repo(work)

    commit, reason, detail = remote_head_commit(str(work), "", timeout_s=10.0)
    assert commit == commit1
    assert reason == "up_to_date"
    assert detail == ""

    row = {"id": "src_local", "source": str(work), "ref": "", "commit": commit1}
    status = check_source_update(row, timeout_s=10.0)
    assert status.reason == "up_to_date"
    assert status.update_available is False
    assert status.remote_commit == commit1


def test_installed_commit_unknown_reason(tmp_path: Path) -> None:
    _work, bare, commit1 = _make_bare_remote(tmp_path)

    row = {"id": "src_a", "source": _remote_source(bare), "ref": "main"}
    status = check_source_update(row, timeout_s=10.0)

    assert status.reason == "installed_commit_unknown"
    assert status.update_available is None
    assert status.remote_commit == commit1
    assert status.installed_commit == ""


def test_annotated_tag_resolves_to_peeled_commit_not_tag_object(tmp_path: Path) -> None:
    """An ANNOTATED tag's ``refs/tags/<ref>`` line is the TAG OBJECT's sha, not the commit
    it points at; a naive resolution would compare that object sha against the installed
    COMMIT sha and report a permanent false ``update_available`` even when fully up to date.
    ``remote_head_commit`` must resolve the PEELED (``^{}``) commit sha instead (#A5 review)."""

    work = tmp_path / "work"
    commit = _init_work_repo(work)
    subprocess.run(
        ["git", "-C", str(work), "tag", "-a", "v1.0.0", "-m", "release"],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    tag_object_sha = subprocess.check_output(
        ["git", "-C", str(work), "rev-parse", "v1.0.0"], text=True
    ).strip()
    assert tag_object_sha != commit, "an annotated tag's own object sha must differ from HEAD"

    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )

    resolved, reason, detail = remote_head_commit(_remote_source(bare), "v1.0.0", timeout_s=10.0)
    assert resolved == commit
    assert reason == "up_to_date"
    assert detail == ""

    row = {"id": "src_tag", "source": _remote_source(bare), "ref": "v1.0.0", "commit": commit}
    status = check_source_update(row, timeout_s=10.0)
    assert status.reason == "up_to_date"
    assert status.update_available is False
    assert status.remote_commit == commit


def test_lightweight_tag_still_resolves_without_a_peeled_line(tmp_path: Path) -> None:
    """A LIGHTWEIGHT tag has no ``^{}`` peeled line at all -- the plain ``refs/tags/<ref>``
    entry (already the commit sha there) must remain the fallback."""

    work = tmp_path / "work"
    commit = _init_work_repo(work)
    subprocess.run(
        ["git", "-C", str(work), "tag", "lw1.0"], env=_GIT_ENV, check=True, capture_output=True
    )
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )

    resolved, reason, _detail = remote_head_commit(_remote_source(bare), "lw1.0", timeout_s=10.0)
    assert resolved == commit
    assert reason == "up_to_date"


def test_abbreviated_installed_commit_matches_full_remote_sha(tmp_path: Path) -> None:
    """A short/abbreviated ``installed_commit`` (7-39 hex chars) compares as a PREFIX of the
    full remote sha, not a strict equality -- a human-edited or older-recorded pin must never
    report a permanent false ``update_available`` (#A5 review)."""

    _work, bare, commit1 = _make_bare_remote(tmp_path)

    row = {"id": "src_a", "source": _remote_source(bare), "ref": "main", "commit": commit1[:10]}
    status = check_source_update(row, timeout_s=10.0)

    assert status.reason == "up_to_date"
    assert status.update_available is False
    assert status.remote_commit == commit1


def test_updates_route_lists_every_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user-config"))
    _work, bare, commit1 = _make_bare_remote(tmp_path)
    upsert_agent_blueprint_source(
        {
            "id": "src_a",
            "name": "A",
            "source": _remote_source(bare),
            "ref": "main",
            "commit": commit1,
        }
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        resp = client.get("/v1/agent-blueprints/sources/updates")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "checked_at" in body
    assert [row["source_id"] for row in body["sources"]] == ["src_a"]
    assert body["sources"][0]["reason"] == "up_to_date"
    assert body["sources"][0]["update_available"] is False

    with TestClient(app) as client:
        one = client.get("/v1/agent-blueprints/sources/src_a/updates")
    assert one.status_code == 200, one.text
    assert one.json()["source"]["source_id"] == "src_a"


def test_updates_route_404_unknown_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user-config"))
    app = build_app(sessions_path=tmp_path / "sessions.json")

    with TestClient(app) as client:
        resp = client.get("/v1/agent-blueprints/sources/does-not-exist/updates")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["error"] == "not_found"


def test_updates_route_precedes_blueprint_catch_all(tmp_path: Path, monkeypatch) -> None:
    """With zero registered sources this must 200 with an empty list -- NOT
    fall through to the ``GET /v1/agent-blueprints/{blueprint_id:path}``
    catch-all, which would 404 treating ``sources/updates`` as a blueprint id.
    """

    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user-config"))
    app = build_app(sessions_path=tmp_path / "sessions.json")

    with TestClient(app) as client:
        resp = client.get("/v1/agent-blueprints/sources/updates")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sources"] == []
    assert "checked_at" in body
