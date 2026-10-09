"""Model download truth, host ownership, immutable retries, and registry isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from clio_schemas.connected_resources import HostStorageLocations
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.infrastructure import node_models
from clio_agent.gact.infrastructure.model_registry import (
    ModelAcquisition,
    ModelDownloadRequest,
    search_models,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    CreateTargetRequest,
    SshRoute,
    TargetFacts,
    UpdateTargetRequest,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes


def job(destination: Path | str, **changes: Any) -> dict[str, Any]:
    return {
        "id": "a" * 24,
        "repository": "org/model",
        "requested_revision": "main",
        "revision": "f" * 40,
        "destination": str(destination),
        "state": "running",
        "phase": "Downloading model files",
        "created_at": 1.0,
        "updated_at": 2.0,
        "bytes_done": 0,
        "bytes_total": None,
        "error": None,
        "pid": 123,
        "process_identity": "boot:1",
        **changes,
    }


def test_inventory_checks_files_and_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "job"
    folder.mkdir()
    model = folder / "weights"
    model.write_bytes(b"model")
    receipt = folder / "receipt.json"
    ready = job(folder, state="ready", verified_files={"weights": [5, model.stat().st_mtime_ns]})
    node_models.write_json(receipt, ready)
    assert node_models.inspect_jobs(tmp_path)[0]["state"] == "ready"
    model.write_bytes(b"tampered")
    assert node_models.inspect_jobs(tmp_path)[0]["state"] == "stale"
    node_models.write_json(receipt, job(folder))
    monkeypatch.setattr(node_models, "process_identity", lambda pid: "different-boot:1")
    assert node_models.inspect_jobs(tmp_path)[0]["state"] == "interrupted"
    monkeypatch.setattr(node_models, "process_identity", lambda pid: "boot:1")
    observed = node_models.inspect_jobs(tmp_path)[0]
    assert observed["state"] == "running"
    assert "pid" not in observed and "process_identity" not in observed


def test_cancel_never_signals_reused_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / ("a" * 24)
    folder.mkdir()
    node_models.write_json(folder / "receipt.json", job(folder))
    monkeypatch.setattr(node_models, "process_identity", lambda pid: "other:2")

    def forbidden(*args: object) -> None:
        pytest.fail("Must not signal another process")

    monkeypatch.setattr(node_models.os, "killpg", forbidden, raising=False)
    assert node_models._cancel_locked(tmp_path, "a" * 24)["state"] == "cancelled"
    assert (folder / "cancel").exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_worker_verifies_hash_and_retries_resolved_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: bool
) -> None:
    import huggingface_hub

    destination = tmp_path / "model"
    destination.mkdir()
    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(destination))
    data = b"weights"
    requests = []

    class Api:
        def model_info(self, repository: str, **kwargs: Any) -> Any:
            requests.append(kwargs)
            return SimpleNamespace(
                sha="f" * 40,
                siblings=[
                    SimpleNamespace(
                        rfilename="weights.bin",
                        size=len(data),
                        lfs=SimpleNamespace(sha256=hashlib.sha256(data).hexdigest()),
                        blob_id=None,
                    )
                ],
            )

    def snapshot(repository: str, **kwargs: Any) -> None:
        assert kwargs["revision"] == "f" * 40
        (destination / "weights.bin").write_bytes(b"invalid" if corrupt else data)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot)
    if corrupt:
        with pytest.raises(node_models.AcquisitionError, match="hash check"):
            node_models.download(receipt)
    else:
        node_models.download(receipt)
    result = json.loads(receipt.read_text())
    assert requests[0]["revision"] == "f" * 40
    assert result["state"] == ("failed" if corrupt else "ready")
    if corrupt:
        assert "hash check" in result["error"]
    else:
        assert node_models.complete(result)


def test_worker_redacts_auth_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub
    import requests
    from huggingface_hub.errors import HfHubHTTPError

    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(tmp_path))

    class Api:
        def model_info(self, *args: Any, **kwargs: Any) -> Any:
            # The pinned worker client raises requests-based errors, not httpx ones.
            response = requests.Response()
            response.status_code = 403
            response.url = "https://registry/secret"
            raise HfHubHTTPError("secret signed URL", response=response)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    with pytest.raises(node_models.AcquisitionError, match="access denied") as error:
        node_models.download(receipt)
    assert error.value.__suppress_context__
    assert "secret" not in str(error.value)
    result = json.loads(receipt.read_text())
    assert result["state"] == "failed" and "access denied" in result["error"]
    assert "secret" not in receipt.read_text()


def test_worker_checks_capacity_before_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import huggingface_hub

    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(tmp_path))

    class Api:
        def model_info(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                sha="f" * 40, siblings=[SimpleNamespace(rfilename="weights.bin", size=2**40)]
            )

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Must reject capacity before downloading bytes")

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", unexpected)
    monkeypatch.setattr(node_models.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(node_models.AcquisitionError, match="Insufficient space"):
        node_models.download(receipt)
    result = json.loads(receipt.read_text())
    assert result["state"] == "failed" and "Insufficient space" in result["error"]


def test_store_retains_model_ownership_across_restart_and_folder_change(tmp_path: Path) -> None:
    path = tmp_path / "infra.json"
    store = InfrastructureStore(path)
    target = store.create_target(
        CreateTargetRequest(label="Node", kind="ssh", ssh=SshRoute(profile="node"))
    )
    store.register_model_root(target.id, "/data/original")
    record = ModelAcquisition.model_validate(
        {**job("/data/original/models/m"), "target_id": target.id, "storage_root": "/data/original"}
    )
    store.put_model_acquisition(record)
    store.set_storage(target.id, HostStorageLocations(root="/data/next"))
    restored = InfrastructureStore(path)
    assert restored.model_roots(target.id) == ["/data/original"]
    assert restored.model_acquisitions(target.id)[0].state == "running"
    with pytest.raises(ValueError, match="owns model storage"):
        restored.delete_target(target.id)
    with pytest.raises(ValueError, match="different SSH route"):
        restored.update_target(
            target.id,
            UpdateTargetRequest(label="Elsewhere", kind="ssh", ssh=SshRoute(profile="other")),
        )


def test_http_reconciles_original_root_and_targets_retry_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    calls = []
    rows = []

    async def facts(target: Any, execute: Any = None) -> TargetFacts:
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="linux",
            arch="x86_64",
            agent_data_root="/data/original",
            uv_available=True,
            transport_state="connected",
        )

    async def execute(target_id: str, spec: CommandSpec) -> CommandResult:
        body = json.loads(spec.stdin)
        calls.append((target_id, body))
        assert app.state.infrastructure_store.model_roots(target_id) == ["/data/original"]
        if body["action"] == "list":
            result = rows
        else:
            result = job(
                "/data/original/models/org--model--main",
                state="cancelled" if body["action"] == "cancel" else "running",
            )
            rows[:] = [result]
        return CommandResult(exit_code=0, stdout=json.dumps(result))

    monkeypatch.setattr("clio_agent.gact.routes.infrastructure_models.probe_target", facts)
    monkeypatch.setattr(app.state.infrastructure_runtime, "execute_on_target", execute)
    with TestClient(app) as client:
        route = "/v1/infrastructure/targets/local/models"
        result = client.post(route, json={"repository": "org/model"})
        assert result.status_code == 202, result.text
        assert calls[0][1]["destination"] == "/data/original/models/org--model--main"
        app.state.infrastructure_store.set_storage(
            "local", HostStorageLocations(root="/data/changed")
        )
        assert client.post(route + "/" + "a" * 24 + "/retry").status_code == 202
        assert calls[-1][1]["root"] == "/data/original"
        assert client.post(route + "/" + "a" * 24 + "/cancel").json()["state"] == "cancelled"
        assert calls[-1][1]["root"] == "/data/original"
        assert client.get(route).json()["models"][0]["destination"].startswith("/data/original")
        assert client.post(route + "/" + "b" * 24 + "/cancel").status_code == 404
        assert client.get("/v1/infrastructure/targets/missing/models").status_code == 404


def test_registry_search_contract_and_input_validation() -> None:
    async def run() -> list[dict[str, Any]]:
        def respond(request: httpx.Request) -> httpx.Response:
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "org/model",
                        "sha": "f" * 40,
                        "gated": "manual",
                        "pipeline_tag": "text-generation",
                    }
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await search_models("small", client=client)

    found = asyncio.run(run())
    assert found[0]["size_bytes"] is None and found[0]["gated"] == "manual"
    with pytest.raises(ValueError):
        ModelDownloadRequest(repository="https://example/unsafe")
    with pytest.raises(ValueError):
        ModelDownloadRequest(repository="org/model", revision="../escape")


def test_disconnected_host_keeps_receipts_and_explains_transport_first(tmp_path: Path) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    store = app.state.infrastructure_store
    target = store.create_target(
        CreateTargetRequest(label="Linux node", kind="ssh", ssh=SshRoute(profile="node"))
    )
    store.register_model_root(target.id, "/data/original")
    store.put_model_acquisition(
        ModelAcquisition.model_validate(
            {
                **job("/data/original/model"),
                "target_id": target.id,
                "storage_root": "/data/original",
            }
        )
    )
    with TestClient(app) as client:
        route = f"/v1/infrastructure/targets/{target.id}/models"
        result = client.get(route).json()
        assert len(result["models"]) == 1
        assert result["unavailable_reason"].startswith("Connect this execution host")
        assert result["errors"]
        response = client.post(route, json={"repository": "org/model"})
        assert response.status_code == 409
        assert response.json()["detail"].startswith("Connect this execution host")


def test_worker_imports_only_stdlib_and_its_declared_client() -> None:
    """The worker runs in its own uv project, not CLIO's environment (live: httpx was missing)."""
    import ast
    import sys

    declared = {
        requirement.split("==")[0].replace("-", "_")
        for requirement in node_models.WORKER_DEPENDENCIES
    }
    tree = ast.parse(Path(node_models.__file__).read_text(encoding="utf-8"))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
    }
    assert imported - set(sys.stdlib_module_names) - {"__future__"} <= declared


