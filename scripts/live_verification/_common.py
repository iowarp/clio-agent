"""Shared harness for the MCP-client-unification live-verification package (#1286).

Reuses the house live-gate pattern (``scripts/live_gate_observe_1000.py`` /
``scripts/live_gate_1031.py``): boot a gact ``run_server`` subprocess on a
private port, a thin ``requests`` client wrapper, a health poll, and a verdict
JSON written to ``out/``. Adds two things the campaign's own wait-semantics
rule (#1274/#1275, C1-S2) asks of every NEW script: an expanding-backoff
waiter that names what it is waiting on (never a silent flat-interval poll to
a bare deadline), and a workspace-scoped ``.clio/mcp.yaml`` writer paired with
booting the server subprocess at that SAME directory.

CONSTRAINT DISCOVERED while building this package: ``GET /v1/mcp/handshake``
correctly threads a workspace's ``root_path`` as the ``cwd`` used to discover
its ``.clio/mcp.yaml`` (``gact/routes/mcp_specs.py::declared_mcp_specs`` ->
``load_mcp_servers(cwd=...)``), but the ACTUAL per-turn tool-gateway build
(``agent.py::_build_tool_gateway``) calls ``load_mcp_servers(plugin_servers=...)``
with NO ``cwd=`` at all, so it silently defaults to ``Path.cwd()`` -- the gact
SERVER PROCESS's OS working directory, not the HTTP workspace's ``root_path``.
The existing ``live_gate_1031.py`` P3/composed gates work around this by
writing ``.clio/mcp.yaml`` at ``<repo>/.clio/mcp.yaml`` and always booting the
subprocess with ``cwd=str(REPO)`` -- process cwd and mcp.yaml location always
coincide there. This module generalizes that workaround: :func:`boot_server`
takes an explicit ``cwd`` and :func:`write_mcp_yaml` writes into that SAME
directory, so a leg's declared server reaches BOTH the handshake probe and the
real tool executor. A live leg that boots with one cwd and writes mcp.yaml
into a different one would see the handshake pass (false-green) while the
real turn's tool call fails to find the namespace -- exactly the trap #1286
leg (ii) warns about. ``write_mcp_yaml``/``quoted_command`` remain here as
general infra for workspace ``mcp.yaml``, but legs B/C (C1-S6, #1301) no longer
call ``write_mcp_yaml`` themselves -- see the next paragraph.

TESTING AGENTS (C1-S6, #1301): legs B/C and the task qualification legs drive a
purpose-built testing agent instead of a bare session + workspace ``mcp.yaml``.
Each one under ``agents/<name>/`` is a small native marketplace (Agent Plugins
1.0.0 plus a Python agent class; ``docs/PLUGINS_AND_FLOWS.md``): one plugin with
the agent's prompt fragment and, when the leg needs one, an ``mcp.json`` the
materializer writes at run time; the agent selects that plugin plus shared
plugins from the default marketplace (``web``, ``workspace``, ``shell``,
``delegation``). A plugin's MCP servers reach the real per-turn tool gateway
through the session's pinned resolution, the same path every marketplace agent
uses. See :func:`materialize_testing_marketplace`,
:func:`install_testing_agent`, :func:`use_agent` and
:func:`resolved_agent_tools` below, and ``RUNBOOK.md``'s "Testing-agent
mechanism" section.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

OUT_ROOT = REPO / "out" / "live-verification"

#: Default gact ``run_server`` module invocation, matching every existing
#: live_gate script byte-for-byte (``clio_agent.gact.app.run_server``).
_RUN_SERVER_CODE = (
    "from clio_agent.gact.app import run_server; run_server(host='127.0.0.1', port={port})"
)


# --------------------------------------------------------------------------- #
# Expanding-backoff wait (C1-S2 house rule: no flat-interval poll to a bare
# deadline; every wait names what it waits on and surfaces attempt/elapsed).
# --------------------------------------------------------------------------- #
def expanding_wait(
    check: Callable[[], Any],
    *,
    what: str,
    initial: float = 1.0,
    factor: float = 1.6,
    cap: float = 15.0,
    max_elapsed: float = 180.0,
    quiet: bool = False,
) -> Any:
    """Poll ``check()`` until truthy, backing off, naming what it waits on.

    Per-step check + expanding backoff (never a flat ``time.sleep(N)`` loop to
    a silent deadline) -- the house wait rule pinned by C1-S2 (#1274/#1275)
    applied to this verification tooling itself. Returns ``check()``'s truthy
    value, or ``None`` if ``max_elapsed`` passed with no truthy result (a
    NAMED give-up, printed, never a bare timeout exception mid-composite-work).
    """

    start = time.monotonic()
    delay = initial
    attempt = 0
    while True:
        attempt += 1
        result = check()
        if result:
            return result
        elapsed = time.monotonic() - start
        if elapsed >= max_elapsed:
            if not quiet:
                print(
                    f"[wait] giving up on {what!r} after {attempt} attempts / "
                    f"{elapsed:.0f}s (max_elapsed={max_elapsed:g}s)",
                    flush=True,
                )
            return None
        if not quiet:
            print(
                f"[wait] {what}: attempt {attempt}, elapsed {elapsed:.0f}s, "
                f"retrying in {delay:.1f}s",
                flush=True,
            )
        time.sleep(min(delay, max(0.0, max_elapsed - elapsed)))
        delay = min(delay * factor, cap)


# --------------------------------------------------------------------------- #
# HTTP client (matches live_gate_1031.py's ``_client``)
# --------------------------------------------------------------------------- #
def client(base: str) -> Callable[..., Any]:
    """Return a ``call(method, path, body=, params=, ok=, raw=)`` closure."""

    import requests

    def call(
        method: str,
        path: str,
        body: Any = None,
        params: Any = None,
        ok: tuple[int, ...] = (200, 201),
        raw: bool = False,
    ) -> Any:
        r = requests.request(method, f"{base}{path}", json=body, params=params, timeout=300)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        if raw:
            return r
        return r.json() if r.content else {}

    return call


# --------------------------------------------------------------------------- #
# Port + server lifecycle
# --------------------------------------------------------------------------- #
def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Best-effort check that nothing is currently listening on ``port``."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) != 0


def boot_server(
    port: int,
    *,
    cwd: Path,
    sse_log: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Boot a gact ``run_server`` subprocess with its OS cwd pinned to ``cwd``.

    ``cwd`` is deliberately load-bearing (see module docstring): whichever
    directory the server PROCESS runs in is where ``load_mcp_servers()``'s
    default ``Path.cwd()`` resolution finds ``.clio/mcp.yaml`` for the REAL
    tool executor, not just the handshake probe. Callers must write their
    workspace's ``.clio/mcp.yaml`` into this SAME directory (see
    :func:`write_mcp_yaml`) and should also set the HTTP workspace's
    ``root_path`` to it, so handshake / turn / stdio-spawn cwd all coincide.
    """

    cwd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("CLIO_LM_MODEL", None)
    env.pop("CLIO_LM_THINKING_LEVEL", None)
    if sse_log is not None:
        sse_log.parent.mkdir(parents=True, exist_ok=True)
        env["CLIO_STREAM_AUDIT_LOG"] = str(sse_log.resolve())
    # Plugin stdio servers name a bare command; ``python`` must be this interpreter.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-c", _RUN_SERVER_CODE.format(port=port)],
        env=env,
        cwd=str(cwd),
    )


