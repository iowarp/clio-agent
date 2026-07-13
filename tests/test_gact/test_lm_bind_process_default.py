"""Regression tests for the admin-bind process-default + atomicity group.

These cover the three adversarially-confirmed defects in the demoted
``PUT /v1/providers/lm`` bind (design ``docs/archive/per-expert-provider-lm.md``
§5/§6):

* **#2 deferred-boot ambient 503** — a GACT booted *without* ``CLIO_LM_PROVIDER``
  (configured only via a later PUT — the gact-tui Save+Connect flow) never runs
  the boot ``dspy.configure``. The demoted bind must install the process-default
  LM so every ambient consumer (manual context compaction, usage/token metering,
  the turn-end model-id probe) resolves a valid LM instead of ``None`` (503).
* **#3 rebind stale ambient model** — after ``PUT A -> PUT B`` the ambient
  ``dspy.settings.lm`` must report B, not the stale boot/first model, because the
  admin bind *refreshes* the process default on every bind.
* **#1 concurrent cloud binds** — two concurrent cloud PUTs must be serialized by
  the in-progress 409 guard (previously only ``lm_studio``/``argonne`` set
  ``app.state.lm_config_task``), so the singleton agent is published as ONE
  internally-consistent object (LM + adapter from the same provider), never a
  field-torn mix.

The admin/default bind is the ONLY writer of the process default; experts still
resolve their own LM per ``dspy.context`` (design §6). These tests assert the
default is installed/refreshed by the bind, not that experts read it.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import httpx
import pytest
from dspy.dsp.utils.settings import main_thread_config
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import _current_lm_model_id, build_app

_REMOVED_BIND_ENV_KEYS = (
    "CLIO_LM_PROVIDER",
    "CLIO_LM_API_BASE",
    "CLIO_LM_MODEL",
    "CLIO_LM_API_KEY",
    "CLIO_CODEX_TRANSPORT",
    "CLIO_CLAUDE_CODE_TRANSPORT",
)


def _fake_lm(cfg: Any) -> Any:
    """A stand-in dspy LM tagged with the config's identity (never network-called)."""
    return SimpleNamespace(model=cfg.model, provider=cfg.provider, history=[])


def _fake_adapter(cfg: Any) -> Any:
    return SimpleNamespace(provider=cfg.provider)


class _RebindLMStub:
    """Mixin giving a bind-path stub a bound ``rebind_lms`` mirroring ClioAgent's.

    The provider hot-swap now does ``agent = copy.copy(existing); agent.rebind_lms(cfg)``,
    so every stub that flows through a bind needs a real bound method — a plain
    ``SimpleNamespace`` attribute would not receive ``self``.
    """

    def rebind_lms(self, cfg: Any) -> None:
        self._provider_config = cfg
        self._main_lm = _fake_lm(cfg)
        self._planner_lm = _fake_lm(cfg)
        self._dspy_adapter = _fake_adapter(cfg)


class _StubBindAgent(_RebindLMStub, SimpleNamespace):
    """SimpleNamespace-style agent stub that survives the hot-swap rebind."""


def _install_stub_factories(monkeypatch: pytest.MonkeyPatch, *, create_lm: Any = None) -> None:
    """Stub the LM factories + handshake so a bind needs no network."""

    monkeypatch.setattr("clio_agent.config.create_lm", create_lm or _fake_lm)
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", _fake_adapter)
    monkeypatch.setattr("clio_agent.config.create_planner_lm", _fake_lm)

    async def _no_handshake(ctx: Any, **kwargs: Any) -> Any:
        raise RuntimeError("handshake disabled in test")

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _no_handshake)


# --------------------------------------------------------------------------- #
# #2 — deferred boot: the bind installs the ambient process default
# --------------------------------------------------------------------------- #


