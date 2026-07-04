#!/usr/bin/env python

"""ClioAgent Command-Line Interface — a thin GACT client.

Interactive TUI for the CLIO scientific-data agent. As of the "one front
door" work (#799/#800) this CLI holds **no in-process agent**: it does not
load DSPy, it does not construct a :class:`~clio_agent.agent.ClioAgent`, and
it never drives an LM directly. Everything runs on the GACT server; the CLI
speaks to it exclusively through the typed SDK
(:class:`clio_agent.sdk.ClioClient`).

Boot is separated from logic for testability: :class:`ClioAgentCLI` takes an
already-built :class:`ClioClient` (tests inject one over an in-process ASGI
transport), and the module-level :func:`boot_client` connect-or-spawns the
real server via :func:`clio_agent.serve.ensure_server` and wraps it in a
client. A server that cannot be reached is a structured, non-zero exit — no
silent fallback.

The standalone ``doctor`` subcommand is the one exception: it runs the same
probe engine (:func:`clio_agent.runtime.status.collect_runtime_status`)
**in-process**, because a doctor must work when no server is up. The in-REPL
``/doctor`` instead renders the *server's* health view via the SDK. Two access
paths, one engine.

Example:
    # Interactive (connect-or-spawn the server, then talk to it)
    $ uv run src/clio_agent/ui/cli.py

    # Diagnose locally without a server
    $ uv run src/clio_agent/ui/cli.py doctor
"""

import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Add src to path
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/clio_agent/ui/cli.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from clio_agent import serve
from clio_agent.sdk import (
    ClioClient,
    ClioSDKError,
    Health,
    MessageCompleted,
)

# ============================================================================
# CLI CLASS
# ============================================================================


