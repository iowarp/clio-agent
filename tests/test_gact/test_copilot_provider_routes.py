"""GitHub Copilot on the GACT surface: readiness, the handshake gate, binding and sign-in.

* the row is ready only when CLIO is signed in to GitHub AND a live ``list_models()``
  read under that sign-in is recorded and fresh;
* the handshake gate re-reads the account and reports a refused token as
  ``auth: rejected``, a missing install by name, anything else in the runtime's words;
* binding Copilot goes through the subscription gate with typed 401/503 codes;
* the Copilot row's sign-in IS CLIO's GitHub device flow and vault entry, and a
  dropped GitHub grant releases the Copilot sessions and marks its list unverified;
* the doctor probe and the turn failure text name Copilot's own state.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from clio_agent.config import LMProviderConfig
from clio_agent.gact.providers.bind_configuration import (
    _apply_subscription_gate,
    prepare_bind_configuration,
)
from clio_agent.gact.routes import provider_auth_copilot as adapter
from clio_agent.gact.storage import github_oauth
from clio_agent.gact.storage.auth import NON_EXPIRING_TOKEN_LIFETIME_S
from clio_agent.gact.storage.github_access import GitHubAccess
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.stream_failures import forward_error_info, provider_auth_failure
from clio_agent.gact.types import LMProviderRequest
from clio_agent.providers.components import client_binary
from clio_agent.providers.copilot import auth
from clio_agent.providers.copilot.errors import (
    COPILOT_INSTALL_FAILED_MESSAGE,
    COPILOT_SIGNED_OUT_MESSAGE,
    CopilotAuthRequiredError,
)
from clio_agent.providers.copilot.sessions import SESSION_POOL
from clio_agent.providers.model_discovery import overlay
from clio_agent.runtime.lm_provider_probe import probe_cli_transport
from clio_agent.runtime.status import IntegrationState
from tests import _fake_copilot_sdk as fake

_PRESET = SimpleNamespace(id="copilot", provider="copilot")


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "model_catalog.json"))
    fake.reset()
    client_binary.reset_client_cache()
    yield
    fake.reset()
    client_binary.reset_client_cache()


def _service(*, signed_in: bool = True) -> Any:
    return SimpleNamespace(
        principal="user",
        store=SimpleNamespace(clio_id="clio"),
        auth=SimpleNamespace(account_connected=lambda *_args: signed_in),
    )


def _app(service: Any = None, *, checking: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.connected_storage = service
    app.state.provider_catalog_startup_task = SimpleNamespace(done=lambda: not checking)
    return app


def _verified(default: str = "gpt-5") -> None:
    overlay.record_refresh(
        overlay.ProviderDiscoveryResult(
            provider="copilot",
            discovered=[{"id": default}, {"id": "claude-sonnet-4.5"}],
            source=overlay.COPILOT_SOURCE,
            default_model=default,
        )
    )


# --------------------------------------------------------------------------- #
# Readiness                                                                   #
# --------------------------------------------------------------------------- #
def test_missing_support_is_install_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "copilot", None)  # find_spec: not importable
    status, message, verified, _ = adapter.copilot_readiness(_app(_service()))
    assert (status, verified) == ("install_required", False)
    assert "Copilot" in message


def test_without_a_github_sign_in_the_row_asks_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch)
    _verified()  # a list from an earlier sign-in does not make a signed-out row ready
    for app in (_app(None), _app(_service(signed_in=False))):
        assert adapter.copilot_readiness(app) == (
            "auth_required",
            COPILOT_SIGNED_OUT_MESSAGE,
            False,
            "",
        )


def test_a_running_startup_check_is_reported_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch)
    _verified()
    status, message, verified, _ = adapter.copilot_readiness(_app(_service(), checking=True))
    assert (status, verified) == ("auth_check_required", False)
    assert "being checked" in message
    assert adapter.copilot_readiness(_app(_service(), checking=True), ignore_startup=True)[2]


def test_signed_in_but_never_checked_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch)
    status, message, verified, _ = adapter.copilot_readiness(_app(_service()))
    assert (status, verified) == ("auth_check_required", False)
    assert "verify Copilot access" in message


def test_a_fresh_verified_list_is_ready_with_the_accounts_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.install(monkeypatch)
    _verified("claude-sonnet-4.5")
    status, _, verified, default = adapter.copilot_readiness(_app(_service()))
    assert (status, verified, default) == ("ready", True, "claude-sonnet-4.5")


def test_a_list_from_a_previous_grant_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch)
    _verified()
    overlay.update_entry_fields("copilot", {"account_changed_at": "2026-10-22T00:00:00+00:00"})
    status, _, verified, _ = adapter.copilot_readiness(_app(_service()))
    assert (status, verified) == ("auth_check_required", False)


def test_a_malformed_catalog_is_unavailable_with_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.install(monkeypatch)
    overlay.overlay_path().write_text("{not json", encoding="utf-8")
    status, message, verified, _ = adapter.copilot_readiness(_app(_service()))
    assert (status, verified) == ("unavailable", False)
    assert message.startswith("GitHub Copilot model catalog is invalid")


# --------------------------------------------------------------------------- #
# Handshake gate                                                              #
# --------------------------------------------------------------------------- #
async def test_a_refresh_reads_the_live_list_and_then_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch, models=[fake.model_info("gpt-5")])
    assert await adapter.copilot_handshake_gate(_app(_service()), _PRESET, refresh=True) is None
    assert sdk.clients and sdk.clients[0].kwargs["use_logged_in_user"] is False
    assert adapter.copilot_readiness(_app(_service()))[0] == "ready"


async def test_a_refused_token_is_auth_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, authenticated=False, status_message="Bad credentials")
    report = await adapter.copilot_handshake_gate(_app(_service()), _PRESET, refresh=True)
    assert report is not None
    assert (report["auth"], report["connectivity"]) == ("rejected", "ok")
    assert report["error"] == COPILOT_SIGNED_OUT_MESSAGE
    assert report["models"] == [] and report["source"] == "unavailable"


async def test_a_missing_runtime_names_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.copilot import runtime  # noqa: PLC0415
    from clio_agent.providers.copilot.errors import (  # noqa: PLC0415
        COPILOT_RUNTIME_MISSING_MESSAGE,
        CopilotRuntimeMissingError,
    )

    fake.install(monkeypatch)

    def missing() -> str:
        raise CopilotRuntimeMissingError(COPILOT_RUNTIME_MISSING_MESSAGE, detail="none")

    monkeypatch.setattr(runtime, "runtime_path", missing)
    report = await adapter.copilot_handshake_gate(_app(_service()), _PRESET, refresh=True)
    assert report is not None
    assert report["error"] == COPILOT_INSTALL_FAILED_MESSAGE
    assert (report["auth"], report["connectivity"]) == ("deferred", "unreachable")


async def test_any_other_failure_keeps_the_runtimes_words(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, list_error=fake.JsonRpcError("models.list: 503"))
    report = await adapter.copilot_handshake_gate(_app(_service()), _PRESET, refresh=True)
    assert report is not None
    assert report["error"].endswith("JsonRpcError: models.list: 503")
    assert report["auth"] == "deferred"


async def test_signed_out_never_starts_a_runtime_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch)
    report = await adapter.copilot_handshake_gate(
        _app(_service(signed_in=False)), _PRESET, refresh=True
    )
    assert report is not None
    assert (report["auth"], report["connectivity"]) == ("missing", "skipped")
    assert report["error"] == COPILOT_SIGNED_OUT_MESSAGE
    assert sdk.clients == []


async def test_without_refresh_the_gate_only_reads_local_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch)
    report = await adapter.copilot_handshake_gate(_app(_service()), _PRESET, refresh=False)
    assert report is not None and report["auth"] == "deferred"
    _verified()
    assert await adapter.copilot_handshake_gate(_app(_service()), _PRESET, refresh=False) is None
    assert sdk.clients == []


# --------------------------------------------------------------------------- #
# Binding                                                                     #
# --------------------------------------------------------------------------- #
def _request(model: str = "") -> LMProviderRequest:
    return LMProviderRequest(
        provider="copilot", provider_id="copilot", api_base="copilot://sdk", model=model
    )


async def test_the_bind_gate_fills_the_accounts_default_model() -> None:
    cfg = LMProviderConfig(provider="copilot", model="", api_base="copilot://sdk", api_key="x")
    await _apply_subscription_gate(
        cfg, _request(), lambda: ("ready", "ok", True, "gpt-5"), "copilot"
    )
    assert cfg.model == "gpt-5"


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        ("auth_required", 401, "copilot_auth_required"),
        ("auth_check_required", 401, "copilot_auth_required"),
        ("install_required", 503, "copilot_install_required"),
        ("support_restoring", 503, "copilot_install_required"),
        ("unavailable", 503, "copilot_auth_required"),
    ],
)
async def test_an_unverified_bind_is_a_typed_refusal_after_one_recheck(
    monkeypatch: pytest.MonkeyPatch, status: str, code: int, error: str
) -> None:
    from clio_agent.providers import model_discovery  # noqa: PLC0415

    rechecked: list[list[Any]] = []

    async def refresh_all(presets: list[Any]) -> list[dict[str, Any]]:
        rechecked.append(presets)
        return []

    monkeypatch.setattr(model_discovery, "refresh_all", refresh_all)
    cfg = LMProviderConfig(provider="copilot", model="gpt-5", api_base="copilot://sdk")
    with pytest.raises(HTTPException) as caught:
        await _apply_subscription_gate(
            cfg, _request("gpt-5"), lambda: (status, "why", False, ""), "copilot"
        )
    assert caught.value.status_code == code
    assert caught.value.detail["error"]["error"] == error
    assert caught.value.detail["error"]["message"] == "why"
    assert [[p.id for p in presets] for presets in rechecked] == [["copilot"]]


async def test_binding_copilot_requires_its_readiness_reader() -> None:
    app = _app()
    app.state.agent = None
    app.state.lm_config = None

    def unused() -> tuple[str, str, bool, str]:
        raise AssertionError("not Codex or Claude Code")

    with pytest.raises(RuntimeError, match="needs copilot_readiness"):
        await prepare_bind_configuration(app, _request("gpt-5"), unused, unused)


# --------------------------------------------------------------------------- #
# Sign-in: the row's sign-in is CLIO's GitHub device flow                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A real storage service whose GitHub device flow answers from a mock."""
    approved = [False]
    now = [1000.0]
    grant: dict[str, Any] = {
        "access_token": "ghu_private",
        "refresh_token": "ghr_private",
        "expires_in": 28800,
    }
    monkeypatch.setenv("CLIO_STORAGE_GITHUB_CLIENT_ID", "test-client")
    monkeypatch.setattr(github_oauth.time, "time", lambda: now[0])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if not approved[0]:
            return httpx.Response(200, json={"error": "authorization_pending"})
        return httpx.Response(200, json=grant)

    def client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handle))

    begin, poll = github_oauth.begin, github_oauth.poll
    monkeypatch.setattr(github_oauth, "begin", lambda client_id: begin(client_id, client()))
    monkeypatch.setattr(
        github_oauth,
        "poll",
        lambda flow, client_id, http=None: poll(flow, client_id, http or client()),
    )
    monkeypatch.setattr(adapter, "ensure_copilot_support", lambda: False)
    service = StorageService(tmp_path / "data", tmp_path / "auth.json")
    adapter.install_copilot_token_source(service)
    app = _app(service)
    return SimpleNamespace(app=app, service=service, approved=approved, now=now, grant=grant)