def terminate_server(proc: subprocess.Popen, *, timeout: float = 20.0) -> None:
    """Kill ``proc`` and its whole descendant tree (MCP stdio children included).

    Delegates to :func:`clio_agent.serve._terminate_tree` -- the owner-module
    reaper the real ``clio`` CLI and ``tests/test_real_cases/conftest.py`` both
    use, cross-platform (POSIX process-group kill / Windows psutil descendant
    walk). ``trusted=True``: this caller holds the live ``Popen`` for its whole
    lifetime, so there is no PID-reuse ambiguity to guard against.
    """

    if proc.poll() is not None:
        return
    try:
        from clio_agent.serve import _terminate_tree

        _terminate_tree(proc.pid, record_create_time=None, trusted=True)
        proc.wait(timeout=timeout)
    except Exception:  # noqa: BLE001 - best-effort teardown must never raise
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            proc.kill()


def wait_health(call: Callable[..., Any], *, max_elapsed: float = 240.0) -> bool:
    """Wait for ``GET /v1/health`` to answer (200 up, or 503 up-but-no-LM)."""

    def _check() -> bool:
        try:
            call("GET", "/v1/health", ok=(200, 503))
            return True
        except Exception:  # noqa: BLE001 - transient connect-refused during boot
            return False

    return bool(
        expanding_wait(_check, what="gact server health (/v1/health)", max_elapsed=max_elapsed)
    )


