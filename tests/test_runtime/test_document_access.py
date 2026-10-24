"""Document outputs share their parent's grants while Office profiles remain private."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from clio_agent.runtime import document_runtime, sandbox
from clio_agent.runtime.document_stack import office, process


def test_published_directory_inherits_access_without_changing_its_contents(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        directory = Path(temporary)
        content = directory / "manifest.json"
        content.write_bytes(b"retained evidence")
        process.inherit_parent_access(directory)
        assert not process._windows_dacl_protected(directory)
        assert content.read_bytes() == b"retained evidence"
        if os.name != "nt":
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_protected_cache_root_is_repaired_once_without_recursive_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = [True]
    mutations: list[list[str]] = []
    monkeypatch.setattr(process, "_windows_dacl_protected", lambda path: protected[0])

    def run(command: list[str], **kwargs: Any) -> str:
        assert kwargs == {"cwd": tmp_path, "timeout": 30}
        mutations.append(command)
        protected[0] = False
        return ""

    monkeypatch.setattr(process, "run", run)
    process.inherit_parent_access(tmp_path)
    process.inherit_parent_access(tmp_path)
    assert mutations == [["icacls", str(tmp_path), "/inheritancelevel:e", "/Q"]]


def test_confined_converter_cannot_start_an_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_DOCUMENT_CONFINED", "1")
    monkeypatch.setenv("CLIO_DOCUMENT_OFFICE_ROOT", str(tmp_path / "office"))
    monkeypatch.setattr(process, "find_native", lambda name: None)

    def forbidden(root: Path) -> NoReturn:
        pytest.fail("A confined document worker must not provision Office")

    monkeypatch.setattr(office, "ensure_office", forbidden)
    with pytest.raises(process.DocumentError, match="repair renderer access on the CLIO host"):
        process.office_convert(tmp_path / "deck.pptx", tmp_path / "out", "pdf")


def test_host_created_office_profile_is_accessible_to_its_confined_executor(tmp_path: Path) -> None:
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"immutable deck")
    output = tmp_path / "out"

    def execute(command: list[str], cwd: Path) -> None:
        profile_url = command[1].split("=", 1)[1]
        profile = Path(url2pathname(urlparse(profile_url).path))
        assert not process._windows_dacl_protected(profile.parent)
        assert (profile / "registrymodifications.xcu").is_file()
        assert cwd == output
        (cwd / "deck.pdf").write_bytes(b"%PDF-1.7\npreview")

    pdf = process.office_convert(
        source, output, "pdf", execute=execute, executable_path="soffice", workspace=tmp_path
    )
    assert pdf.read_bytes() == b"%PDF-1.7\npreview"
    assert source.read_bytes() == b"immutable deck"
    assert not list(process.scratch_root(tmp_path).iterdir())


@pytest.mark.parametrize("managed", [False, True])
def test_discovered_managed_renderer_still_receives_host_access_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: bool
) -> None:
    root = tmp_path / "managed-office"
    native = str(root / "program" / "soffice") if managed else str(tmp_path / "system-soffice")
    calls: list[Path] = []

    def prepare(path: Path) -> str:
        calls.append(path)
        return native

    monkeypatch.setattr(document_runtime, "office_runtime_root", lambda: root)
    monkeypatch.setattr(process, "find_native", lambda name: native)
    monkeypatch.setattr(office, "ensure_office", prepare)
    assert document_runtime.prepare_office_runtime() == native
    assert calls == ([root] if managed else [])


@pytest.mark.parametrize(
    "action,source,needs_office",
    [
        ("render", "deck.pptx", True),
        ("recalculate", "data.xlsx", True),
        ("render", "paper.pdf", False),
        ("inspect", "deck.pptx", False),
    ],
)
def test_renderer_is_prepared_on_the_host_before_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    source: str,
    needs_office: bool,
) -> None:
    order: list[str] = []
    environment: dict[str, str] = {}
    monkeypatch.delenv("CLIO_DOCUMENT_SOFFICE", raising=False)

    def prepare() -> str:
        order.append("renderer")
        return "verified-soffice"

    def wrap(command: str, args: list[str], **kwargs: Any) -> SimpleNamespace:
        order.append("confinement")
        return SimpleNamespace(command=command, args=args, env_overlay={}, release=lambda: None)

    def run(command: list[str], **kwargs: Any) -> str:
        environment.update(kwargs["env"])
        return json.dumps({"status": "complete"})

    monkeypatch.setattr(document_runtime, "prepare_office_runtime", prepare)
    monkeypatch.setattr(sandbox, "wrap_confined", wrap)
    monkeypatch.setattr(document_runtime, "_run", run)
    result = document_runtime.run_document_helper(
        {"helper_argv": ["python", "cli.py"]},
        [action, source],
        cwd=tmp_path,
    )
    assert result["status"] == "complete"
    assert order == (["renderer", "confinement"] if needs_office else ["confinement"])
    assert environment["CLIO_DOCUMENT_CONFINED"] == "1"
    assert environment.get("CLIO_DOCUMENT_SOFFICE") == (
        "verified-soffice" if needs_office else None
    )