async def _sign_in(github: SimpleNamespace, presets: list[Any] | None = None) -> None:
    started = await adapter.copilot_start(_PRESET, {}, github.app, presets or [_PRESET])
    github.approved[0] = True
    github.now[0] += 10  # past GitHub's polling interval
    state = await adapter.copilot_status(
        _PRESET, started["flow_id"], github.app, presets or [_PRESET]
    )
    assert state["state"] == "complete"


async def test_the_device_flow_signs_clio_in_to_github_and_feeds_copilot(
    github: SimpleNamespace,
) -> None:
    with pytest.raises(CopilotAuthRequiredError):
        auth.github_token()

    started = await adapter.copilot_start(_PRESET, {}, github.app, [_PRESET])
    assert started["device"] == {
        "user_code": "ABCD-EFGH",
        "verification_url": "https://github.com/login/device",
        "interval": 5,
    }
    assert "covers GitHub sources and Copilot" in started["instructions"]
    flow = started["flow_id"]

    assert await adapter.copilot_status(_PRESET, flow, github.app, [_PRESET]) == {
        "state": "pending",
        "reason": "",
    }
    waiting = await adapter.copilot_complete(_PRESET, {"flow_id": flow}, github.app, [_PRESET])
    assert waiting["is_authenticated"] is False

    github.approved[0] = True
    github.now[0] += 10  # past GitHub's polling interval
    assert (await adapter.copilot_status(_PRESET, flow, github.app, [_PRESET]))[
        "state"
    ] == "complete"
    # The same vault entry Settings > Sources reads, through its refresh path.
    assert auth.github_credential() == ("ghu_private", 28800)
    assert github.service.auth.account_connected(
        github.service.principal, github.service.store.clio_id, "github"
    )