def _gguf_repo(data: bytes) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            rfilename=name,
            size=len(data),
            lfs=SimpleNamespace(sha256=hashlib.sha256(data).hexdigest()),
            blob_id=None,
        )
        for name in ("Qwen3-4B-Q4_K_M.gguf", "Qwen3-4B-Q8_0.gguf", "Qwen3-4B-f16.gguf")
    ]


def test_worker_fetches_and_verifies_only_the_selected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import huggingface_hub

    destination = tmp_path / "model"
    destination.mkdir()
    receipt = tmp_path / "receipt.json"
    selected = "Qwen3-4B-Q4_K_M.gguf"
    node_models.write_json(receipt, job(destination, files=[selected]))
    data = b"gguf"
    calls: list[dict[str, Any]] = []

    class Api:
        def model_info(self, repository: str, **kwargs: Any) -> Any:
            return SimpleNamespace(sha="f" * 40, siblings=_gguf_repo(data))

    def snapshot(repository: str, **kwargs: Any) -> None:
        calls.append(kwargs)
        for name in kwargs["allow_patterns"]:
            (destination / name).write_bytes(data)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot)
    node_models.download(receipt)
    result = json.loads(receipt.read_text())
    assert calls[0]["allow_patterns"] == [selected]
    assert result["state"] == "ready"
    assert result["bytes_total"] == len(data)  # the capacity check counts one file
    assert list(result["verified_files"]) == [selected]


