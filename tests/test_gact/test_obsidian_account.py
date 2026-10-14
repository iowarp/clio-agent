"""Managed client boundaries and vault creation without an installed Obsidian app."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.routes.storage_obsidian import ObsidianSignIn
from clio_agent.gact.storage.knowledge_service import KnowledgeService
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.obsidian_account import ObsidianAccount
from clio_agent.gact.storage.service import StorageService
from clio_agent.runtime import obsidian_client_runtime as runtime


def test_create_empty_vault_then_write_and_index_notes_without_any_client(tmp_path: Path) -> None:
    """A new vault is real editable storage; no application or account is a prerequisite."""
    storage = StorageService(tmp_path / "sources", tmp_path / "auth" / "credentials.json")
    record = storage.create(
        "w",
        CreateSource(
            provider="obsidian", create_vault=True, label="New knowledge", mode="write_enabled"
        ),
    )
    root = Path(record.source.root)
    assert root.is_relative_to(tmp_path / "knowledge-vaults")
    assert (root / ".obsidian" / "app.json").read_text() == "{}\n"
    knowledge = KnowledgeService(
        storage, ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    )
    assert knowledge.index.graph(record).notes == []
    knowledge.create("w", record.source.id, "Welcome.md", "# Welcome\n[[Plan]]")
    knowledge.create("w", record.source.id, "Plan.md", "# Plan")
    snapshot = knowledge.index.graph(storage.get("w", record.source.id))
    assert {note.path for note in snapshot.notes} == {"Welcome.md", "Plan.md"}
    assert snapshot.links[0].target == "Plan.md"
    assert (root / "Welcome.md").read_text() == "# Welcome\n[[Plan]]"
    with pytest.raises(FileExistsError):
        knowledge.create("w", record.source.id, "Welcome.md", "overwrite")
    with pytest.raises((PermissionError, ValueError)):
        knowledge.create("w", record.source.id, ".obsidian/private.md", "private")
    with pytest.raises((PermissionError, ValueError)):
        knowledge.create("w", record.source.id, "../outside.md", "escape")


@pytest.mark.parametrize(
    "input",
    [
        {"provider": "local", "create_vault": True},
        {"provider": "obsidian", "create_vault": True, "root": "existing"},
        {
            "provider": "obsidian",
            "create_vault": True,
            "configuration": {"obsidian_connection": "headless_sync"},
        },
        {"provider": "obsidian"},
    ],
)
def test_creation_cannot_overwrite_or_implicitly_select_existing_folders(
    input: dict[str, object],
) -> None:
    """Creation and attachment are separate validated operations."""
    with pytest.raises(ValidationError):
        CreateSource.model_validate({"label": "Notes", **input})


def test_http_creation_and_plain_folder_attachment_do_not_require_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production routes create a vault and index an untouched ordinary notes folder."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "A.md").write_text("# A\n[[B]]", encoding="utf-8")
    (plain / "B.md").write_text("# B", encoding="utf-8")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in plain.iterdir()}
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    app.state.resource_store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    register_connected_storage_routes(app)
    monkeypatch.setattr(
        app.state.connected_storage.obsidian_account.client,
        "install",
        Mock(side_effect=AssertionError("local vaults need no client")),
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces/w/sources",
            json={"provider": "obsidian", "label": "Empty", "create_vault": True},
        )
        assert created.status_code == 201, created.text
        assert (
            client.get(f"/v1/workspaces/w/sources/{created.json()['id']}/knowledge/graph").json()[
                "notes"
            ]
            == []
        )
        attached = client.post(
            "/v1/workspaces/w/sources",
            json={"provider": "obsidian", "label": "Existing", "root": str(plain)},
        )
        assert attached.status_code == 201, attached.text
        snapshot = client.get(
            f"/v1/workspaces/w/sources/{attached.json()['id']}/knowledge/graph"
        ).json()
        assert len(snapshot["notes"]) == 2 and snapshot["links"][0]["target"] == "B.md"
        assert (
            client.post(
                f"/v1/workspaces/w/sources/{attached.json()['id']}/knowledge/open",
                json={"path": "A.md"},
            ).status_code
            == 200
        )
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in plain.iterdir()}


def test_account_home_and_ambient_token_are_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A personal client account can never be inherited by a CLIO-managed operation."""
    monkeypatch.setenv("OBSIDIAN_AUTH_TOKEN", "personal-secret")
    account = ObsidianAccount(tmp_path / "private", tmp_path / "vaults")
    env = account.environment()
    assert "OBSIDIAN_AUTH_TOKEN" not in env
    assert Path(env["HOME"]).is_relative_to(account.private_root)
    assert Path(env["USERPROFILE"]).is_relative_to(account.private_root)
    assert Path(env["XDG_CONFIG_HOME"]).is_relative_to(account.private_root)


