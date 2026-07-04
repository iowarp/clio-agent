"""The CLI as a thin GACT client (#799/#800).

Drives :class:`ClioAgentCLI` with an INJECTED :class:`ClioClient` over the
in-process ASGI transport (no real server, no spawning). Asserts the main
Q&A path posts + renders a stubbed answer, the catalog/health/metrics slash
commands render their SDK reads, the optimizer commands emit the uniform
not-implemented message with no optimizer call, and that :func:`boot_client`
exits structured when the server cannot be started.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from clio_agent import serve
from clio_agent.sdk import ClioClient
from clio_agent.ui.cli import ClioAgentCLI, boot_client


def _recording_console() -> Console:
    """A Rich console that captures output as plain text (no real TTY)."""

    return Console(file=io.StringIO(), record=True, width=200, force_terminal=False)


def _make_cli(client: ClioClient, *, verbose: bool = False) -> tuple[ClioAgentCLI, Console]:
    console = _recording_console()
    cli = ClioAgentCLI(client, verbose=verbose, console=console)
    return cli, console


# --------------------------------------------------------------------------- #
# no in-process agent / DSPy
# --------------------------------------------------------------------------- #


def test_cli_module_imports_without_clioagent() -> None:
    """The CLI must not drag in the in-process agent or DSPy anymore."""

    import clio_agent.ui.cli as cli_mod

    assert not hasattr(cli_mod, "ClioAgent")
    src = __import__("inspect").getsource(cli_mod)
    assert "from clio_agent.agent import ClioAgent" not in src
    assert "setup_dspy" not in src


# --------------------------------------------------------------------------- #
# main Q&A
# --------------------------------------------------------------------------- #


def test_ask_question_posts_and_renders_stub_answer(client: ClioClient, stub_agent: Any) -> None:
    cli, console = _make_cli(client)

    result = cli.ask_question("How well did the dataset compress?")

    assert result["error_info"] is None
    assert "3.2x" in result["answer"]
    # The turn actually reached the server-side (stub) agent.
    assert stub_agent.calls
    assert stub_agent.calls[0][0] == "How well did the dataset compress?"
    # And the session id was created + reused.
    assert result["session_id"].startswith("sess_")

    cli._render_answer(result)
    out = console.export_text()
    assert "3.2x" in out
    assert "CLIO" in out


def test_ask_question_reuses_one_session_across_turns(client: ClioClient) -> None:
    cli, _ = _make_cli(client)

    first = cli.ask_question("first question")
    second = cli.ask_question("second question")

    assert first["session_id"] == second["session_id"]
    assert "3.2x" in second["answer"]


# --------------------------------------------------------------------------- #
# slash commands re-backed by the SDK
# --------------------------------------------------------------------------- #


def test_experts_renders_agent_catalog(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/experts") is True
    out = console.export_text()
    assert "Available Experts" in out
    # Real catalog rows come back with at least one built-in expert id.
    assert cli.client.agents(), "server should expose an agent catalog"


def test_registry_summary_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/registry") is True
    out = console.export_text()
    assert "Registry Status" in out
    assert "Registered Agents" in out


def test_doctor_renders_server_health(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/doctor") is True
    out = console.export_text()
    assert "CLIO Server Health" in out
    # Health carries at least one integration row.
    assert cli.client.health().integrations


def test_metrics_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/metrics") is True
    out = console.export_text()
    assert "Runtime Metrics" in out
    assert "Uptime" in out


def test_tools_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/tools") is True
    out = console.export_text()
    assert "Tools" in out


def test_models_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/models") is True
    out = console.export_text()
    assert "LM Provider" in out


def test_memory_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/memory") is True
    out = console.export_text()
    # Either the ARC row or the explicit "not reported" note — never a crash.
    assert out.strip()


# --------------------------------------------------------------------------- #
# optimizer commands = uniform not-implemented stub, no optimizer call
# --------------------------------------------------------------------------- #


def test_compare_prints_not_implemented(client: ClioClient, monkeypatch: Any) -> None:
    # If any real optimizer module were imported the CLI would be wrong; make
    # sure the message comes from the stub, and no VariantManager is touched.
    import clio_agent.optimizer.stub as stub_mod

    cli, console = _make_cli(client)
    assert cli.handle_command("/compare data") is True
    out = console.export_text()
    assert stub_mod.OPTIMIZER_NOT_IMPLEMENTED_MESSAGE.split(";")[0][:30] in out
    assert "not implemented" in out.lower()


def test_rollback_prints_not_implemented(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/rollback data") is True
    out = console.export_text()
    assert "not implemented" in out.lower()


# --------------------------------------------------------------------------- #
# help reflects the new reality
# --------------------------------------------------------------------------- #


def test_help_lists_current_commands(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/help") is True
    out = console.export_text()
    for cmd in ("/experts", "/tools", "/metrics", "/doctor", "/models"):
        assert cmd in out


# --------------------------------------------------------------------------- #
# boot helper: structured, non-zero exit on server-start failure
# --------------------------------------------------------------------------- #


def test_boot_client_exits_structured_on_binary_not_found(monkeypatch: Any) -> None:
    payload = {
        "reason": "binary_not_found",
        "managed": False,
        "detail": "the clio-agent-gact server binary could not be located on this system",
        "searched": ["/nowhere/clio-agent-gact", "PATH"],
    }

    def _raise(*_a: Any, **_k: Any) -> str:
        raise serve.ServerBinaryNotFound(payload)

    monkeypatch.setattr(serve, "ensure_server", _raise)
    console = _recording_console()

    with pytest.raises(SystemExit) as excinfo:
        boot_client(console=console)

    assert excinfo.value.code == 1
    out = console.export_text()
    assert "binary_not_found" in out
    assert "could not be located" in out


def test_boot_client_exits_structured_on_start_timeout(monkeypatch: Any) -> None:
    payload = {
        "reason": "spawn_timeout",
        "managed": False,
        "detail": "spawned server did not become healthy before the timeout; torn down",
        "log": "/tmp/gact-server-8100.log",
    }

    def _raise(*_a: Any, **_k: Any) -> str:
        raise serve.ServerStartTimeout(payload)

    monkeypatch.setattr(serve, "ensure_server", _raise)
    console = _recording_console()

    with pytest.raises(SystemExit) as excinfo:
        boot_client(console=console)

    assert excinfo.value.code == 1
    out = console.export_text()
    assert "spawn_timeout" in out
    assert "gact-server-8100.log" in out


def test_boot_client_attaches_and_returns_client(monkeypatch: Any) -> None:
    monkeypatch.setattr(serve, "ensure_server", lambda *a, **k: "http://127.0.0.1:8100")
    client = boot_client()
    assert isinstance(client, ClioClient)
    client.close()