def test_worker_rejects_a_file_the_revision_does_not_have(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import huggingface_hub

    destination = tmp_path / "model"
    destination.mkdir()
    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(destination, files=["Qwen3-4B-Q2_K.gguf"]))

    class Api:
        def model_info(self, repository: str, **kwargs: Any) -> Any:
            return SimpleNamespace(sha="f" * 40, siblings=_gguf_repo(b"gguf"))

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    with pytest.raises(node_models.AcquisitionError, match="no file named"):
        node_models.download(receipt)


@pytest.mark.parametrize("name", ["../x.gguf", "/abs.gguf", "*.gguf", "a\\b.gguf", "q?.gguf"])
def test_download_request_accepts_only_plain_file_names(name: str) -> None:
    with pytest.raises(ValueError):
        ModelDownloadRequest(repository="Qwen/Qwen3-4B-GGUF", files=[name])
    request = ModelDownloadRequest(
        repository="Qwen/Qwen3-4B-GGUF", files=["b.gguf", "sub/a.gguf", "b.gguf"]
    )
    assert request.files == ["b.gguf", "sub/a.gguf"]


def _crash_log(folder: Path) -> None:
    (folder / "download.log").write_text(
        "Installed 13 packages\n"
        "GET https://cdn.example/weights?X-Amz-Signature=abc123 token=hf_SECRETSECRET\n"
        "Traceback (most recent call last):\n"
        "ModuleNotFoundError: No module named 'httpx'\n"
    )


