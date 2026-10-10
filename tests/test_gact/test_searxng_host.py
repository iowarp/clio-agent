"""The files CLIO ships into the SearXNG service directory: launcher, hook, shim, verify."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tarfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest

from clio_agent.gact.infrastructure import (
    searxng_hook,
    searxng_launch,
    searxng_pwd_shim,
    searxng_verify,
)
from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.models import InfrastructureTarget, TargetFacts
from clio_agent.gact.infrastructure.searxng_service import (
    SEARXNG_COMMIT,
    SEARXNG_VERSION,
    engine_key_variable,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def shipped_manifest(**configuration: str) -> dict[str, Any]:
    facts = TargetFacts(
        target_id="local",
        label="This computer",
        os="linux",
        arch="x86_64",
        uv_available=True,
        hostname="node",
        agent_data_root="/data/clio",
    )
    plan = build_driver_plan(
        service_id="searxng",
        action="install",
        variant_id="native",
        configuration=configuration,
        facts=facts,
        target=InfrastructureTarget(id="local", label="This computer", kind="local"),
    )
    return json.loads(plan.commands[-1].stdin or "{}")["manifest"]


class FakeSearxng(BaseHTTPRequestHandler):
    """Answers /config and /search like SearXNG's JSON API."""

    instance: ClassVar[str] = ""
    results: ClassVar[list[dict[str, Any]]] = []
    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urlsplit(self.path).path
        type(self).seen.append(self.path)
        if path == "/config":
            payload: dict[str, Any] = {"instance_name": type(self).instance}
        elif path == "/search":
            payload = {"results": type(self).results, "unresponsive_engines": [["qwant", "x"]]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        del args


@contextmanager
def fake_searxng(instance: str, results: list[dict[str, Any]]) -> Iterator[int]:
    FakeSearxng.instance, FakeSearxng.results, FakeSearxng.seen = instance, results, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSearxng)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def service_root(tmp_path: Path, port: int = 18890, secret: str = "s" * 64) -> Path:
    root = tmp_path / "searxng"
    (root / "evidence").mkdir(parents=True)
    manifest = shipped_manifest()
    manifest["port"] = port
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, content in manifest["files"].items():
        (root / name).write_text(content, encoding="utf-8")
    searxng_hook.write_private(root / "secret_key", secret)
    (root / "receipt.json").write_text(json.dumps({"generation": "g1", "pid": 4242}))
    return root


def test_the_instance_name_is_derived_from_the_secret() -> None:
    first = searxng_hook.instance_name("a" * 64)
    assert first == searxng_hook.instance_name("a" * 64)
    assert first != searxng_hook.instance_name("b" * 64)
    assert first.startswith("CLIO SearXNG ") and len(first.split()[-1]) == 16
    assert "a" * 16 not in first


@POSIX_ONLY
def test_the_secret_file_is_private(tmp_path: Path) -> None:
    searxng_hook.write_private(tmp_path / "secret_key", "x" * 64)
    assert stat.S_IMODE((tmp_path / "secret_key").stat().st_mode) == 0o600
    assert searxng_hook.read_secret(tmp_path) == "x" * 64
    (tmp_path / "secret_key").write_text("short")
    with pytest.raises(RuntimeError, match="invalid"):
        searxng_hook.read_secret(tmp_path)
    with pytest.raises(RuntimeError, match="missing"):
        searxng_hook.read_secret(tmp_path / "absent")


def test_identity_proves_the_answering_server_is_this_installation(tmp_path: Path) -> None:
    secret = "c" * 64
    with fake_searxng(searxng_hook.instance_name(secret), []) as port:
        root = service_root(tmp_path, port, secret)
        assert searxng_hook.running(root)
        assert json.loads((root / "evidence/json-api.json").read_text()) == {
            "generation": "g1",
            "pid": 4242,
        }
        searches = [path for path in FakeSearxng.seen if path.startswith("/search")]
        assert len(searches) == 1 and "format=json" in searches[0]
        assert searxng_hook.running(root)  # the JSON API is proven once per launch
        assert len([path for path in FakeSearxng.seen if path.startswith("/search")]) == 1