async def test_an_existing_github_sign_in_is_reused_not_asked_again(
    github: SimpleNamespace,
) -> None:
    await _sign_in(github)

    again = await adapter.copilot_start(_PRESET, {}, github.app, [_PRESET])
    assert "device" not in again
    assert again["flow_id"].startswith(adapter.READY_FLOW_PREFIX)
    assert "Using CLIO's GitHub sign-in" in again["instructions"]
    done = await adapter.copilot_complete(
        _PRESET, {"flow_id": again["flow_id"]}, github.app, [_PRESET]
    )
    assert done["is_authenticated"] is True

    github.service.auth.sign_out(github.service.principal, github.service.store.clio_id, "github")
    gone = await adapter.copilot_status(_PRESET, again["flow_id"], github.app, [_PRESET])
    assert gone == {
        "state": "failed",
        "reason": "CLIO is no longer signed in to GitHub. Sign in again.",
    }
    with pytest.raises(CopilotAuthRequiredError):
        auth.github_token()


async def test_force_starts_a_new_device_code_even_when_signed_in(
    github: SimpleNamespace,
) -> None:
    await _sign_in(github)
    forced = await adapter.copilot_start(_PRESET, {"force": True}, github.app, [_PRESET])
    assert forced["device"]["user_code"] == "ABCD-EFGH"