class ClioAgentCLI:
    """Interactive CLI that talks to a GACT server through the SDK.

    The CLI owns no agent, no LM, and no ARC — it is a pure client. All
    reasoning happens server-side; this class posts messages, consumes the
    session SSE feed, and renders catalog/metric/health reads.

    Attributes:
        client: The injected :class:`ClioClient` (real one built by
            :func:`boot_client`; tests inject an in-process transport).
        console: Rich console for pretty output.
        history: Local render history (question/expert/answer rows).
        verbose: Show routing/latency details.
    """

    def __init__(
        self,
        client: ClioClient,
        *,
        verbose: bool = False,
        console: Console | None = None,
    ) -> None:
        """Initialize the CLI over an already-built client.

        Args:
            client: A connected :class:`ClioClient`. Boot is the caller's
                responsibility (see :func:`boot_client`) so tests can inject
                an in-process transport.
            verbose: Show detailed routing/latency info.
            console: Optional Rich console (tests pass a recording one).
        """
        self.client = client
        self.console = console or Console()
        self.verbose = verbose
        self.history: list[dict[str, Any]] = []
        # Reused across REPL turns so the conversation shares one session.
        self._session_id: str | None = None
        # SSE resume cursor: only events past this id are new to us.
        self._event_cursor: int | None = None

    # -- session lifecycle -------------------------------------------------

    def _ensure_session(self) -> str:
        """Return the reused session id, creating one on first use."""
        if self._session_id is None:
            session = self.client.sessions.create(title="CLI session")
            self._session_id = session.id
        return self._session_id

    # -- banner / help -----------------------------------------------------

    def print_banner(self) -> None:
        """Print the ClioAgent welcome banner."""
        from rich.align import Align

        logo = """   ____ _     ___ ___
  / ___| |   |_ _/ _ \\
 | |   | |    | | | | |
 | |___| |___ | | |_| |
  \\____|_____|___\\___/ """

        logo_text = Text(logo, style="bold cyan", justify="center")

        lm_line = "server-managed"
        try:
            provider = self.client.lm_provider()
            label = self._provider_label(provider.provider) if provider.provider else ""
            if provider.model and label:
                lm_line = f"{label} ({provider.model})"
            elif label:
                lm_line = label
            if not provider.configured:
                lm_line = f"{lm_line} [not configured]"
        except ClioSDKError:
            lm_line = "unavailable (server not reporting a provider)"

        info = f"""[dim]Multi-Agent System for Scientific Computing[/dim]

[cyan]Experts:[/cyan] data (HDF5, compression, I/O), analysis (Parquet, statistics), visualization (charts, plots)
[green]LM:[/green] {escape(lm_line)}

[dim]Gnosis Research Center | IOWarp Project[/dim]
[dim]https://iowarp.ai[/dim]"""

        self.console.print()
        self.console.print(Align.center(logo_text))
        self.console.print()
        self.console.print(
            Align.center(Panel(info, border_style="cyan", expand=False, padding=(0, 2)))
        )

    @staticmethod
    def _provider_label(provider: str) -> str:
        """Return a readable provider name for CLI status text."""
        labels = {
            "lm_studio": "LM Studio",
            "ollama": "Ollama",
            "openai": "OpenAI",
            "anthropic": "Anthropic",
        }
        return labels.get(provider, provider.replace("_", " ").title())

    def print_help(self) -> None:
        """Print the help table (only commands the client actually backs)."""
        help_table = Table(title="Commands", show_header=True)
        help_table.add_column("Command", style="cyan")
        help_table.add_column("Description")

        commands = [
            ("/help", "Show this help message"),
            ("/history", "Show conversation history"),
            ("/experts", "List available experts (server agent catalog)"),
            ("/registry", "Show agent-registry summary"),
            ("/memory", "Show ARC memory integration status"),
            ("/models", "Show the server's configured LM provider"),
            ("/tools", "Show the server's live tool catalog"),
            ("/doctor", "Show the server's health/integration status"),
            ("/metrics", "Show aggregate runtime metrics"),
            ("/compare <expert>", "Optimizer (not implemented — research surface)"),
            ("/rollback <expert>", "Optimizer (not implemented — research surface)"),
            ("/verbose", "Toggle verbose mode (routing/latency details)"),
            ("/clear", "Clear conversation history"),
            ("/quit, /exit", "Exit ClioAgent"),
        ]
        for cmd, desc in commands:
            help_table.add_row(cmd, desc)

        self.console.print(help_table)

    # -- catalog / config reads (all via the SDK) --------------------------

    def print_experts(self) -> None:
        """Print the server's agent catalog (backs ``/experts``)."""
        try:
            agents = self.client.agents()
        except ClioSDKError as exc:
            self.console.print(f"[red]Error listing agents: {escape(str(exc))}[/red]")
            return

        table = Table(title="Available Experts", show_header=True)
        table.add_column("Expert", style="cyan")
        table.add_column("Tier", justify="right")
        table.add_column("Description")
        table.add_column("Keywords", style="yellow")

        for agent in sorted(agents, key=lambda a: (a.tier, a.id)):
            name = agent.title or agent.id
            keywords = ", ".join(agent.keywords[:5])
            desc = agent.description or ""
            if len(desc) > 60:
                desc = desc[:60] + "..."
            table.add_row(
                escape(str(name)),
                str(agent.tier) if agent.tier else "-",
                escape(desc),
                escape(keywords),
            )

        self.console.print(table)

    def print_registry(self) -> None:
        """Print an agent-registry summary from the server catalog."""
        try:
            agents = self.client.agents()
        except ClioSDKError as exc:
            self.console.print(f"[red]Error reading registry: {escape(str(exc))}[/red]")
            return

        agent_ids = [a.id for a in agents]
        by_tier: dict[int, int] = {}
        for a in agents:
            by_tier[a.tier] = by_tier.get(a.tier, 0) + 1
        tier_summary = ", ".join(f"tier {t}: {n}" for t, n in sorted(by_tier.items())) or "none"

        info = f"""[bold]Agent Registry Status[/bold]

Registered Agents: {len(agent_ids)}
By Tier: {tier_summary}
Source: server agent catalog (GET /v1/agents)

[cyan]Registered Agent IDs:[/cyan]
{escape(", ".join(agent_ids)) if agent_ids else "None"}

[dim]Use /experts to see detailed agent capabilities[/dim]"""

        self.console.print(Panel(info, title="Registry Status", border_style="blue"))

    def print_memory(self) -> None:
        """Show the ARC memory integration status from server health."""
        try:
            health = self.client.health()
        except ClioSDKError as exc:
            self.console.print(f"[red]Error reading health: {escape(str(exc))}[/red]")
            return

        arc_row = None
        for item in health.integrations:
            if "arc" in item.name.lower() or "memory" in item.name.lower():
                arc_row = item
                break

        if arc_row is None:
            self.console.print(
                "[yellow]The server did not report an ARC memory integration.[/yellow]"
            )
            return

        table = Table(title="ARC Memory Integration")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Name", escape(arc_row.name))
        table.add_row("Status", escape(arc_row.status))
        summary = arc_row.summary or arc_row.detail
        if summary:
            table.add_row("Summary", escape(summary))
        if arc_row.config_source:
            table.add_row("Config Source", escape(arc_row.config_source))
        if arc_row.endpoint:
            table.add_row("Endpoint", escape(arc_row.endpoint))
        if arc_row.next_action:
            table.add_row("Next Action", escape(arc_row.next_action))
        self.console.print(table)

    def print_tools(self) -> None:
        """Display the server's live tool catalog (backs ``/tools``)."""
        try:
            tools = self.client.tools()
        except ClioSDKError as exc:
            self.console.print(f"[red]Error listing tools: {escape(str(exc))}[/red]")
            return

        table = Table(title="Tools (server catalog)", show_header=True)
        table.add_column("Tool", style="cyan")
        table.add_column("Server", style="magenta")
        table.add_column("Description")

        for tool in sorted(tools, key=lambda t: (t.source == "error", t.name or t.id)):
            desc = tool.description or ""
            style_name = tool.name or tool.id
            if tool.source == "error":
                table.add_row(
                    f"[red]{escape(style_name)}[/red]",
                    escape(tool.server_id),
                    f"[red]{escape(desc[:80])}[/red]",
                )
            else:
                table.add_row(escape(style_name), escape(tool.server_id), escape(desc[:80]))

        self.console.print(table)

    def print_models(self) -> None:
        """Display the server's configured LM provider (backs ``/models``)."""
        try:
            provider = self.client.lm_provider()
        except ClioSDKError as exc:
            self.console.print(f"[red]Error reading LM provider: {escape(str(exc))}[/red]")
            return

        table = Table(title="LM Provider (server)", show_header=True)
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Configured", "yes" if provider.configured else "no")
        table.add_row(
            "Provider", self._provider_label(provider.provider) if provider.provider else "-"
        )
        table.add_row("API Base", provider.api_base or "-")
        table.add_row("Model", provider.model or "-")
        table.add_row("Max Tokens", str(provider.max_tokens))
        if provider.context_length:
            table.add_row("Context Length", str(provider.context_length))
        table.add_row("State", provider.state)
        if provider.status_message:
            table.add_row("Status", escape(provider.status_message))
        if provider.error:
            table.add_row("Error", f"[red]{escape(provider.error)}[/red]")
        self.console.print(table)

    def print_doctor(self) -> None:
        """Render the *server's* health view (backs the in-REPL ``/doctor``)."""
        try:
            health = self.client.health()
        except ClioSDKError as exc:
            self.console.print(f"[red]Error reading health: {escape(str(exc))}[/red]")
            return
        render_health(self.console, health)

    def _handle_metrics(self) -> None:
        """Handle ``/metrics`` — aggregate runtime counters from the server."""
        try:
            metrics = self.client.metrics()
        except ClioSDKError as exc:
            self.console.print(f"[red]Error reading metrics: {escape(str(exc))}[/red]")
            return

        overview = Table(title="Runtime Metrics", show_header=True)
        overview.add_column("Metric", style="cyan")
        overview.add_column("Value", justify="right", style="green")
        overview.add_row("Uptime (s)", str(metrics.uptime_s))
        overview.add_row("Sessions (total)", str(metrics.sessions.total))
        overview.add_row("Sessions (active)", str(metrics.sessions.active))
        overview.add_row("Messages (total)", str(metrics.messages.total))
        overview.add_row("Tokens in", str(metrics.tokens.input_total))
        overview.add_row("Tokens out", str(metrics.tokens.output_total))
        overview.add_row("Cost (USD)", f"{metrics.cost.total_usd:.4f}")
        self.console.print(overview)

        if metrics.latencies:
            lat = Table(title="Latencies", show_header=True)
            lat.add_column("Operation", style="cyan")
            lat.add_column("Count", justify="right")
            lat.add_column("p50 (ms)", justify="right")
            lat.add_column("p95 (ms)", justify="right")
            lat.add_column("max (ms)", justify="right")
            for name, stat in sorted(metrics.latencies.items()):
                lat.add_row(
                    escape(name),
                    str(stat.count),
                    f"{stat.p50_ms:.1f}",
                    f"{stat.p95_ms:.1f}",
                    f"{stat.max_ms:.1f}",
                )
            self.console.print(lat)

    def _handle_optimizer_stub(self, verb: str) -> None:
        """Handle ``/compare`` and ``/rollback`` — the optimizer is a research
        surface, not implemented. Emit the uniform structured message; never
        touch an in-process optimizer (there is none in the CLI anymore)."""
        from clio_agent.optimizer.stub import optimizer_not_implemented_payload

        payload = optimizer_not_implemented_payload()
        self.console.print(f"[yellow]{escape(payload['message'])}[/yellow]")

    # -- command dispatch --------------------------------------------------

    def handle_command(self, user_input: str) -> bool:
        """Handle a slash command. Returns True if it was handled."""
        cmd = user_input.lower().strip()

        if cmd in ("/help", "/h"):
            self.print_help()
            return True
        if cmd == "/history":
            if not self.history:
                self.console.print("[yellow]No history yet[/yellow]")
            else:
                for i, entry in enumerate(self.history, 1):
                    self.console.print(f"\n[cyan]{i}. Q:[/cyan] {entry['question']}")
                    self.console.print(f"[cyan]   Expert:[/cyan] {entry['expert']}")
                    self.console.print(f"[green]   A:[/green] {entry['answer'][:100]}...")
            return True
        if cmd == "/clear":
            self.history = []
            self.console.clear()
            self.print_banner()
            self.console.print("[green]History cleared[/green]")
            return True
        if cmd == "/experts":
            self.print_experts()
            return True
        if cmd == "/registry":
            self.print_registry()
            return True
        if cmd == "/memory":
            self.print_memory()
            return True
        if cmd == "/models":
            self.print_models()
            return True
        if cmd == "/tools":
            self.print_tools()
            return True
        if cmd == "/doctor":
            self.print_doctor()
            return True
        if cmd == "/metrics":
            self._handle_metrics()
            return True
        if cmd.startswith("/compare"):
            self._handle_optimizer_stub("compare")
            return True
        if cmd.startswith("/rollback"):
            self._handle_optimizer_stub("rollback")
            return True
        if cmd == "/verbose":
            self.verbose = not self.verbose
            status = "enabled" if self.verbose else "disabled"
            self.console.print(f"[green]Verbose mode {status}[/green]")
            return True
        if cmd in ("/quit", "/exit", "/q"):
            self.console.print("\n[cyan]Thanks for using ClioAgent![/cyan]\n")
            sys.exit(0)

        return False

    # -- main Q&A (post -> SSE -> render) -----------------------------------

    def ask_question(self, question: str) -> dict[str, Any]:
        """Ask the server a question: post the message, consume the session
        SSE feed until the turn completes, and return the rendered result.

        The conversation reuses one server session across REPL turns; the SSE
        cursor advances so each turn only reads its own new events.

        Args:
            question: The user's question.

        Returns:
            A dict with ``question``, ``expert``, ``answer``, ``error_info``,
            ``session_id``, and ``duration_ms``.
        """
        session_id = self._ensure_session()
        start = time.time()
        with self.console.status("[#00B4FF]Running agent loop...[/#00B4FF]", spinner="dots"):
            ack = self.client.messages.post(session_id, text=question)
            completed, message = self._consume_turn(session_id, ack.message_id)
        duration_ms = (time.time() - start) * 1000.0

        error_info: dict[str, Any] | None = None
        answer = ""
        expert = ""
        if completed is not None and completed.error_info is not None:
            error_info = completed.error_info.model_dump()
            answer = completed.error_info.message
        elif message is not None:
            answer = message.text()
            if message.error_info is not None:
                error_info = message.error_info.model_dump()
            for part in message.parts:
                if part.type == "routing_decision" and part.selected_agent:
                    expert = part.selected_agent
                    break

        return {
            "question": question,
            "expert": expert,
            "answer": answer,
            "error_info": error_info,
            "session_id": session_id,
            "duration_ms": duration_ms,
        }

    def _consume_turn(self, session_id: str, ack_message_id: str) -> tuple[Any, Any]:
        """Consume the SSE feed until this turn's ``message.completed``.

        Returns ``(completed_event, assistant_message)``. The assistant
        message is fetched from the ledger once the turn settles so we render
        the authoritative parts, not buffered deltas.
        """
        completed = None
        with self.client.sessions.events(session_id, last_event_id=self._event_cursor) as stream:
            for event in stream:
                if isinstance(event, MessageCompleted):
                    completed = event
                    break
            self._event_cursor = stream.last_event_id

        message = None
        if completed is not None and completed.message_id:
            try:
                message = self.client.messages.get(session_id, completed.message_id)
            except ClioSDKError:
                message = None
        return completed, message

    def _render_answer(self, result: dict[str, Any]) -> None:
        """Render one Q&A result in the Panel/Markdown house style."""
        if self.verbose:
            if result["expert"]:
                self.console.print(
                    f"\n[#00B4FF]Agent:[/#00B4FF] [bold]{escape(result['expert'])}[/bold]"
                )
            self.console.print(f"[#FF8800]Duration:[/#FF8800] {result['duration_ms']:.0f}ms\n")

        if result["error_info"]:
            message = str(result["error_info"].get("message") or "CLIO reported an error.")
            self.console.print(Panel(Markdown(message), title="CLIO Error", border_style="red"))
            return

        expert_label = (result["expert"] or "CLIO").upper()
        subtitle = None if self.verbose else "[dim]Agent loop[/dim]"
        title = "[bold #00FF88]CLIO[/bold #00FF88]"
        if result["expert"]:
            title = f"{title} [dim]via {escape(expert_label)}[/dim]"
        self.console.print(
            Panel(
                Markdown(result["answer"] or "_(no answer)_"),
                title=title,
                subtitle=subtitle,
                border_style="#00B4FF",
            )
        )

    def run(self) -> None:
        """Run the interactive REPL loop."""
        from prompt_toolkit import prompt as pt_prompt
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import InMemoryHistory

        self.print_banner()
        self.console.print(
            "\n[bold green]Ready[/bold green]  [dim]|[/dim]  Type [cyan]/help[/cyan] for commands\n"
        )

        history = InMemoryHistory()

        while True:
            try:
                user_input = pt_prompt(
                    "You: ", history=history, auto_suggest=AutoSuggestFromHistory()
                ).strip()

                if not user_input:
                    continue

                if user_input.startswith("/") and self.handle_command(user_input):
                    continue

                result = self.ask_question(user_input)
                self._render_answer(result)
                self.history.append(result)

            except KeyboardInterrupt:
                self.console.print("\n\n[#FF8800]Use /quit to exit gracefully[/#FF8800]")
                continue
            except EOFError:
                self.console.print("\n[#00FF88]Goodbye![/#00FF88]\n")
                break
            except ClioSDKError as e:
                self.console.print(f"\n[red]Server error: {escape(str(e))}[/red]")
                continue
            except Exception as e:  # noqa: BLE001 - REPL must survive one bad turn
                self.console.print(f"\n[red]Error: {escape(str(e))}[/red]")
                if self.verbose:
                    import traceback

                    traceback.print_exc()
                continue


