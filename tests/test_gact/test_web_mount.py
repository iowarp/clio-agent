"""The web UI mount: SPA history routes get the shell, unknown API paths stay JSON 404s."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.error_middleware import install_error_envelope
from clio_agent.gact.web_mount import mount_web_ui


def _client(web_dir: Path | None) -> TestClient:
    app = FastAPI()
    install_error_envelope(app)

    @app.get("/v1/health")
    def health() -> dict[str, bool]:
        return {"healthy": True}

    mount_web_ui(app, str(web_dir) if web_dir else None)
    return TestClient(app)


def _bundle(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<html>shell</html>")
    (tmp_path / "app.js").write_text("console.log(1)")
    return tmp_path


def test_spa_routes_and_assets_are_served(tmp_path: Path) -> None:
    client = _client(_bundle(tmp_path))

    assert client.get("/").text == "<html>shell</html>"
    assert client.get("/sessions/abc").text == "<html>shell</html>"
    assert client.get("/app.js").text == "console.log(1)"
    assert client.get("/v1/health").json() == {"healthy": True}


def test_unknown_api_path_is_a_json_404_not_the_shell(tmp_path: Path) -> None:
    client = _client(_bundle(tmp_path))

    response = client.get("/v1/no-such-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_nothing_is_mounted_without_a_built_bundle(tmp_path: Path) -> None:
    assert _client(None).get("/").status_code == 404
    assert _client(tmp_path).get("/").status_code == 404