async def test_an_unknown_flow_fails_with_the_vaults_words(github: SimpleNamespace) -> None:
    state = await adapter.copilot_status(_PRESET, "no-such-flow", github.app, [_PRESET])
    assert state["state"] == "failed" and state["reason"]
    with pytest.raises(HTTPException) as caught:
        await adapter.copilot_complete(_PRESET, {"flow_id": "no-such-flow"}, github.app, [_PRESET])
    assert caught.value.status_code == 401
    assert caught.value.detail["error"]["error"] == "github_auth_failed"


async def test_a_failed_support_install_is_a_typed_503(
    github: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.providers.dependencies import ProviderDependencyInstallError  # noqa: PLC0415

    def refuse() -> bool:
        raise ProviderDependencyInstallError("uv: no network")

    monkeypatch.setattr(adapter, "ensure_copilot_support", refuse)
    with pytest.raises(HTTPException) as caught:
        await adapter.copilot_start(_PRESET, {}, github.app, [_PRESET])
    assert caught.value.status_code == 503
    assert caught.value.detail["error"]["error"] == "dependency_install_failed"
    assert "uv: no network" in caught.value.detail["error"]["message"]


async def test_sign_in_without_connected_storage_is_a_typed_503() -> None:
    with pytest.raises(HTTPException) as caught:
        await adapter.copilot_start(_PRESET, {}, _app(None), [_PRESET])
    assert caught.value.status_code == 503
    assert caught.value.detail["error"]["error"] == "github_sign_in_unavailable"


async def test_a_non_expiring_grant_reports_a_bounded_lifetime(github: SimpleNamespace) -> None:
    github.grant.pop("expires_in")
    github.grant.pop("refresh_token")
    await _sign_in(github)
    service = github.service
    assert service.auth.account_token(service.principal, service.store.clio_id, "github") == (
        "ghu_private",
        NON_EXPIRING_TOKEN_LIFETIME_S,
    )


def test_a_signed_out_account_has_no_token(github: SimpleNamespace) -> None:
    service = github.service
    with pytest.raises(PermissionError, match="Sign in to github"):
        service.auth.account_token(service.principal, service.store.clio_id, "github")


# --------------------------------------------------------------------------- #
# A dropped GitHub grant drops Copilot                                        #
# --------------------------------------------------------------------------- #
def test_every_listener_runs_when_the_github_clients_are_dropped() -> None:
    access = GitHubAccess()
    calls: list[str] = []
    access.on_client_dropped(lambda: calls.append("one"))
    access.on_client_dropped(lambda: calls.append("two"))
    access.clear()
    access.clear()
    assert calls == ["one", "two", "one", "two"]


def test_a_dropped_grant_releases_sessions_and_marks_the_list_unverified(
    monkeypatch: pytest.MonkeyPatch, github: SimpleNamespace
) -> None:
    released: list[bool] = []
    monkeypatch.setattr(SESSION_POOL, "release_all", lambda: released.append(True))
    _verified()
    github.service.github.clear()
    assert released == [True]
    wire = overlay.overlay_models_wire("copilot", "copilot")
    assert wire is not None
    assert wire["staleness"]["reason"] == "overlay_account_changed"


async def test_a_new_grant_rechecks_every_copilot_row(
    monkeypatch: pytest.MonkeyPatch, github: SimpleNamespace
) -> None:
    invalidated: list[str] = []
    monkeypatch.setattr(adapter, "invalidate_provider", lambda _app, pid: invalidated.append(pid))
    other = SimpleNamespace(id="openrouter", provider="openrouter")
    await _sign_in(github, [_PRESET, other])
    assert invalidated == ["copilot"]


# --------------------------------------------------------------------------- #
# Doctor probe and turn failures                                              #
# --------------------------------------------------------------------------- #
def _probe() -> Any:
    cfg = LMProviderConfig(provider="copilot", model="gpt-5", api_base="copilot://sdk")
    return probe_cli_transport(cfg, source="test", auth_mode="github")


def test_the_probe_names_missing_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "copilot", None)
    row = _probe()
    assert row.state == IntegrationState.UNAVAILABLE
    assert row.details["reason"] == "sdk_package_absent"


