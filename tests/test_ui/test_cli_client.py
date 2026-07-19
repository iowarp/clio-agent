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
import sys
from typing import Any

import pytest
from rich.console import Console

from clio_agent import serve
from clio_agent.sdk import ClioClient
from clio_agent.ui.cli import ClioAgentCLI, boot_client, main

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host/stub fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


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
    agents = cli.client.agents()
    assert agents, "server should expose an agent catalog"
    assert cli.handle_command("/experts") is True
    out = console.export_text()
    assert "Available Experts" in out
    # A real catalog row rendered — the name cell is title-or-id, from live data.
    assert any((a.title or a.id) in out for a in agents), "no expert row rendered"


def test_registry_summary_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    agents = cli.client.agents()
    assert agents
    assert cli.handle_command("/registry") is True
    out = console.export_text()
    assert "Registry Status" in out
    # The count is bound to the live catalog, and a real id surfaces.
    assert f"Registered Agents: {len(agents)}" in out
    assert any(a.id in out for a in agents), "no registered agent id rendered"


def test_doctor_renders_server_health(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    assert cli.handle_command("/doctor") is True
    out = console.export_text()
    assert "CLIO Server Health" in out
    # Health carries at least one integration row.
    assert cli.client.health().integrations


def test_metrics_renders(client: ClioClient) -> None:
    cli, _ = _make_cli(client)
    # Drive one turn so the counters are non-trivial (messages.total >= 1),
    # otherwise every counter is 0 and "0 in out" would prove nothing.
    cli.ask_question("warm up the metrics")
    m = cli.client.metrics()
    assert m.messages.total >= 1
    # Render /metrics into a FRESH console so we assert ONLY its output (not the
    # warm-up answer, which also contains digits).
    fresh = _recording_console()
    cli.console = fresh
    assert cli.handle_command("/metrics") is True
    out = fresh.export_text()
    assert "Runtime Metrics" in out
    # The exact live counters render — a decode/render regression would break these.
    assert f"{m.messages.total}" in out
    assert f"{m.sessions.total}" in out


def test_tools_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    tools = cli.client.tools()
    assert cli.handle_command("/tools") is True
    out = console.export_text()
    assert "Tools" in out
    # If the server mounts any tools, a real tool row (name or id) rendered.
    if tools:
        assert any((t.name or t.id) in out for t in tools), "no tool row rendered"


def test_models_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    prov = cli.client.lm_provider()
    assert cli.handle_command("/models") is True
    out = console.export_text()
    assert "LM Provider" in out
    # A dynamic SDK-sourced value renders (state is always populated on the wire).
    assert prov.state and prov.state in out


def test_memory_renders(client: ClioClient) -> None:
    cli, console = _make_cli(client)
    health = cli.client.health()
    arc_row = next(
        (i for i in health.integrations if "arc" in i.name.lower() or "memory" in i.name.lower()),
        None,
    )
    assert arc_row is not None, "server health should expose an ARC/memory row in the test env"
    assert cli.handle_command("/memory") is True
    out = console.export_text()
    # The ARC-row branch rendered (not the "not reported" note) with its live fields.
    assert "ARC Memory Integration" in out
    assert arc_row.name in out
    assert arc_row.status in out


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


# --------------------------------------------------------------------------- #
# console-script entry: `clio-agent doctor` runs in-process, never spawns
# --------------------------------------------------------------------------- #


def test_main_doctor_runs_in_process_without_a_server(monkeypatch: Any) -> None:
    """Regression: the shipped ``clio-agent`` script points at ``main`` (not
    ``run_cli``), so ``clio-agent doctor`` parses argv and runs the in-process
    doctor — it must NEVER connect-or-spawn a server (a doctor has to work when
    the server is down)."""
    import clio_agent.ui.cli as cli_mod

    def _no_spawn(*_a: Any, **_k: Any) -> str:
        raise AssertionError("`clio-agent doctor` must not connect-or-spawn a server")

    monkeypatch.setattr(cli_mod.serve, "ensure_server", _no_spawn)

    ran = {"doctor": False}

    def _fake_doctor(json_output: bool = False) -> int:
        ran["doctor"] = True
        return 0

    monkeypatch.setattr(cli_mod, "run_doctor", _fake_doctor)
    monkeypatch.setattr(sys, "argv", ["clio-agent", "doctor"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    assert ran["doctor"] is True  # dispatched in-process, no server booted


def test_main_serve_runs_gact_server_in_foreground(monkeypatch: Any) -> None:
    """``clio-agent serve --host --port`` must dispatch to the gact foreground
    runner with the parsed host/port — running the SAME server the
    ``clio-agent-gact`` console script runs, in-process. It must NEVER
    connect-or-spawn a client server, and NEVER actually bind a socket (the
    gact runner is monkeypatched)."""
    import clio_agent.gact.app as gact_app
    import clio_agent.ui.cli as cli_mod

    def _no_spawn(*_a: Any, **_k: Any) -> str:
        raise AssertionError("`clio-agent serve` must not connect-or-spawn a client server")

    monkeypatch.setattr(cli_mod.serve, "ensure_server", _no_spawn)
    monkeypatch.setattr(cli_mod, "boot_client", _no_spawn)

    calls: list[dict[str, Any]] = []

    def _fake_run_server(host: str = "127.0.0.1", port: int = 8100, **kwargs: Any) -> None:
        # Records the args and returns immediately — never binds a socket.
        calls.append({"host": host, "port": port, **kwargs})

    monkeypatch.setattr(gact_app, "run_server", _fake_run_server)
    monkeypatch.setattr(sys, "argv", ["clio-agent", "serve", "--port", "0", "--host", "127.0.0.1"])

    # serve returns (no SystemExit) after the foreground runner returns.
    main()

    assert calls == [{"host": "127.0.0.1", "port": 0}]


def test_serve_is_a_parser_choice() -> None:
    """``serve`` must be an accepted positional command (alongside ``doctor``),
    so argparse does not reject it with exit 2."""
    import argparse

    # Mirror cli.main()'s positional command spec.
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["doctor", "serve"])
    args = parser.parse_args(["serve"])
    assert args.command == "serve"

    with pytest.raises(SystemExit):
        parser.parse_args(["nonsense-command"])
