"""Publishing the private marketplace to GitHub: one commit that mirrors this host.

A small in-memory GitHub serves the REST calls publication makes (repository, refs,
commits, trees, blobs, the contents call that starts an empty repository), so the
real request sequence, conflict checks and deletions run unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.gact import local_publish
from clio_agent.gact.local_publish import PublishError, parse_repository, publish, status
from clio_agent.plugins import authored
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore


def _sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


class FakeGitHub:
    """One repository's branch as git objects, behind the REST paths CLIO calls."""

    def __init__(self, *, private: bool = True, push: bool = True, files: Any = None) -> None:
        self.private, self.push = private, push
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.head: str | None = None
        self.messages: list[str] = []
        if files:
            self._commit({path: self._blob(data) for path, data in files.items()}, "seed")

    def _blob(self, data: bytes) -> str:
        sha = _sha(data)
        self.blobs[sha] = data
        return sha

    def _commit(self, tree: dict[str, str], message: str) -> str:
        tree_sha = hashlib.sha1(json.dumps(sorted(tree.items())).encode()).hexdigest()
        self.trees[tree_sha] = dict(tree)
        sha = hashlib.sha1(f"{tree_sha}{self.head}{message}".encode()).hexdigest()
        self.commits[sha] = {"tree": tree_sha, "parent": self.head}
        self.messages.append(message)
        self.head = sha
        return sha

    def files(self) -> dict[str, bytes]:
        if self.head is None:
            return {}
        tree = self.trees[self.commits[self.head]["tree"]]
        return {path: self.blobs[sha] for path, sha in tree.items()}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/repos/lab/backup")
        body = json.loads(request.content) if request.content else {}
        if request.url.path.startswith("/repos/") and not request.url.path.startswith(
            "/repos/lab/backup"
        ):
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "default_branch": "main",
                    "private": self.private,
                    "permissions": {"push": self.push},
                },
            )
        if path.startswith("/contents/") and request.method == "PUT":
            data = base64.b64decode(body["content"])
            self._commit({path.removeprefix("/contents/"): self._blob(data)}, body["message"])
            return httpx.Response(201, json={})
        if path == "/git/ref/heads/main":
            if self.head is None:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if path.startswith("/git/commits/") and request.method == "GET":
            return httpx.Response(200, json={"tree": {"sha": self.commits[path[13:]]["tree"]}})
        if path.startswith("/git/trees/") and request.method == "GET":
            tree = self.trees[path[11:]]
            rows = [
                {"path": p, "sha": s, "type": "blob", "mode": "100644"} for p, s in tree.items()
            ]
            return httpx.Response(200, json={"tree": rows, "truncated": False})
        if path == "/git/blobs":
            return httpx.Response(201, json={"sha": self._blob(base64.b64decode(body["content"]))})
        if path == "/git/trees":
            tree = dict(self.trees[body["base_tree"]])
            for row in body["tree"]:
                if row["sha"] is None:
                    tree.pop(row["path"], None)
                else:
                    tree[row["path"]] = row["sha"]
            sha = hashlib.sha1(json.dumps(sorted(tree.items())).encode()).hexdigest()
            self.trees[sha] = tree
            return httpx.Response(201, json={"sha": sha})
        if path == "/git/commits":
            assert body["parents"] == [self.head]
            sha = hashlib.sha1(f"{body['tree']}{self.head}".encode()).hexdigest()
            self.commits[sha] = {"tree": body["tree"], "parent": self.head}
            self.messages.append(body["message"])
            self.pending = sha
            return httpx.Response(201, json={"sha": sha})
        if path == "/git/refs/heads/main" and request.method == "PATCH":
            assert body == {"sha": self.pending, "force": False}
            self.head = self.pending
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected GitHub call {request.method} {request.url}")


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    real = httpx.Client

    def client(*args: Any, **kwargs: Any) -> httpx.Client:
        return real(*args, transport=httpx.MockTransport(fake.handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return fake


def _service(tmp_path: Path) -> PluginService:
    return PluginService(PluginStore(tmp_path / "store"))


def _plugin(service: PluginService, name: str, body: str = "Run id, then sample.") -> None:
    manifest = {"name": name, "version": "0.1.0", "description": f"{name}."}
    skill = f"---\nname: {name}\ndescription: Write up a run.\n---\n\n{body}\n"
    authored.write(
        service.store.root,
        name,
        {"plugin.json": json.dumps(manifest), f"skills/{name}/SKILL.md": skill},
        "sess_test",
    )


@pytest.mark.parametrize(
    "text",
    [
        "lab/backup",
        "https://github.com/lab/backup",
        "https://github.com/lab/backup.git",
        "git@github.com:lab/backup.git",
    ],
)
def test_a_repository_can_be_named_the_ways_people_copy_it(text: str) -> None:
    assert parse_repository(text) == "lab/backup"


@pytest.mark.parametrize(
    "text", ["", "backup", "lab/backup/extra", "https://gitlab.com/lab/backup", "lab/.."]
)
def test_anything_else_is_refused_by_name(text: str) -> None:
    with pytest.raises(PublishError) as refused:
        parse_repository(text)
    assert refused.value.code == "repository_invalid"


def test_the_first_publication_starts_an_empty_repository_and_mirrors_the_host(
    tmp_path: Path, github: FakeGitHub
) -> None:
    service = _service(tmp_path)
    _plugin(service, "run-notes")
    result = publish(service, "token", "lab/backup")
    files = github.files()
    assert set(files) == {
        "ai.iowarp.clio/marketplace.json",
        "plugins/run-notes/plugin.json",
        "plugins/run-notes/skills/run-notes/SKILL.md",
    }
    assert result["up_to_date"] is False and result["commit"] == github.head
    assert result["commit_url"] == f"https://github.com/lab/backup/commit/{github.head}"
    assert github.messages[0] == "Start the CLIO plugin backup"
    assert status(service)["changed_since"] is False


def test_publishing_again_with_no_change_makes_no_commit(
    tmp_path: Path, github: FakeGitHub
) -> None:
    service = _service(tmp_path)
    _plugin(service, "run-notes")
    first = publish(service, "token", "lab/backup")
    commits = len(github.commits)
    again = publish(service, "token", "lab/backup")
    assert again["up_to_date"] is True and len(github.commits) == commits
    assert again["commit"] == first["commit"]


def test_a_change_and_a_removal_reach_the_repository_and_its_own_files_stay(
    tmp_path: Path, github: FakeGitHub
) -> None:
    service = _service(tmp_path)
    _plugin(service, "run-notes")
    _plugin(service, "old-plugin")
    publish(service, "token", "lab/backup")
    head = github.head
    github._commit(
        {**github.trees[github.commits[head]["tree"]], "README.md": github._blob(b"# Lab")},
        "readme",
    )
    _plugin(service, "run-notes", body="Run id, sample, then the peak in kelvin.")
    authored.remove_tree(authored.plugin_folder(service.store.root, "old-plugin"))
    assert status(service)["changed_since"] is True
    result = publish(service, "token", "lab/backup")
    files = github.files()
    assert b"peak in kelvin" in files["plugins/run-notes/skills/run-notes/SKILL.md"]
    assert not [path for path in files if path.startswith("plugins/old-plugin/")]
    assert files["README.md"] == b"# Lab"
    assert result["removed"] == [
        "plugins/old-plugin/plugin.json",
        "plugins/old-plugin/skills/old-plugin/SKILL.md",
    ]
    assert result["written"] == ["plugins/run-notes/skills/run-notes/SKILL.md"]


def test_a_public_repository_needs_the_person_to_say_so(tmp_path: Path, github: FakeGitHub) -> None:
    service = _service(tmp_path)
    _plugin(service, "run-notes")
    github.private = False
    with pytest.raises(PublishError) as refused:
        publish(service, "token", "lab/backup")
    assert refused.value.code == "repository_public" and github.head is None
    assert publish(service, "token", "lab/backup", allow_public=True)["private"] is False


def test_a_repository_the_app_cannot_see_says_how_to_give_access(
    tmp_path: Path, github: FakeGitHub
) -> None:
    service = _service(tmp_path)
    _plugin(service, "run-notes")
    with pytest.raises(PublishError) as refused:
        publish(
            service,
            "token",
            "lab/other",
            account_url="https://github.com/apps/clio/installations/new",
        )
    assert refused.value.code == "repository_not_reachable"
    assert refused.value.details["account_url"].endswith("/installations/new")


def test_a_repository_without_write_access_is_refused(tmp_path: Path, github: FakeGitHub) -> None:
    service = _service(tmp_path)
    _plugin(service, "run-notes")
    github.push = False
    with pytest.raises(PublishError) as refused:
        publish(service, "token", "lab/backup")
    assert refused.value.code == "repository_not_writable"


def test_an_empty_collection_is_not_published(tmp_path: Path, github: FakeGitHub) -> None:
    with pytest.raises(PublishError) as refused:
        publish(_service(tmp_path), "token", "lab/backup")
    assert refused.value.code == "local_collection_empty" and github.head is None


def test_the_publication_record_lives_beside_the_collection_not_in_it(
    tmp_path: Path, github: FakeGitHub
) -> None:
    service = _service(tmp_path)
    _plugin(service, "run-notes")
    publish(service, "token", "lab/backup")
    record = service.store.root / local_publish.STATE_FILE
    assert json.loads(record.read_text())["repository"] == "lab/backup"
    assert not [path for path in github.files() if "local-publish" in path]