def test_deferred_boot_put_installs_process_default_for_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred boot (no ``CLIO_LM_PROVIDER``) then PUT: the ambient default is set,
    so a manual compaction (an ambient call) uses the bound LM and does NOT 503.
    """
    for key in _REMOVED_BIND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Deferred boot: the boot ``dspy.configure`` never ran, so the ambient default
    # is unset — exactly the state a GACT started without CLIO_LM_PROVIDER is in.
    monkeypatch.setitem(main_thread_config, "lm", None)
    monkeypatch.setitem(main_thread_config, "adapter", None)

    real_arc = ARCMemory(data_dir=str(tmp_path / "arc"))

    class _StubAgent(_RebindLMStub):
        def __init__(self, *args: Any, arc: Any = None, **kwargs: Any) -> None:
            # Keep the real injected ARC so the compaction route has live segments.
            self.arc = arc

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    _install_stub_factories(monkeypatch)

    # A summariser mirroring real dspy: no bound LM -> raise (caller returns "" ->
    # 503); a bound LM -> a summary (200). This is what distinguishes the bug
    # (ambient lm=None) from the fix (ambient lm=bound).
    class _FakePredict:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def __call__(self, *, prior_context: str, lm: Any = None) -> Any:
            if lm is None:
                raise RuntimeError("no LM configured")
            return SimpleNamespace(summary="COMPACT_OK")

    monkeypatch.setattr(dspy, "Predict", _FakePredict)

    app = build_app(sessions_path=tmp_path / "s.json", arc=real_arc)
    with TestClient(app) as c:
        assert app.state.agent is None  # deferred boot: no agent yet
        assert main_thread_config["lm"] is None  # ambient default really unset

        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://deferred.example/v1",
                "model": "bound-model",
                "api_key": "k",
            },
        )
        assert resp.status_code == 200, resp.text

        # The admin bind installed the process default -> ambient reads resolve it.
        assert getattr(main_thread_config["lm"], "model", None) == "bound-model"
        assert _current_lm_model_id() == "bound-model"

        # A manual compaction is an ambient call (no expert dspy.context). It must
        # now find the bound LM and summarise, not 503 on lm=None.
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        real_arc.append_segment(sid, "agentA", "thought", {"text": "T0"}, step=0, token_count=5)
        real_arc.append_segment(sid, "agentA", "observation", {"text": "O0"}, step=0, token_count=9)
        r = c.post(f"/v1/sessions/{sid}/context/compact", params={"scope": "agentA"})
        assert r.status_code == 200, r.text
        assert "COMPACT_OK" in r.json()["render_text"]


# --------------------------------------------------------------------------- #
# #3 — rebind refreshes the ambient process default
# --------------------------------------------------------------------------- #


def test_rebind_refreshes_ambient_process_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After ``PUT A -> PUT B`` the ambient model reports B, not the stale boot/A
    model — the admin bind refreshes the process default on every bind.
    """
    for key in _REMOVED_BIND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Eager boot: a boot default was installed once (as dspy.configure would). A
    # rebind must refresh it, not leave it pinned to the boot model.
    monkeypatch.setitem(main_thread_config, "lm", SimpleNamespace(model="boot-model", history=[]))
    monkeypatch.setitem(main_thread_config, "adapter", SimpleNamespace())

    existing = _StubBindAgent(
        arc=ARCMemory(data_dir=str(tmp_path / "arc")),
        _provider_config=SimpleNamespace(provider="openai", model="boot-model"),
        _main_lm=SimpleNamespace(model="boot-model", provider="openai"),
        _planner_lm=SimpleNamespace(model="boot-model", provider="openai"),
        _dspy_adapter=SimpleNamespace(provider="openai"),
    )
    _install_stub_factories(monkeypatch)

    app = build_app(sessions_path=tmp_path / "s.json", agent=existing)
    with TestClient(app) as c:
        c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://a.example/v1",
                "model": "model-a",
                "api_key": "k",
            },
        ).raise_for_status()
        assert _current_lm_model_id() == "model-a"
        assert getattr(main_thread_config["lm"], "model", None) == "model-a"

        c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://b.example/v1",
                "model": "model-b",
                "api_key": "k",
            },
        ).raise_for_status()
        assert _current_lm_model_id() == "model-b"
        assert getattr(main_thread_config["lm"], "model", None) == "model-b"


# --------------------------------------------------------------------------- #
# #1 — concurrent cloud binds are serialized + internally consistent
# --------------------------------------------------------------------------- #


@pytest.mark.concurrency
def test_concurrent_cloud_binds_serialized_and_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent cloud PUTs are serialized by the in-progress 409 guard.

    Previously only ``lm_studio``/``argonne`` registered ``app.state.lm_config_task``,
    so the guard never fired for cloud providers and two cloud PUTs mutated the
    singleton agent's ``_main_lm`` / ``_dspy_adapter`` concurrently (a torn mix).
    The fix registers the in-flight bind for *every* provider, so exactly one of two
    concurrent cloud PUTs is admitted (200) and the other is rejected (409); the
    published agent carries LM + adapter from the SAME provider.
    """
    for key in _REMOVED_BIND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    # A pre-existing agent so both concurrent binds hit the hot-swap path (the exact
    # site the finding tore field-by-field).
    existing = _StubBindAgent(
        arc=ARCMemory(data_dir=str(tmp_path / "arc")),
        _provider_config=SimpleNamespace(provider="boot", model="boot-model"),
        _main_lm=SimpleNamespace(model="boot-model", provider="boot"),
        _planner_lm=SimpleNamespace(model="boot-model", provider="boot"),
        _dspy_adapter=SimpleNamespace(provider="boot"),
    )

    gate = threading.Event()

    def _blocking_create_lm(cfg: Any) -> Any:
        # Block the admitted bind mid-flight (in the executor worker thread) so the
        # event loop is free to service the second concurrent PUT and hit the guard.
        gate.wait(timeout=5.0)
        return SimpleNamespace(model=cfg.model, provider=cfg.provider, history=[])

    _install_stub_factories(monkeypatch, create_lm=_blocking_create_lm)

    app = build_app(sessions_path=tmp_path / "s.json", agent=existing)

    async def _run() -> list[Any]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:

            async def _put(provider_model: tuple[str, str, str]) -> int:
                api_base, model, _ = provider_model
                resp = await client.put(
                    "/v1/providers/lm",
                    json={
                        "provider": "openai",
                        "api_base": api_base,
                        "model": model,
                        "api_key": "k",
                    },
                )
                return resp.status_code

            async def _releaser() -> None:
                # Give both requests time to pass/hit the guard, then unblock the
                # admitted bind so it can finish.
                await asyncio.sleep(0.4)
                gate.set()

            return await asyncio.gather(
                _put(("http://a.example/v1", "model-a", "a")),
                _put(("http://b.example/v1", "model-b", "b")),
                _releaser(),
            )

    results = asyncio.run(_run())
    codes = sorted(results[:2])

    # Exactly one admitted, one rejected as configuring-in-progress.
    assert codes == [200, 409], codes

    # The published agent is a NEW object (atomic pointer swap, not the mutated
    # singleton) and carries LM + adapter from the SAME provider/model — never torn.
    published = app.state.agent
    assert published is not existing
    assert published._main_lm.model == published._provider_config.model
    assert published._main_lm.provider == published._dspy_adapter.provider
    # And the store default agrees with the published agent (one consistent state).
    assert app.state.provider_profiles.default.model == published._main_lm.model
