"""iowarp/clio-agent#1363 (A2UI live gate, campaign #1363): the first-bind
``PUT /v1/providers/lm`` defect.

On a pristine install (no config file, no ``CLIO_LM_MODEL``, LM Studio not
running), binding a non-lm_studio provider used to 400 ``config_error``. Root
cause: the first-bind path (``app.state.agent is None``) built the singleton
``ClioAgent`` via ``construct_agent_with_relay`` WITHOUT the requested
provider config, so ``ClioAgent.__init__`` fell back to
``load_config_from_env()`` -- provider defaults to ``lm_studio`` with an empty
model -- and ran LM Studio discovery against ``http://127.0.0.1:1234/v1``
*before* ``agent.rebind_lms(cfg)`` ever applied the caller's real provider.

These tests build the REAL ``ClioAgent`` through the REAL ``PUT`` route (no
``clio_agent.agent.ClioAgent`` stub, unlike every other first-bind test in
``test_lm_provider.py``) so the actual ``__init__`` discovery branch is
exercised, and assert LM Studio discovery is never reached for a non-lm_studio
bind. The mirror test proves the LM Studio discovery-on-empty-model path is
still intact.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._config_layer import delete_config


def _patch_hermetic_bind_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two real network dependencies every bind reaches on its way
    through ``_apply_lm_provider`` (mirrors ``test_lm_provider.py``'s
    ``_patch_ambient_bind_network``): the per-provider connectivity handshake
    and the relay/MCP-federation catalog discovery the first-bind path pulls
    in via ``construct_agent_with_relay``. Neither is what these tests are
    about, and both can legitimately take seconds against ambient conditions
    (or hang against a claude_code/codex CLI that isn't logged in on this
    box), so they are stubbed for hermetic, fast isolation exactly like the
    rest of the ``PUT /v1/providers/lm`` suite.
    """

    async def _unreachable_handshake(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("handshake stubbed for hermetic test (no live backend)")

    async def _no_relay_kwargs(app: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _unreachable_handshake)
    monkeypatch.setattr("clio_agent.gact.relay_wiring.relay_agent_kwargs", _no_relay_kwargs)


def _forbid_lm_studio_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make LM Studio discovery fail loud instead of hanging on a real
    (absent) ``http://127.0.0.1:1234`` connect, so a regression shows up as a
    typed assertion instead of a 30s+ retry-loop timeout.

    Patched under BOTH bound names discovery can be reached through:

    * ``clio_agent.config.list_lm_studio_models`` -- the lazy-import seam
      ``lm/factory.py::_resolve_lm_studio_model_if_needed`` re-reads at call
      time (reached from ``create_lm``, called for every bind regardless of
      provider).
    * ``clio_agent.agent.list_lm_studio_models`` -- the name
      ``ClioAgent.__init__``'s own discovery branch calls directly. This is a
      SEPARATE binding (``from clio_agent.config import list_lm_studio_models``
      at ``agent.py`` module-import time): patching only the config module's
      attribute does not reach it, since agent.py already holds its own copy
      of the original function reference. This second binding is the one the
      live #1363 bug actually calls (agent.py's own eager import), so it is
      the one that must be patched to make a regression here fail loud.
    """

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LM Studio discovery must not run for a claude_code bind")

    monkeypatch.setattr("clio_agent.config.list_lm_studio_models", _raise)
    monkeypatch.setattr("clio_agent.agent.list_lm_studio_models", _raise)


def _wait_lm_provider_ready(c: TestClient, timeout_s: float = 30.0) -> dict[str, Any]:
    """Poll ``GET /v1/providers/lm`` until an async lm_studio/argonne bind
    leaves its ``configuring`` state (mirrors ``test_lm_provider.py``'s
    helper of the same name)."""

    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = c.get("/v1/providers/lm").json()
        if body.get("state") != "configuring":
            return body
        time.sleep(0.05)
    return body


def _pristine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the pristine-install state the A2UI live gate exercises: no
    ``lm.model`` in the per-test config-file layer (the autouse
    ``allow_pytest_tmp_path`` fixture normally pins ``lm.model:
    ibm/granite-4-h-tiny`` there, which alone would suppress the bug -- file
    wins over env in ``clio_agent.conf``) and no ``CLIO_LM_MODEL`` in the
    environment.
    """

    delete_config("lm.model")
    monkeypatch.delenv("CLIO_LM_MODEL", raising=False)


def test_claude_code_then_codex_first_bind_never_runs_lm_studio_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claude_sdk_installed: Any,
) -> None:
    """A pristine-install claude_code bind must never touch LM Studio
    discovery -- the first-bind path must construct ``ClioAgent`` off the
    REQUESTED provider config, not the ambient (lm_studio-defaulting) boot
    env. A follow-up codex bind (now a hot-swap of the already-constructed
    agent) must stay clean too.
    """

    _pristine_env(monkeypatch)
    _patch_hermetic_bind_network(monkeypatch)
    _forbid_lm_studio_discovery(monkeypatch)
    # A codex bind requires present credentials AND an SDK-validated catalog
    # (subscription availability check). Pin both in tmp_path so the result
    # never depends on the host's real ~/.codex sign-in.
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    from clio_agent.providers import model_discovery

    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "Sol", "description": ""}],
            source=model_discovery.CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "claude_code",
                "api_base": "claude-code://sdk",
                "model": "sonnet",
                "api_key": "x",
                "temperature": 0.0,
                "max_tokens": 0,
                "context_length": 0,
                "parallel": 0,
                "turn_timeout_s": 0.0,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is True
        assert body["provider"] == "claude_code"
        assert body["model"] == "sonnet"

        resp2 = c.put(
            "/v1/providers/lm",
            json={
                "provider": "codex",
                "api_base": "codex://sdk",
                "model": "gpt-5.6-sol",
                "api_key": "x",
                "temperature": 0.0,
                "max_tokens": 0,
                "context_length": 0,
                "parallel": 0,
                "turn_timeout_s": 0.0,
            },
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        assert body2["configured"] is True
        assert body2["provider"] == "codex"
        assert body2["model"] == "gpt-5.6-sol"


def test_lm_studio_empty_model_first_bind_still_discovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror case: an lm_studio bind with an EMPTY model must still run
    discovery (proving the fix does not collaterally suppress the legitimate
    LM Studio discovery-on-first-bind path)."""

    _pristine_env(monkeypatch)
    _patch_hermetic_bind_network(monkeypatch)

    def _fake_list_lm_studio_models(*args: Any, **kwargs: Any) -> list[str]:
        return ["stub-lm-studio-model"]

    monkeypatch.setattr("clio_agent.config.list_lm_studio_models", _fake_list_lm_studio_models)
    monkeypatch.setattr("clio_agent.agent.list_lm_studio_models", _fake_list_lm_studio_models)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "lm_studio",
                "api_base": "http://127.0.0.1:1234/v1",
                "model": "",
                "api_key": "x",
                "temperature": 0.0,
                "max_tokens": 0,
                "context_length": 0,
                "parallel": 0,
                "turn_timeout_s": 0.0,
            },
        )
        assert resp.status_code == 200, resp.text
        body = _wait_lm_provider_ready(c)

    assert body["state"] == "ready", body
    assert body["provider"] == "lm_studio"
    assert body["model"] == "stub-lm-studio-model"