def test_supervisor_types_a_crashed_worker_with_its_log_tail(tmp_path: Path) -> None:
    """Live F003: the worker died at import and the UI only said "interrupted; retry"."""
    folder = tmp_path / ("a" * 24)
    folder.mkdir()
    receipt = folder / "receipt.json"
    node_models.write_json(receipt, job(folder, state="queued"))
    _crash_log(folder)
    assert node_models.supervise(receipt, [sys.executable, "-c", "raise SystemExit(3)"]) == 3
    result = json.loads(receipt.read_text())
    assert result["state"] == "failed" and result["error_code"] == "worker_exited"
    assert result["exit_code"] == 3
    assert "exited with code 3" in result["error"]
    assert result["error"].endswith("ModuleNotFoundError: No module named 'httpx'")
    assert result["log_path"] == str(folder / "download.log")
    tail = "\n".join(result["log_tail"])
    assert "Traceback" in tail and "abc123" not in tail and "hf_SECRET" not in tail
    acquisition = ModelAcquisition.model_validate(
        {**node_models.public(result), "target_id": "local", "storage_root": "/data"}
    )
    assert acquisition.error_code == "worker_exited" and acquisition.log_tail


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_supervisor_names_the_signal_that_killed_the_worker(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(tmp_path))
    kill = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    node_models.supervise(receipt, [sys.executable, "-c", kill])
    result = json.loads(receipt.read_text())
    assert result["state"] == "failed" and "killed by SIGKILL" in result["error"]


@pytest.mark.parametrize("terminal", ["ready", "cancel-requested", "failed"])
def test_supervisor_keeps_a_terminal_or_cancelled_receipt(tmp_path: Path, terminal: str) -> None:
    receipt = tmp_path / "receipt.json"
    if terminal == "cancel-requested":
        node_models.write_json(receipt, job(tmp_path))
        (tmp_path / "cancel").touch()
    else:
        node_models.write_json(receipt, job(tmp_path, state=terminal, error="worker reason"))
    before = json.loads(receipt.read_text())
    node_models.supervise(receipt, [sys.executable, "-c", "raise SystemExit(1)"])
    after = json.loads(receipt.read_text())
    assert after["state"] == before["state"] and after["error"] == before["error"]
    assert after.get("exit_code") == (1 if terminal == "failed" else None)