# --------------------------------------------------------------------------- #
# Provider / policy / workspace / session helpers
# --------------------------------------------------------------------------- #
def bind_provider(
    call: Callable[..., Any],
    *,
    provider: str = "claude_code",
    model: str = "sonnet",
    max_elapsed: float = 180.0,
) -> dict[str, Any]:
    """Bind the LM provider and wait for it to report ``ready``.

    THE ONLY place in this package that turns on a model. Every ``--plumbing-
    only`` leg run stops before calling this.
    """

    body = {"provider": provider, "api_base": "", "model": model}
    if provider == "codex":
        # As the UI's "Check the provider": validate the sign-in and load the model
        # list, so the bind does not race the startup check (401 "being checked").
        call("POST", "/v1/providers/models/refresh", {"provider_id": "codex"})
    call("PUT", "/v1/providers/lm", body)

    def _check() -> dict[str, Any] | None:
        info = call("GET", "/v1/providers/lm/wait", params={"timeout": 20}, ok=(200, 503))
        state = str(info.get("state") or "")
        if state == "ready":
            return info
        if state == "error":
            raise RuntimeError(f"LM provider failed to configure: {info.get('error') or info}")
        return None

    result = expanding_wait(
        _check, what=f"LM provider ready ({provider}/{model})", max_elapsed=max_elapsed
    )
    if result is None:
        raise TimeoutError(f"LM provider {provider}/{model} not ready in {max_elapsed:g}s")
    return result


def allow_all(call: Callable[..., Any]) -> None:
    """PUT a workspace-scope allow-all policy -- MUST run before the first turn."""

    call(
        "PUT",
        "/v1/policies",
        {"policies": [{"scope": "workspace", "action": "allow", "tool_name_pattern": "*"}]},
    )


def create_workspace(call: Callable[..., Any], name: str, root_path: Path) -> str:
    root_path.mkdir(parents=True, exist_ok=True)
    ws = call("POST", "/v1/workspaces", {"name": name, "root_path": str(root_path)})
    return str(ws.get("id") or ws.get("workspace_id") or "")


def create_session(call: Callable[..., Any], workspace_id: str, title: str, **extra: Any) -> str:
    body = {"title": title, "workspace_id": workspace_id, **extra}
    return str(call("POST", "/v1/sessions", body)["id"])


def post_message(call: Callable[..., Any], sid: str, text: str) -> dict[str, Any]:
    return call("POST", f"/v1/sessions/{sid}/messages", {"text": text}, ok=(200, 201, 202))


def wait_turn(call: Callable[..., Any], wsid: str, sid: str, *, max_elapsed: float = 1800.0) -> str:
    """Wait for a session to reach a terminal turn status, expanding backoff."""

    def _check() -> str | None:
        rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
        row = next((r for r in rows if r.get("id") == sid), {})
        status = str(row.get("status") or "?")
        return (
            status
            if status in ("idle", "completed", "error", "waiting_user", "cancelled", "stopped")
            else None
        )

    result = expanding_wait(
        _check, what=f"session {sid} turn to reach a terminal status", max_elapsed=max_elapsed
    )
    return result or "timed_out"


def session_messages(call: Callable[..., Any], sid: str) -> list[dict[str, Any]]:
    """Return retained messages chronologically; the product API pages newest first."""
    rows = call("GET", f"/v1/sessions/{sid}/messages").get("messages", [])
    return sorted(rows, key=lambda row: (row.get("created_at", ""), row.get("id", "")))


def pending_questions(call: Callable[..., Any], sid: str) -> list[dict[str, Any]]:
    return call("GET", f"/v1/sessions/{sid}/questions", params={"status": "pending"}).get(
        "questions", []
    )


def wait_pending_question(
    call: Callable[..., Any], sid: str, *, source: str = "", max_elapsed: float = 180.0
) -> dict[str, Any] | None:
    """Wait for a pending ``UserQuestion`` (optionally filtered by ``source``)."""

    def _check() -> dict[str, Any] | None:
        rows = pending_questions(call, sid)
        if source:
            rows = [q for q in rows if q.get("source") == source]
        return rows[0] if rows else None

    what = f"pending user question on session {sid}"
    if source:
        what += f" (source={source!r})"
    return expanding_wait(_check, what=what, max_elapsed=max_elapsed)


def answer_question(
    call: Callable[..., Any], sid: str, question_id: str, answer: str
) -> dict[str, Any]:
    return call(
        "POST",
        f"/v1/sessions/{sid}/questions/{question_id}/answer",
        {"answer": answer},
    )


# --------------------------------------------------------------------------- #
# Tool-call evidence (matches clio_sut.py's ``tools_called`` extraction: the
# per-call "ok" boolean is the reliable success signal, see tool_observer.py).
# --------------------------------------------------------------------------- #
def tool_calls_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        meta = message.get("metadata") or {}
        for tool in meta.get("tools_called") or []:
            if isinstance(tool, dict):
                calls.append(tool)
    return calls


