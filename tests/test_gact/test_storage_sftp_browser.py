"""Real local SFTP qualification of the production fsspec key/password boundary."""

from __future__ import annotations

import asyncio
import io
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import paramiko
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from clio_agent.gact.error_middleware import install_typed_error_handlers
from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.routes.storage_sftp import InspectSftp, inspect_sftp
from clio_agent.gact.storage.auth import StorageAuth
from clio_agent.gact.storage.models import CreateSource, SftpCredentials, SourceConfiguration
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.sftp import SftpSource


@pytest.fixture
def sftp_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SshRoute, str, Path]]:
    """Serve a disposable SFTP directory using real password and public-key authentication."""
    host_key = paramiko.RSAKey.generate(2048)
    client_key = paramiko.RSAKey.generate(2048)
    key_text = io.StringIO()
    client_key.write_private_key(key_text)
    files = tmp_path / "files"
    files.mkdir()
    (files / "input").mkdir()
    (files / "input" / "sample.txt").write_bytes(b"real SFTP bytes\n")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "known_hosts").write_text(
        f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    stopping = threading.Event()
    transports: list[paramiko.Transport] = []

    class Login(paramiko.ServerInterface):
        def get_allowed_auths(self, username: str) -> str:
            return "password,publickey"

        def check_auth_password(self, username: str, password: str) -> int:
            return (
                paramiko.AUTH_SUCCESSFUL
                if username == "tester" and password == "test-password"
                else paramiko.AUTH_FAILED
            )

        def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
            return (
                paramiko.AUTH_SUCCESSFUL
                if username == "tester" and key == client_key
                else paramiko.AUTH_FAILED
            )

        def check_channel_request(self, kind: str, chanid: int) -> int:
            return (
                paramiko.OPEN_SUCCEEDED
                if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            )

    class Files(paramiko.SFTPServerInterface):
        def local(self, path: str) -> Path:
            local = (files / path.lstrip("/")).resolve()
            if not local.is_relative_to(files.resolve()):
                raise PermissionError(path)
            return local

        def list_folder(self, path: str) -> list[paramiko.SFTPAttributes]:
            rows = []
            for entry in self.local(path).iterdir():
                attr = paramiko.SFTPAttributes.from_stat(entry.stat())
                attr.filename = entry.name
                rows.append(attr)
            return rows

        def stat(self, path: str) -> paramiko.SFTPAttributes:
            return paramiko.SFTPAttributes.from_stat(self.local(path).stat())

        def lstat(self, path: str) -> paramiko.SFTPAttributes:
            return paramiko.SFTPAttributes.from_stat(self.local(path).lstat())

        def open(self, path: str, flags: int, attr: paramiko.SFTPAttributes) -> paramiko.SFTPHandle:
            handle = paramiko.SFTPHandle(flags)
            if flags & (os.O_WRONLY | os.O_RDWR):
                handle.writefile = os.fdopen(os.open(self.local(path), flags), "wb")
            else:
                handle.readfile = self.local(path).open("rb")
            return handle

        def posix_rename(self, oldpath: str, newpath: str) -> int:
            self.local(oldpath).replace(self.local(newpath))
            return paramiko.SFTP_OK

        def remove(self, path: str) -> int:
            self.local(path).unlink()
            return paramiko.SFTP_OK

    def serve() -> None:
        while not stopping.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(client)
            transports.append(transport)
            transport.add_server_key(host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
            try:
                transport.start_server(server=Login())
            except (OSError, paramiko.SSHException):
                transport.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield SshRoute(host="127.0.0.1", port=port, user="tester"), key_text.getvalue(), files
    finally:
        stopping.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(timeout=3)


@pytest.mark.parametrize("commit", [True, False])
def test_fsspec_sftp_write_and_deferred_commit(
    sftp_host: tuple[SshRoute, str, Path], commit: bool
) -> None:
    """Exercise upstream fsspec write/transaction behavior over a real SFTP session."""
    route, _, files = sftp_host
    source = SftpSource.from_route(
        route, "/input", credentials=SftpCredentials(password=SecretStr("test-password"))
    )
    try:
        fs = source.fs
        fs.temppath = "/input"
        with fs.open("/input/sample.txt", "wb") as stream:
            stream.write(b"immediate edit")
        assert (files / "input/sample.txt").read_bytes() == b"immediate edit"
        transaction = fs.start_transaction()
        with fs.open("/input/sample.txt", "wb") as stream:
            stream.write(b"staged edit")
        assert (files / "input/sample.txt").read_bytes() == b"immediate edit"
        transaction.complete(commit=commit)
        assert (files / "input/sample.txt").read_bytes() == (
            b"staged edit" if commit else b"immediate edit"
        )
        assert len(list((files / "input").iterdir())) == 1
    finally:
        source.close()


@pytest.mark.parametrize("method", ["password", "key"])
def test_real_sftp_browse_read_and_no_secret_serialization(
    sftp_host: tuple[SshRoute, str, Path], method: str, tmp_path: Path
) -> None:
    route, key, _ = sftp_host
    credentials = (
        SftpCredentials(password=SecretStr("test-password"))
        if method == "password"
        else SftpCredentials(private_key=SecretStr(key))
    )
    report = inspect_sftp(route, "/", credentials)
    assert report["entries"] == [{"name": "input", "path": "/input"}]
    source = SftpSource.from_route(route, "/input", credentials=credentials)
    try:
        entries = source.entries()
        assert len(entries) == 1
        with source.open_read(entries[0]) as reader:
            assert reader.read() == b"real SFTP bytes\n"
    finally:
        source.close()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = StorageService(tmp_path / "source-store", tmp_path / "auth" / "credentials.json")
    service.target_source = lambda record: SftpSource.from_route(
        route, record.source.root, credentials=service.auth.sftp_credentials(record)
    )
    record = service.create(
        "workspace",
        CreateSource(
            provider="sftp",
            root="/input",
            label="Imported SFTP",
            configuration=SourceConfiguration(
                target_id="test-host", ssh_origin="clio", ssh_authentication=method
            ),
            sftp_credentials=credentials,
        ),
        workspace,
    )
    operation = service.store.begin_operation(record.source.id, "materialize")
    asyncio.run(service._transfer(record, workspace, operation))
    materialized = service.get("workspace", record.source.id)
    assert Path(materialized.source.local_path, "sample.txt").read_bytes() == b"real SFTP bytes\n"
    request = InspectSftp(route=route, credentials=credentials)
    assert "test-password" not in repr(request) + request.model_dump_json()
    assert "PRIVATE KEY" not in repr(request) + request.model_dump_json()


def test_wrong_password_is_a_sanitized_failure(sftp_host: tuple[SshRoute, str, Path]) -> None:
    route, _, _ = sftp_host
    with pytest.raises(ValueError, match="SSH sign-in failed") as raised:
        inspect_sftp(route, "/", SftpCredentials(password=SecretStr("wrong-password")))
    assert "wrong-password" not in str(raised.value)


def test_source_credential_binding_restart_and_disconnect(tmp_path: Path) -> None:
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "credentials.json")
    service.target_source = lambda record: object()  # type: ignore[assignment,return-value]
    body = CreateSource(
        provider="sftp",
        label="SFTP inputs",
        root="/input",
        configuration=SourceConfiguration(
            target_id="host", ssh_origin="clio", ssh_authentication="password"
        ),
        sftp_credentials=SftpCredentials(password=SecretStr("test-password")),
    )
    source = service.create("workspace", body, tmp_path / "workspace")
    assert service.auth.connected(source)
    assert service.auth.sftp_credentials(source).password.get_secret_value() == "test-password"  # type: ignore[union-attr]
    assert "test-password" not in source.model_dump_json() + body.model_dump_json() + repr(body)
    other = source.model_copy(update={"principal": "someone-else"})
    assert not service.auth.connected(other)
    assert not StorageAuth(tmp_path / "private" / "credentials.json").connected(source)
    service.remove(source)
    assert not service.auth.connected(source)


def test_browser_http_sign_in_can_switch_credentials_after_disconnect(
    sftp_host: tuple[SshRoute, str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trusted HTTP path reconnects the same source with a newly selected key."""
    route, key, _ = sftp_host
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()
    install_typed_error_handlers(app)
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    target = SimpleNamespace(kind="ssh", ssh=route)
    app.state.infrastructure_store = SimpleNamespace(target=lambda identifier: target)
    register_connected_storage_routes(app)
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/workspaces/w/sources",
            json={"sftp_credentials": {"password": "test-password", "private_key": key}},
        )
        assert invalid.status_code == 422
        assert "test-password" not in invalid.text
        assert "PRIVATE KEY" not in invalid.text
        inspected = client.post(
            "/v1/storage/ssh/inspect",
            json={"target_id": "test-host", "credentials": {"password": "test-password"}},
        )
        assert inspected.status_code == 200, inspected.text
        created = client.post(
            "/v1/workspaces/w/sources",
            json={
                "provider": "sftp",
                "label": "HTTP SFTP check",
                "root": "/input",
                "configuration": {
                    "target_id": "test-host",
                    "ssh_origin": "clio",
                    "ssh_authentication": "password",
                },
                "sftp_credentials": {"password": "test-password"},
            },
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["id"]
        prefix = f"/v1/workspaces/w/sources/{identifier}"
        assert client.post(prefix + "/disconnect").status_code == 200
        wrong = client.post(prefix + "/auth/sftp", json={"password": "wrong-password"})
        assert wrong.status_code == 409, wrong.text
        assert "wrong-password" not in wrong.text
        signed_in = client.post(prefix + "/auth/sftp", json={"private_key": key})
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["id"] == identifier
        assert signed_in.json()["authenticated"]
        assert signed_in.json()["configuration"]["ssh_authentication"] == "key"
        assert "PRIVATE KEY" not in signed_in.text
        browse = client.post(prefix + "/browse", json={})
        assert browse.status_code == 200, browse.text
        assert browse.json()["entries"][0]["path"] == "sample.txt"
        assert len(client.get("/v1/workspaces/w/sources").json()["sources"]) == 1