def test_login_and_vault_listing_never_return_client_secrets(tmp_path: Path) -> None:
    """Only the official client sees passwords; vault JSON is projected to ID and name."""
    account = ObsidianAccount(tmp_path / "private", tmp_path / "vaults")
    account.token_path.parent.mkdir(parents=True)
    account.token_path.write_text("private-token")
    account.client = Mock()
    account.client.installed.return_value = True
    account.client.run.return_value = subprocess.CompletedProcess([], 0, "Logged in", "")
    status = account.login("person@example.com", "private-password", "private-mfa")
    assert status["authenticated"] and "private-" not in json.dumps(status)
    assert account.client.run.call_args.kwargs["secrets"] == {
        "email": "person@example.com",
        "password": "private-password",
        "mfa": "private-mfa",
    }
    account.client.run.return_value = subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "vaults": [
                    {
                        "id": "v",
                        "name": "Notes",
                        "password": "server-secret",
                        "encryptionKey": "secret-key",
                    }
                ],
                "shared": [],
            }
        ),
        "",
    )
    assert account.vaults() == [{"id": "v", "name": "Notes"}]
    account.client.run.return_value = subprocess.CompletedProcess(
        [], 1, "private-password", "2FA code is incorrect private-token"
    )
    with pytest.raises(runtime.ObsidianClientError, match="two-factor") as failure:
        account.login("person@example.com", "private-password")
    assert "private-" not in str(failure.value)
    assert "private-password" not in repr(
        ObsidianSignIn(email="a@b.co", password="private-password", mfa="secret-code")
    )


def test_remote_setup_uses_owned_copy_and_private_password(tmp_path: Path) -> None:
    """A remote identity cannot escape managed storage, even if it resembles a path."""
    account = ObsidianAccount(tmp_path / "private", tmp_path / "vaults")
    account.client = Mock()
    account.client.run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
    first = account.setup("../../remote-vault", "private-encryption")
    assert Path(first["root"]).is_relative_to(account.vaults_root)
    assert (Path(first["root"]) / ".obsidian").is_dir()
    assert first == account.setup("../../remote-vault", "private-encryption")
    args, kwargs = account.client.run.call_args
    assert "private-encryption" not in str(args)
    assert kwargs["secrets"] == {"password": "private-encryption"}


def test_managed_run_passes_secrets_only_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No password or MFA value appears in the OS-visible command line."""
    client = runtime.ManagedObsidianClient(tmp_path)
    monkeypatch.setattr(client, "command", lambda: ["managed-node", "managed-client"])
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr(runtime.subprocess, "run", runner)
    client.run(
        ["login"], environment={}, secrets={"password": "--secret-password", "mfa": "123456"}
    )
    assert "--secret-password" not in str(runner.call_args.args)
    assert "123456" not in str(runner.call_args.args)
    assert (
        json.loads(runner.call_args.kwargs["input"])["secrets"]["password"] == "--secret-password"
    )


def test_installation_is_single_flight_and_failure_can_be_retried(tmp_path: Path) -> None:
    """Repeated polls/clicks never start duplicate installers; failures remain visible."""
    account = ObsidianAccount(tmp_path / "private", tmp_path / "vaults")
    entered, released, finished = threading.Event(), threading.Event(), threading.Event()
    account.client = Mock()
    account.client.installed.return_value = False

    def install(progress: object) -> None:
        entered.set()
        assert released.wait(10)
        finished.set()
        raise RuntimeError("private installer output")

    account.client.install.side_effect = install
    assert account.install()["state"] == "installing"
    assert entered.wait(10)
    assert account.install()["state"] == "installing"
    assert account.client.install.call_count == 1
    released.set()
    assert finished.wait(10)
    assert account._installer is not None
    account._installer.join(timeout=10)
    assert account.status()["state"] == "failed"
    assert "private installer output" not in str(account.status())
    account.client.install.side_effect = lambda progress: None
    account.install()
    assert account._installer is not None
    account._installer.join(timeout=10)
    assert account.client.install.call_count == 2