# ============================================================================
# BOOT + RENDER HELPERS (module level)
# ============================================================================


def boot_client(
    port: int = 8100,
    host: str = "127.0.0.1",
    *,
    console: Console | None = None,
) -> ClioClient:
    """Connect-or-spawn the GACT server and return a client for it.

    Delegates to :func:`clio_agent.serve.ensure_server` (attach to a running
    server or spawn one), then wraps the base URL in a :class:`ClioClient`.
    A server that cannot be located or started is a structured, non-zero
    exit — never a silent fallback.

    Args:
        port: TCP port to probe/serve on (default 8100).
        host: Bind/probe host (default ``127.0.0.1``).
        console: Optional console for the structured error (tests inject one).

    Returns:
        A connected :class:`ClioClient`.

    Raises:
        SystemExit: If the server cannot be reached/started (after printing
            the structured reason).
    """
    console = console or Console()
    try:
        base_url = serve.ensure_server(port, host)
    except serve.ServeError as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        reason = payload.get("reason", "error")
        detail = payload.get("detail", str(exc))
        console.print(f"\n[red]Cannot start the CLIO server ({escape(str(reason))}).[/red]")
        console.print(f"[yellow]{escape(str(detail))}[/yellow]")
        searched = payload.get("searched")
        if searched:
            console.print(f"[dim]Searched: {escape(str(searched))}[/dim]")
        log = payload.get("log")
        if log:
            console.print(f"[dim]Server log: {escape(str(log))}[/dim]")
        raise SystemExit(1) from exc
    return ClioClient(base_url)