def test_dead_unsupervised_worker_is_typed_with_its_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / ("a" * 24)
    folder.mkdir()
    node_models.write_json(folder / "receipt.json", job(folder))
    _crash_log(folder)
    monkeypatch.setattr(node_models, "process_identity", lambda pid: "")
    row = node_models.inspect_jobs(tmp_path)[0]
    assert row["state"] == "interrupted" and row["error_code"] == "worker_lost"
    assert "No module named 'httpx'" in row["error"] and row["log_tail"]


def test_start_runs_the_worker_under_its_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = tmp_path / "uv"
    uv.write_text("")
    launched: list[list[str]] = []

    def popen(command: list[str], **kwargs: Any) -> Any:
        launched.append(command)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(node_models.shutil, "which", lambda name: str(uv))
    monkeypatch.setattr(node_models.subprocess, "Popen", popen)
    root = tmp_path / "ops"
    request = {"repository": "org/model", "revision": "main", "destination": str(tmp_path / "m")}
    row = node_models.start(root, request, "# worker")
    worker = str(root / row["id"] / "download.py")
    assert launched[0][1:3] == [worker, "--supervise"]
    assert launched[0][4:6] == [str(uv), "run"] and launched[0][-2] == "--worker"
    assert row["error_code"] is None and row["log_tail"] == []


def test_model_verified_on_another_host_stays_ready(tmp_path: Path) -> None:
    """Live: Qwen3-4B (downloaded on gpua005) turned "interrupted" when listed from gpua018."""
    peers = tmp_path / "model-operations"
    here, there = peers / "gpua018", peers / "gpua005"
    here.mkdir(parents=True)
    model = tmp_path / "model"
    model.mkdir()
    (model / "weights").write_bytes(b"model")
    stamp = [5, (model / "weights").stat().st_mtime_ns]
    (there / ("a" * 24)).mkdir(parents=True)
    node_models.write_json(
        there / ("a" * 24) / "receipt.json",
        job(model, state="ready", verified_files={"weights": stamp}),
    )
    (there / ("b" * 24)).mkdir()
    node_models.write_json(there / ("b" * 24) / "receipt.json", job(model, id="b" * 24))
    rows = node_models.inspect_jobs(here, peers=peers)
    assert [(row["id"], row["state"]) for row in rows] == [("a" * 24, "ready")]
    assert node_models.inspect_jobs(here) == []
    (model / "weights").write_bytes(b"changed")
    assert node_models.inspect_jobs(here, peers=peers) == []


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({"state": "ready"}, "stale"),
        (
            {
                "state": "interrupted",
                "phase": "Model available; choose a runtime to serve it",
                "bytes_done": 8,
                "bytes_total": 8,
            },
            "stale",
        ),
        ({"state": "running"}, "interrupted"),
    ],
)
def test_missing_receipt_never_calls_a_finished_download_interrupted(
    changes: dict[str, Any], state: str
) -> None:
    from clio_agent.gact.routes.infrastructure_models import missing_receipt

    prior = ModelAcquisition.model_validate(
        {**job("/data/m"), **changes, "target_id": "local", "storage_root": "/data"}
    )
    relabeled = missing_receipt(prior)
    assert relabeled.state == state and relabeled.error_code == "receipt_missing"


def _hub_snapshot(hub: Path, repository: str, revision: str, files: dict[str, bytes]) -> Path:
    """Lay out one revision the way huggingface_hub's cache does (snapshot -> blob links)."""
    repo = hub / ("models--" + repository.replace("/", "--"))
    (repo / "blobs").mkdir(parents=True, exist_ok=True)
    (repo / "refs").mkdir(exist_ok=True)
    (repo / "refs" / "main").write_text(revision)
    snapshot = repo / "snapshots" / revision
    for name, data in files.items():
        blob = repo / "blobs" / hashlib.sha256(data).hexdigest()
        blob.write_bytes(data)
        link = snapshot / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(blob, link.parent))
    return snapshot


