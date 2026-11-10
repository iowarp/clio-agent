"""The Copilot SDK is a model transport and nothing else (:mod:`clio_agent.providers.copilot.options`).

Pins every isolation flag on the client and on each session: CLIO's gateway stays the
only tool path, the runtime never reads ambient state (``~/.copilot``, repository
instructions, git, a stored login, ambient GitHub tokens) and never acts on its own
(autopilot, compaction, memory, permission grants).

Each pin carries a SABOTAGE note: flip the flag in ``options.py`` and the test goes red.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.copilot import options, runtime
from tests import _fake_copilot_sdk as fake


def _session(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": "gpt-5",
        "system_prompt": "be brief",
        "working_directory": "/w",
        "on_event": lambda _event: None,
        "github_token_provider": lambda _args: None,
    }
    kwargs.update(overrides)
    return options.session_kwargs(**kwargs)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    fake.reset()
    yield
    fake.reset()


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> fake.FakeSDK:
    return fake.install(monkeypatch)


def test_client_never_uses_the_runtimes_stored_login(sdk: fake.FakeSDK) -> None:
    """SABOTAGE: ``use_logged_in_user: True`` -> red (the machine's ``copilot login`` wins)."""
    kwargs = options.client_kwargs(github_token="ghu_x", runtime_path="/r", working_directory="/w")
    assert kwargs["use_logged_in_user"] is False
    assert kwargs["github_token"] == "ghu_x"


def test_client_runs_empty_mode_under_a_clio_owned_home(
    sdk: fake.FakeSDK, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop ``mode: empty`` or ``base_directory`` -> red (``~/.copilot`` is read)."""
    monkeypatch.setattr(options.paths, "user_data_dir", lambda: tmp_path)
    kwargs = options.client_kwargs(github_token="t", runtime_path="/r", working_directory="/w")
    assert kwargs["mode"] == "empty"
    assert kwargs["base_directory"] == str(tmp_path / "copilot" / "home")
    assert (tmp_path / "copilot" / "home").is_dir()
    assert kwargs["working_directory"] == "/w"
    assert kwargs["log_level"] == "error"


def test_client_spawns_the_selected_runtime_over_stdio(sdk: fake.FakeSDK) -> None:
    kwargs = options.client_kwargs(github_token="t", runtime_path="/r/cp", working_directory="/w")
    assert (kwargs["connection"].kind, kwargs["connection"].path) == ("stdio", "/r/cp")


def test_client_env_scrubs_ambient_github_credentials(
    sdk: fake.FakeSDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop a name from ``SCRUBBED_ENV`` -> red (an ambient token signs in)."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_CLI_PATH"):
        monkeypatch.setenv(name, "ambient")
    monkeypatch.setenv("CLIO_UNRELATED", "kept")
    env = options.client_kwargs(github_token="t", runtime_path="/r", working_directory="/w")["env"]
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_CLI_PATH"} & set(env)
    assert env["CLIO_UNRELATED"] == "kept"
    assert env["COPILOT_SKIP_CLI_DOWNLOAD"] == "1"  # a turn never downloads a runtime
    assert env["COPILOT_GITHUB_TELEMETRY_DISABLED"] == "true"
    assert runtime.SCRUBBED_ENV == (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_CLI_PATH",
    )


def test_session_has_no_tools_of_its_own() -> None:
    """SABOTAGE: drop ``available_tools: []`` -> red (the runtime's built-ins run)."""
    kwargs = _session()
    assert kwargs["available_tools"] == []
    assert kwargs["tools"] == []
    assert kwargs["mcp_servers"] == {}


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("skip_custom_instructions", True),
        ("enable_config_discovery", False),
        ("enable_on_demand_instruction_discovery", False),
        ("enable_file_hooks", False),
        ("enable_host_git_operations", False),
        ("enable_session_store", False),
        ("enable_skills", False),
        ("skip_embedding_retrieval", True),
        ("enable_session_telemetry", False),
        ("streaming", True),
    ],
)
def test_session_reads_no_ambient_state(flag: str, value: bool) -> None:
    """SABOTAGE: flip any one of these flags -> its row goes red."""
    assert _session()[flag] is value


def test_session_never_acts_on_its_own() -> None:
    """SABOTAGE: enable autopilot, infinite sessions or memory -> red."""
    kwargs = _session()
    assert kwargs["autopilot"] == {"continuation_enabled": False}
    assert kwargs["infinite_sessions"] == {"enabled": False}
    assert kwargs["memory"] == {"enabled": False}


def test_session_replaces_the_system_prompt() -> None:
    """SABOTAGE: ``mode: append`` -> red (Copilot's own agent prompt rides along)."""
    assert _session()["system_message"] == {"mode": "replace", "content": "be brief"}


def test_session_passes_model_effort_and_callbacks() -> None:
    provider = object()
    kwargs = _session(github_token_provider=provider, reasoning_effort="high")
    assert kwargs["model"] == "gpt-5"
    assert kwargs["working_directory"] == "/w"
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["github_token_provider"] is provider
    assert kwargs["on_permission_request"] is options.deny_permission
    assert "reasoning_effort" not in _session()  # unset: the model's own default


def test_every_permission_request_is_refused(sdk: fake.FakeSDK) -> None:
    """SABOTAGE: approve in ``deny_permission`` -> red."""
    decision = options.deny_permission(object(), object())
    assert type(decision).__name__ == "PermissionDecisionReject"
    assert decision.feedback == options.PERMISSION_DENIED_FEEDBACK


async def test_a_live_session_is_created_with_exactly_these_options(sdk: fake.FakeSDK) -> None:
    """The pooled transport passes these options through unchanged."""
    await fake.drive(fake.request(system="sys"), reasoning_effort="low")
    [client] = sdk.clients
    assert client.kwargs["use_logged_in_user"] is False
    assert client.kwargs["github_token"] == fake.TOKEN
    assert client.kwargs["connection"].path == fake.RUNTIME
    [session] = client.sessions
    assert session.kwargs["available_tools"] == []
    assert session.kwargs["system_message"]["mode"] == "replace"
    assert session.kwargs["reasoning_effort"] == "low"
    assert session.kwargs["memory"] == {"enabled": False}
    reply = await session.kwargs["github_token_provider"](None)
    assert reply == {"kind": "token", "accessToken": fake.TOKEN, "expiresIn": 3600}
    await asyncio.sleep(0)