def render_health(console: Console, health: Health) -> None:
    """Render an SDK :class:`~clio_agent.sdk.Health` (the server's doctor view).

    Reads the widened per-integration fields (#800): ``status`` plus
    ``summary`` / ``config_source`` / ``endpoint`` / ``next_action``. This is
    the SDK counterpart of :func:`render_doctor_report` (which renders the
    in-process ``collect_runtime_status`` report object).
    """
    status_styles = {
        "ready": "green",
        "degraded": "yellow",
        "unavailable": "red",
        "misconfigured": "red",
        "skipped": "cyan",
    }
    overall = health.overall_status or ("healthy" if health.healthy else "unhealthy")
    table = Table(title=f"CLIO Server Health ({overall})", show_header=True)
    table.add_column("Integration", style="cyan")
    table.add_column("Status")
    table.add_column("Summary")
    table.add_column("Config Source")
    table.add_column("Endpoint")
    table.add_column("Next Action")

    for item in health.integrations:
        style = status_styles.get(item.status, "white")
        summary = item.summary or item.detail or ""
        table.add_row(
            escape(item.name),
            f"[{style}]{escape(item.status)}[/{style}]",
            escape(summary),
            escape(item.config_source or ""),
            escape(item.endpoint or ""),
            escape(item.next_action or ""),
        )

    console.print(table)