def test_a_foreign_searxng_on_the_port_is_not_ours(tmp_path: Path) -> None:
    with fake_searxng("SearXNG", []) as port:
        assert not searxng_hook.running(service_root(tmp_path, port))
    with fake_searxng(searxng_hook.instance_name("d" * 64), []) as port:
        assert not searxng_hook.running(service_root(tmp_path / "other", port, "e" * 64))


def _archive(tmp_path: Path, commit: str) -> Path:
    path = tmp_path / "source.tar.gz"
    # git archive (GitHub's source tarballs) records the commit in the global pax header.
    headers = {"comment": commit}
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers=headers) as archive:
        for name, text in (
            ("searx/__init__.py", "settings = {}\n"),
            ("searx/webapp.py", "app = None\n"),
        ):
            data = text.encode()
            info = tarfile.TarInfo(f"searxng-{commit}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_the_source_must_be_the_pinned_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "environment").mkdir(parents=True)
    manifest = shipped_manifest()
    good = _archive(tmp_path, SEARXNG_COMMIT)
    monkeypatch.setattr(searxng_hook.urllib.request, "urlopen", lambda *_a, **_k: good.open("rb"))
    record = searxng_hook.install_source(root, manifest)
    source = root / "environment" / "searxng-source"
    assert (source / "searx" / "webapp.py").read_text() == "app = None\n"
    assert record["commit"] == SEARXNG_COMMIT and len(record["archive_sha256"]) == 64

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an installed pinned source is reused, not downloaded")

    monkeypatch.setattr(searxng_hook.urllib.request, "urlopen", refuse)
    assert searxng_hook.install_source(root, manifest)["commit"] == SEARXNG_COMMIT

    other = tmp_path / "other"
    (other / "environment").mkdir(parents=True)
    (tmp_path / "w").mkdir()
    wrong = _archive(tmp_path / "w", "0" * 40)
    monkeypatch.setattr(searxng_hook.urllib.request, "urlopen", lambda *_a, **_k: wrong.open("rb"))
    with pytest.raises(RuntimeError, match="not the pinned commit"):
        searxng_hook.install_source(other, manifest)
    assert not (other / "environment" / "searxng-source").exists()


def test_settings_carry_the_instance_name_but_no_credential() -> None:
    manifest = shipped_manifest()
    secret = "f" * 64
    document = searxng_launch.settings_document(manifest, secret)
    assert document["general"]["instance_name"] == searxng_hook.instance_name(secret)
    assert secret not in json.dumps(document)
    assert manifest["searxng"]["settings"]["general"]["instance_name"] == "CLIO SearXNG"


def test_proxy_and_engine_keys_reach_only_the_loaded_settings() -> None:
    manifest = shipped_manifest(engines="wikipedia,braveapi")
    variable = engine_key_variable("braveapi")
    settings: dict[str, Any] = {"engines": [{"name": "wikipedia"}, {"name": "braveapi"}]}
    environ = {"HTTPS_PROXY": "http://user:pw@proxy:3128", variable: "brave-key"}
    searxng_launch.apply_private_settings(settings, manifest, environ)
    assert settings["outgoing"]["proxies"] == {"all://": ["http://user:pw@proxy:3128"]}
    assert settings["engines"][1]["api_key"] == "brave-key"
    assert "api_key" not in settings["engines"][0]


def _fake_source(root: Path) -> None:
    package = root / "environment" / "searxng-source" / "searx"
    package.mkdir(parents=True)
    (package / "webapp.py").write_text("app = None\n")
    (package / "__init__.py").write_text(
        "import json, os\n"
        "settings = json.loads(open(os.environ['SEARXNG_SETTINGS_PATH']).read())\n"
        "settings['engines'] = [{'name': 'braveapi'}]\n"
        "SECRET = os.environ['SEARXNG_SECRET']\n"
    )


def test_boot_prepares_unmodified_searxng(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "9" * 64
    root = service_root(tmp_path, secret=secret)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["searxng"]["engine_keys"] = {"braveapi": engine_key_variable("braveapi")}
    manifest["searxng"]["engine_key_settings"] = {"braveapi": "api_key"}
    (root / "manifest.json").write_text(json.dumps(manifest))
    _fake_source(root)
    monkeypatch.setattr(sys, "path", list(sys.path))
    # SearXNG reads os.environ on import, as the launcher's real process does.
    for name in ("SEARXNG_SECRET", "SEARXNG_SETTINGS_PATH", "PYTHONPATH"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv(engine_key_variable("braveapi"), "brave-key")
    environ = os.environ
    try:
        searxng_launch.boot(root, environ)
        import searx  # type: ignore[import-not-found]  # the fake source above

        frozen = sys.modules["searx.version_frozen"]
        assert frozen.VERSION_STRING == SEARXNG_VERSION
        assert searx.SECRET == secret
        assert searx.settings["engines"][0]["api_key"] == "brave-key"
        written = (root / "searxng-settings.yml").read_text()
        assert secret not in written and "brave-key" not in written
        assert json.loads(written)["general"]["instance_name"] == searxng_hook.instance_name(secret)
        assert environ["SEARXNG_SETTINGS_PATH"] == str(root / "searxng-settings.yml")
        if sys.platform != "win32":
            mode = stat.S_IMODE((root / "searxng-settings.yml").stat().st_mode)
            assert mode == 0o600
    finally:
        for name in ("searx", "searx.version_frozen", "searxng_hook"):
            sys.modules.pop(name, None)
        if sys.platform == "win32":
            sys.modules.pop("pwd", None)  # boot installed the shim


def test_boot_refuses_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="source is missing"):
        searxng_launch.boot(service_root(tmp_path), {})


def test_the_pwd_shim_is_installed_on_windows_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = Path(searxng_pwd_shim.__file__).read_text(encoding="utf-8")
    (tmp_path / "clio_pwd_shim.py").write_text(shim, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "pwd", raising=False)
    assert not searxng_launch.install_pwd_shim(tmp_path, "posix")
    assert "pwd" not in sys.modules
    monkeypatch.setenv("USERNAME", "scientist")
    assert searxng_launch.install_pwd_shim(tmp_path, "nt")
    import pwd  # noqa: PLC0415 - resolves to the shim installed above

    entry = pwd.getpwuid(os.getpid())  # what searx.valkeydb does on a Valkey error
    assert (entry.pw_name, entry.pw_uid) == ("scientist", os.getpid())
    assert pwd.getpwnam("scientist").pw_name == "scientist"
    with pytest.raises(KeyError):
        pwd.getpwnam("somebody-else")
    assert not searxng_launch.install_pwd_shim(tmp_path, "nt")  # already present


def test_the_pwd_shim_mirrors_struct_passwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setenv("USER", "fallback")
    entry = searxng_pwd_shim.getpwuid(7)
    assert entry._fields == (
        "pw_name",
        "pw_passwd",
        "pw_uid",
        "pw_gid",
        "pw_gecos",
        "pw_dir",
        "pw_shell",
    )
    assert entry.pw_name == "fallback" and entry.pw_uid == 7
    assert searxng_pwd_shim.getpwall()[0].pw_name == "fallback"


def test_verification_counts_real_results(tmp_path: Path) -> None:
    rows = [
        {"url": "https://www.hdfgroup.org", "engines": ["duckduckgo", "brave"]},
        {"url": "https://en.wikipedia.org/wiki/HDF5", "engines": ["wikipedia"]},
        {"title": "no url"},
    ]
    with fake_searxng("x", rows) as port:
        evidence = searxng_verify.verify(service_root(tmp_path, port))
    assert evidence["result_count"] == 2 and evidence["search_ok"] is True
    assert evidence["engines_answered"] == ["brave", "duckduckgo", "wikipedia"]
    with fake_searxng("x", []) as port, pytest.raises(RuntimeError, match="no results"):
        searxng_verify.verify(service_root(tmp_path / "empty", port))