def tool_call_ok(call_row: dict[str, Any]) -> bool:
    if call_row.get("error"):
        return False
    return call_row.get("ok") is not False


def find_tool_calls(messages: list[dict[str, Any]], name_suffix: str) -> list[dict[str, Any]]:
    """Every recorded tool call whose name ends with ``name_suffix`` (namespaced
    tools are ``<namespace>_<tool>``; pass e.g. ``"_task_echo"`` or the bare
    unnamespaced name for a builtin)."""

    return [
        call
        for call in tool_calls_from_messages(messages)
        if str(call.get("name") or "").endswith(name_suffix)
    ]


# --------------------------------------------------------------------------- #
# mcp.yaml declaration
# --------------------------------------------------------------------------- #
def write_mcp_yaml(workspace_dir: Path, servers: dict[str, str]) -> Path:
    """Write ``<workspace_dir>/.clio/mcp.yaml`` declaring ``servers`` (name ->
    command string).

    Serialized through ``yaml.safe_dump`` (never hand-rolled string
    concatenation): a value built by :func:`quoted_command` embeds literal
    double-quote characters (for ``shlex.split``'s benefit downstream -- see
    that function's docstring), and naively writing ``f"{name}: {command}"``
    produces INVALID YAML for a value with more than one quoted segment (e.g.
    ``v2ex: "<python>" "<path>"`` -- a plain scalar cannot contain multiple
    quoted fragments; ``yaml.safe_load`` raises ``while parsing a block
    mapping``, verified live). ``yaml.safe_dump`` picks the correct scalar
    style (single-quoted, escaping only embedded ``'``) so backslashes and
    embedded ``"`` survive the round trip through ``yaml.safe_load`` exactly
    as :func:`quoted_command` built them.
    """

    import yaml

    clio_dir = workspace_dir / ".clio"
    clio_dir.mkdir(parents=True, exist_ok=True)
    path = clio_dir / "mcp.yaml"
    path.write_text(
        yaml.safe_dump({"mcp_servers": dict(servers)}, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return path


def quoted_command(*tokens: str) -> str:
    """Build an mcp.yaml command-string value that survives ``shlex.split``.

    CONSTRAINT DISCOVERED (verified against ``tools/mcp_config.py::
    _spec_from_string``'s exact call, ``shlex.split(text)`` == POSIX mode):
    an UNQUOTED Windows absolute path fed through POSIX-mode ``shlex.split``
    has its backslashes eaten as escape characters (``D:\\Libraries\\...``
    becomes ``DLibraries...``), silently mangling the path into garbage no
    launcher can resolve. Double-quoting each token is unaffected by this --
    ``shlex.split('"C:\\a\\b.exe" "C:\\c\\d.py"')`` returns the two tokens with
    backslashes intact, on both Windows and POSIX. Always quote every token
    that may carry a filesystem path.
    """

    return " ".join(f'"{token}"' for token in tokens)


# --------------------------------------------------------------------------- #
# Testing agents: small native marketplaces under ``agents/<name>/`` whose
# plugin MCP servers reach the REAL per-turn tool gateway through the session's
# pinned plugin resolution (unlike the bare-session builtin main of #1301).
# --------------------------------------------------------------------------- #
#: Installed marketplace names are ``lv-<materialized folder>``.
TESTING_MARKETPLACE_PREFIX = "lv-"
_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def materialize_testing_marketplace(
    template_dir: Path,
    ws_dir: Path,
    mcp_servers: dict[str, list[str]],
    *,
    name: str = "",
    agent_id: str = "",
) -> Path:
    """Copy a testing marketplace template into ``ws_dir`` and write its plugin's
    ``mcp.json`` from ``mcp_servers`` (server name -> argv).

    A server's argv may carry a per-run resolved path (the v2ex exerciser's
    script path -- see ``leg_c_synthetic_session.py``) that must never be a
    hardcoded drive path committed to source, so ``mcp.json`` is written here
    rather than committed. Agent Plugins 1.0.0 requires a stdio ``command`` to be
    a bare executable name, so a Python server is launched as ``python``:
    :func:`boot_server` puts this interpreter's directory first on the server's
    ``PATH``. JSON argv lists need no shell quoting. An empty ``mcp_servers``
    writes no ``mcp.json``.

    ``name`` materializes the copy under another folder (and so another
    marketplace name); ``agent_id`` renames the template's agent, for a leg that
    needs two distinct agents from one template.

    Returns the materialized marketplace root, the ``source`` for
    :func:`install_testing_agent`.
    """

    dest = ws_dir / "testing-marketplaces" / (name or template_dir.name)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(template_dir, dest)
    if agent_id:
        for module in (dest / "agents").glob("*.py"):
            text = module.read_text(encoding="utf-8")
            marker = f'    id = "{template_dir.name}"\n'
            if marker not in text:
                raise ValueError(f"{module} does not declare agent id {template_dir.name!r}")
            module.write_text(text.replace(marker, f'    id = "{agent_id}"\n'), encoding="utf-8")
    if mcp_servers:
        servers = {
            server: {"type": "stdio", "command": argv[0], "args": list(argv[1:])}
            for server, argv in mcp_servers.items()
        }
        (dest / "plugins" / template_dir.name / "mcp.json").write_text(
            json.dumps({"$schema": _MCP_SCHEMA, "mcpServers": servers}, indent=2) + "\n",
            encoding="utf-8",
        )
    return dest


def testing_prompt(marketplace: Path, name: str = "main") -> Path:
    """The prompt file ``name`` of a materialized testing marketplace's plugin.

    ``main`` is the agent's prompt fragment; ``worker`` is the defined worker
    subagent's prompt (``task-subagent-testing``). Legs append mission-specific
    instructions to these before install.
    """

    (plugin,) = (marketplace / "plugins").iterdir()
    return plugin / "ai.iowarp.clio" / "prompt" / f"{name}.md"


def testing_prompts(marketplace: Path) -> list[Path]:
    """Every prompt file of a materialized testing marketplace's plugin."""

    (plugin,) = (marketplace / "plugins").iterdir()
    return sorted((plugin / "ai.iowarp.clio" / "prompt").glob("*.md"))


def install_testing_agent(call: Callable[..., Any], marketplace: Path) -> str:
    """Install a materialized testing marketplace, run the trust step, return its agent ref.

    ``POST /v1/plugins/marketplaces`` then ``POST
    /v1/plugins/marketplaces/{name}/trust``: a local marketplace's Python and MCP
    servers load only after the trust step, and re-installing with a changed
    ``mcp.json`` holds the update until trust runs again, so this always trusts
    after installing. Idempotent: re-running a leg re-installs over the same name.
    """

    name = TESTING_MARKETPLACE_PREFIX + marketplace.name
    call("POST", "/v1/plugins/marketplaces", {"source": str(marketplace), "name": name})
    trusted = call("POST", f"/v1/plugins/marketplaces/{name}/trust", {})
    agents = list(trusted.get("agents") or [])
    if len(agents) != 1:
        raise RuntimeError(f"testing marketplace {name} must define one agent, got {agents}")
    return f"{name}/{agents[0]}"


def use_agent(call: Callable[..., Any], sid: str, ref: str) -> dict[str, Any]:
    """``PATCH /v1/sessions/{sid}/agent`` -- resolve agent ``ref`` and pin it to ``sid``."""

    return call("PATCH", f"/v1/sessions/{sid}/agent", {"ref": ref})


def agent_id_of(ref: str) -> str:
    """The agent id of a ``marketplace/id`` reference."""

    return ref.rsplit("/", 1)[-1].partition("@")[0]


def resolved_agent_tools(
    call: Callable[..., Any], sid: str, *, workspace_id: str = ""
) -> dict[str, list[str]]:
    """The session agent's resolved toolset, as ``{agent_id: [tool_name, ...]}``.

    ``GET /v1/sessions/{sid}/agent`` returns the pinned resolution the turn
    path builds from: its built-in and Python tools by name, and its plugins'
    MCP servers. A server's tools are listed live by ``GET /v1/mcp/handshake``
    (``session_id`` makes the probe see the session's plugin servers) and
    namespaced ``<server>_<tool>``, so this is the pre-turn PROOF that a
    testing agent's servers reached this session's agent.
    """

    view = call("GET", f"/v1/sessions/{sid}/agent")
    resolution = view.get("resolution") or {}
    agent_id = str((resolution.get("agent") or {}).get("id") or "")
    tools = list(resolution.get("tool_names") or [])
    servers = set(resolution.get("mcp_servers") or {})
    if servers:
        params: dict[str, str] = {"session_id": sid}
        if workspace_id:
            params["workspace_id"] = workspace_id
        handshake = call("GET", "/v1/mcp/handshake", params=params)
        for row in handshake.get("servers") or []:
            name = str(row.get("name") or "")
            if name in servers:
                tools.extend(f"{name}_{tool}" for tool in row.get("tools") or [])
    return {agent_id: tools}


# --------------------------------------------------------------------------- #
# Verdict output
# --------------------------------------------------------------------------- #
def write_verdict(out_path: Path, verdict: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
    print(json.dumps(verdict, indent=2, default=str), flush=True)


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