def render_doctor_report(console: Console, report: Any) -> None:
    """Render an in-process runtime doctor report (``collect_runtime_status``).

    Reads :class:`~clio_agent.runtime.status.IntegrationStatus.state`; used by
    the standalone ``doctor`` subcommand, which must run without a server.
    """
    status_styles = {
        "ready": "green",
        "degraded": "yellow",
        "unavailable": "red",
        "misconfigured": "red",
        "skipped": "cyan",
    }
    title = f"CLIO Runtime Doctor ({report.overall_status})"
    table = Table(title=title, show_header=True)
    table.add_column("Integration", style="cyan")
    table.add_column("Status")
    table.add_column("Summary")
    table.add_column("Config Source")
    table.add_column("Endpoint")
    table.add_column("Next Action")

    for item in report.integrations:
        state = item.state.value
        style = status_styles.get(state, "white")
        table.add_row(
            item.name,
            f"[{style}]{state}[/{style}]",
            escape(item.summary),
            escape(item.config_source),
            escape(item.endpoint or ""),
            escape(item.next_action),
        )

    console.print(table)


def run_doctor(json_output: bool = False) -> int:
    """Run the non-interactive doctor command IN-PROCESS.

    A doctor must work when no server is up, so this uses the same probe
    engine the server hosts at ``/v1/health`` directly, via
    :func:`clio_agent.runtime.status.collect_runtime_status`.
    """
    from clio_agent.runtime.status import collect_runtime_status

    report = collect_runtime_status()
    if json_output:
        import json

        print(json.dumps(report.to_dict(), indent=2))
    else:
        render_doctor_report(Console(), report)
    return 0


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================