def test_hub_cache_snapshots_are_listed_as_ready_revisions(tmp_path: Path) -> None:
    snapshot = _hub_snapshot(
        tmp_path,
        "unsloth/Llama-3.2-1B-Instruct",
        "a" * 40,
        {"config.json": b"{}", "sub/w.bin": b"xx"},
    )
    assert node_models.is_hub_cache(tmp_path)
    (row,) = node_models.hub_snapshots(tmp_path)
    assert row["repository"] == "unsloth/Llama-3.2-1B-Instruct"
    assert row["revision"] == "a" * 40
    assert row["requested_revision"] == "main"
    assert row["destination"] == str(snapshot)
    assert row["state"] == "ready"
    assert row["bytes_total"] == 4
    ModelAcquisition.model_validate({**row, "target_id": "t", "storage_root": str(tmp_path)})


def test_hub_cache_unfinished_revision_is_never_ready(tmp_path: Path) -> None:
    snapshot = _hub_snapshot(tmp_path, "org/model", "b" * 40, {"config.json": b"{}"})
    (snapshot.parent.parent / "blobs" / "deadbeef.incomplete").write_bytes(b"")
    (row,) = node_models.hub_snapshots(tmp_path)
    assert (row["state"], row["error_code"]) == ("interrupted", "hf_cache_incomplete")
    (snapshot.parent.parent / "blobs" / "deadbeef.incomplete").unlink()
    (snapshot / "gone.bin").symlink_to("../../blobs/missing")
    assert node_models.hub_snapshots(tmp_path)[0]["state"] == "interrupted"


def test_list_on_hub_cache_root_without_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _hub_snapshot(tmp_path, "org/model", "c" * 40, {"config.json": b"{}"})
    monkeypatch.setattr(sys, "argv", ["node_models.py"])
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(read=lambda: json.dumps({"root": str(tmp_path), "action": "list"})),
    )
    node_models.main()
    rows = json.loads(capsys.readouterr().out)
    assert [row["repository"] for row in rows] == ["org/model"]
    assert not (tmp_path / "model-operations").exists()


def test_plain_root_is_not_a_hub_cache(tmp_path: Path) -> None:
    (tmp_path / "org--model--main").mkdir()
    assert not node_models.is_hub_cache(tmp_path)
    assert node_models.hub_snapshots(tmp_path) == []


def test_inventory_lists_a_chosen_hub_cache_models_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    hub = tmp_path / "hub"
    _hub_snapshot(hub, "org/model", "d" * 40, {"config.json": b"{}"})
    plain = tmp_path / "plain"
    plain.mkdir()
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)

    async def facts(target: Any, execute: Any = None) -> TargetFacts:
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="linux",
            arch="x86_64",
            agent_data_root=str(tmp_path / "data"),
            uv_available=True,
            transport_state="connected",
        )

    async def execute(target_id: str, spec: CommandSpec) -> CommandResult:
        done = subprocess.run(
            [sys.executable, *spec.args], input=spec.stdin, capture_output=True, text=True
        )
        return CommandResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    monkeypatch.setattr("clio_agent.gact.routes.infrastructure_models.probe_target", facts)
    monkeypatch.setattr(app.state.infrastructure_runtime, "execute_on_target", execute)
    with TestClient(app) as client:
        route = "/v1/infrastructure/targets/local/models"
        store = app.state.infrastructure_store
        store.set_storage("local", HostStorageLocations(root=str(tmp_path), models=str(hub)))
        body = client.get(route).json()
        assert body["errors"] == []
        (row,) = body["models"]
        assert (row["repository"], row["state"], row["storage_root"]) == (
            "org/model",
            "ready",
            str(hub),
        )
        store.set_storage("local", HostStorageLocations(root=str(tmp_path), models=str(plain)))
        assert client.get(route).json()["errors"] == []
    assert not (hub / "model-operations").exists()
    assert not (plain / "model-operations").exists()