def test_the_probe_is_ready_only_with_a_verified_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.copilot import runtime  # noqa: PLC0415

    fake.install(monkeypatch)
    selection = SimpleNamespace(path=fake.RUNTIME, reason="staged")
    monkeypatch.setattr(runtime, "copilot_client", lambda: selection)
    assert _probe().details["reason"] == "auth_check_required"
    _verified()
    row = _probe()
    assert row.state == IntegrationState.READY
    assert row.details["runtime_path"] == fake.RUNTIME
    overlay.overlay_path().write_text("{not json", encoding="utf-8")
    broken = _probe()
    assert broken.state == IntegrationState.UNAVAILABLE
    assert broken.details["reason"] == "overlay_malformed"


def test_the_probe_names_a_missing_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.copilot import runtime  # noqa: PLC0415

    fake.install(monkeypatch)
    selection = SimpleNamespace(path=None, reason="copilot_no_runtime")
    monkeypatch.setattr(runtime, "copilot_client", lambda: selection)
    row = _probe()
    assert row.state == IntegrationState.UNAVAILABLE
    assert row.details["reason"] == "copilot_no_runtime"


def _state() -> Any:
    """The slice of ``TurnState`` ``forward_error_info`` reads: the turn's route."""
    return SimpleNamespace(
        user_msg=SimpleNamespace(
            metadata={"effective_model": {"provider_id": "copilot", "model_id": "gpt-5"}}
        )
    )


def test_a_signed_out_turn_is_the_copilot_sign_in_message() -> None:
    exc = RuntimeError(
        f"litellm.APIConnectionError: {CopilotAuthRequiredError(COPILOT_SIGNED_OUT_MESSAGE)}"
    )
    assert provider_auth_failure(exc, provider_id="copilot") == COPILOT_SIGNED_OUT_MESSAGE
    assert provider_auth_failure(exc, provider_id="openrouter") is None
    info = forward_error_info(_state(), exc, "")
    assert info.message == COPILOT_SIGNED_OUT_MESSAGE
    assert info.details["reason"] == "provider_auth_required"


def test_a_missing_runtime_turn_is_the_install_message() -> None:
    from clio_agent.gact.stream_failures import cli_provider_stream_failure  # noqa: PLC0415

    exc = ModuleNotFoundError("No module named 'copilot'")
    assert cli_provider_stream_failure(exc, provider_id="copilot") == (
        COPILOT_INSTALL_FAILED_MESSAGE
    )