def run_cli(verbose: bool = False, *, port: int = 8100, host: str = "127.0.0.1") -> None:
    """Boot the server-backed client and run the interactive CLI.

    Args:
        verbose: Show routing reasoning and latency.
        port: Server port to connect-or-spawn.
        host: Server host.
    """
    client = boot_client(port, host)
    try:
        ClioAgentCLI(client, verbose=verbose).run()
    finally:
        client.close()


def run_query(
    query: str,
    *,
    session_title: str = "CLI query",
    json_output: bool = False,
    verbose: bool = False,
    port: int = 8100,
    host: str = "127.0.0.1",
) -> int:
    """Non-interactive single-question mode over the server-backed client."""
    import json

    console = Console()
    client = boot_client(port, host, console=console)
    try:
        cli = ClioAgentCLI(client, verbose=verbose, console=console)
        result = cli.ask_question(query)
        if json_output:
            output = {
                "question": query,
                "answer": result["answer"],
                "selected_expert": result["expert"],
                "duration_ms": result["duration_ms"],
                "session_id": result["session_id"],
                "error_info": result["error_info"],
                "status": "degraded" if result["error_info"] else "success",
            }
            print(json.dumps(output, indent=2))
        else:
            console.print(f"\n[bold cyan]Question:[/bold cyan] {escape(query)}")
            cli._render_answer(result)
        return 1 if result["error_info"] else 0
    except ClioSDKError as exc:
        if json_output:
            print(json.dumps({"error": str(exc), "status": "failed"}))
        else:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="ClioAgent: Agent Framework for Scientific Computing (IOWarp Intelligence Layer)"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["doctor"],
        help="Optional command. Use 'doctor' to inspect runtime integrations (in-process).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show routing reasoning and latency"
    )
    parser.add_argument(
        "--query", "-q", type=str, help="Non-interactive mode: ask single question and exit"
    )
    parser.add_argument(
        "--session",
        type=str,
        default="CLI query",
        help="Session title for the non-interactive query (default: 'CLI query')",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output results as JSON (use with --query)"
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=8100, help="Server port")
    parser.add_argument(
        "--tune",
        type=str,
        choices=["data", "analysis", "visualization"],
        metavar="EXPERT_ID",
        help="Run optimizer for an expert (not implemented — research surface)",
    )

    args = parser.parse_args()
    from clio_agent.config import load_project_env_file  # noqa: PLC0415
    from clio_agent.runtime import trace  # noqa: PLC0415

    load_project_env_file()
    trace.configure()

    if args.command == "doctor":
        sys.exit(run_doctor(json_output=args.json))

    # Tune mode (#801): the optimizer is a research surface — return the
    # uniform structured not-implemented stub. No LM setup, no server needed.
    if args.tune:
        import json as tune_json

        from clio_agent.optimizer.stub import optimizer_not_implemented_payload

        tune_payload = {"expert_id": args.tune, **optimizer_not_implemented_payload()}
        if args.json:
            print(tune_json.dumps(tune_payload))
        else:
            Console().print(f"[yellow]{tune_payload['message']}[/yellow]")
        sys.exit(2)

    if args.query:
        sys.exit(
            run_query(
                args.query,
                session_title=args.session,
                json_output=args.json,
                verbose=args.verbose,
                port=args.port,
                host=args.host,
            )
        )
    else:
        run_cli(verbose=args.verbose, port=args.port, host=args.host)
