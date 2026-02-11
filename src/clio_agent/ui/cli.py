#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "fastmcp>=2.13.0",
#   "rich>=14.2.0",
#   "prompt-toolkit>=3.0.0",
#   "sortedcontainers>=2.4.0",
#   "msgspec>=0.18.0",
#   "requests>=2.31.0",
# ]
# ///

"""
ClioAgent Command-Line Interface

Interactive ClioAgent Agent Framework TUI for scientific data I/O assistance.

Features:
- Router-based dispatch to DataExpert or ChatAgent
- DataExpert agent with ReAct pattern (reasoning + tool calling)
- ChatAgent for conversational responses
- Rich TUI with syntax highlighting
- Conversation history

Example:
    # Run CLI with LM Studio
    $ uv run src/clio_agent/ui/cli.py

    # Or from Python
    >>> from clio_agent.ui.cli import run_cli
    >>> run_cli()
"""

import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Add src to path
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/clio_agent/ui/cli.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from clio_agent.agent import ClioAgent
from clio_agent.config import setup_dspy

# ============================================================================
# CLI CLASS
# ============================================================================

class ClioAgentCLI:
    """Interactive CLI for ClioAgent data I/O expert system.

    Demonstrates:
    - ClioAgent Router -> Expert/Chat dispatch
    - DataExpert ReAct agent with MCP tools
    - ChatAgent for conversational queries
    - Observable reasoning traces

    Attributes:
        agent: ClioAgent (main agent instance)
        console: Rich console for pretty output
        history: Conversation history
    """

    def __init__(
        self,
        verbose: bool = False
    ):
        """Initialize ClioAgent CLI.

        Args:
            verbose: Show detailed routing/reasoning
        """
        self.console = Console()
        self.verbose = verbose
        self.history = []

        # Setup LM Studio
        try:
            setup_dspy(
                verbose=False  # Don't spam console during init
            )
        except Exception as e:
            self.console.print(f"\n[red]Error setting up LM: {e}[/red]")
            self.console.print("\n[yellow]Troubleshooting:[/yellow]")
            self.console.print("- Ensure LM Studio is running at http://127.0.0.1:1234")
            self.console.print("- Ensure a model is loaded in LM Studio")
            sys.exit(1)

        # Create ClioAgent agent
        self.agent = ClioAgent(verbose=False)

    def print_banner(self):
        """Print ClioAgent welcome banner."""
        from rich.align import Align

        # ASCII art logo
        logo = """   ____ _     ___ ___
  / ___| |   |_ _/ _ \\
 | |   | |    | | | | |
 | |___| |___ | | |_| |
  \\____|_____|___\\___/ """

        # Create centered logo
        logo_text = Text(logo, style="bold cyan", justify="center")

        # Info section with proper markup
        info = """[dim]Multi-Agent System for Scientific Computing[/dim]

[cyan]Experts:[/cyan] data (HDF5, compression, I/O), analysis (Parquet, statistics), visualization (charts, plots)
[green]Local LM:[/green] LM Studio (Router + ChatAgent + 3 Experts)

[dim]Gnosis Research Center | IOWarp Project[/dim]
[dim]https://iowarp.ai[/dim]"""

        self.console.print()
        self.console.print(Align.center(logo_text))
        self.console.print()
        self.console.print(Align.center(
            Panel(info, border_style="cyan", expand=False, padding=(0, 2))
        ))

    def print_help(self):
        """Print help message."""
        help_table = Table(title="Commands", show_header=True)
        help_table.add_column("Command", style="cyan")
        help_table.add_column("Description")

        commands = [
            ("/help", "Show this help message"),
            ("/history", "Show conversation history"),
            ("/experts", "List available experts and capabilities"),
            ("/registry", "Show agent registry status"),
            ("/memory", "Display ARC memory statistics"),
            ("/tools", "Show available MCP tools"),
            ("/metrics", "Show per-expert performance metrics"),
            ("/compare <expert>", "Compare all variants for an expert"),
            ("/rollback <expert>", "Rollback to previous variant for an expert"),
            ("/verbose", "Toggle verbose mode (show routing details)"),
            ("/clear", "Clear conversation history"),
            ("/quit, /exit", "Exit ClioAgent"),
        ]

        for cmd, desc in commands:
            help_table.add_row(cmd, desc)

        self.console.print(help_table)

    def print_experts(self):
        """Print available experts and their capabilities."""
        from clio_agent.experts import get_expert_capabilities

        caps = get_expert_capabilities()

        experts_table = Table(title="Available Experts", show_header=True)
        experts_table.add_column("Expert", style="cyan")
        experts_table.add_column("Description")
        experts_table.add_column("Keywords", style="yellow")

        for expert_id, cap in caps.items():
            keywords = ", ".join(cap['keywords'][:5])  # Show first 5
            experts_table.add_row(
                expert_id,
                cap['description'][:60] + "...",
                keywords
            )

        self.console.print(experts_table)

    def print_registry(self):
        """Print agent registry status and statistics."""
        agent_count = self.agent.registry.get_agent_count()
        agent_ids = self.agent.registry.list_agents()

        info = f"""[bold]Agent Registry Status[/bold]

Registered Agents: {agent_count}
Registry Type: Capability-Based Routing
Router: Literal["chat", "data", "analysis", "visualization", "none"] via ChainOfThought

[cyan]Registered Agent IDs:[/cyan]
{', '.join(agent_ids) if agent_ids else 'None'}

[dim]Use /experts to see detailed agent capabilities[/dim]"""

        self.console.print(Panel(info, title="Registry Status", border_style="blue"))

    def print_memory(self):
        """Display ARC memory statistics."""
        stats = self.agent.get_arc_stats()

        table = Table(title="ARC Memory Statistics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Cache Hit Rate", f"{stats['hit_rate']:.2%}")
        table.add_row("Cache Size", f"{stats['size']}/{stats['capacity']}")
        table.add_row("Cache Hits", str(stats['hits']))
        table.add_row("Cache Misses", str(stats['misses']))
        table.add_row("Disk Reads", str(stats.get('disk_reads', 0)))
        table.add_row("Disk Writes", str(stats.get('disk_writes', 0)))

        self.console.print(table)

        # Show performance vs targets
        hit_rate = stats['hit_rate']
        if hit_rate >= 0.85:
            self.console.print(f"\n[green]Cache hit rate ({hit_rate:.1%}) exceeds target (85%)[/green]")
        else:
            self.console.print(f"\n[yellow]Cache hit rate ({hit_rate:.1%}) below target (85%)[/yellow]")

    def print_tools(self):
        """Display available MCP tools from the gateway."""
        from fastmcp import Client

        from clio_agent.tools.gateway import gateway

        async def _list():
            async with Client(gateway) as c:
                return await c.list_tools()

        try:
            tools = asyncio.run(_list())
        except Exception as e:
            self.console.print(f"[red]Error listing tools: {e}[/red]")
            return

        tools_table = Table(title="MCP Tools (via Gateway)", show_header=True)
        tools_table.add_column("Tool", style="cyan")
        tools_table.add_column("Description")

        for t in sorted(tools, key=lambda x: x.name):
            desc = t.description or ""
            tools_table.add_row(t.name, desc[:80])

        self.console.print(tools_table)

    def handle_command(self, user_input: str) -> bool:
        """Handle special commands.

        Args:
            user_input: User's input

        Returns:
            True if command was handled, False otherwise
        """
        cmd = user_input.lower().strip()

        if cmd in ["/help", "/h"]:
            self.print_help()
            return True

        elif cmd == "/history":
            if not self.history:
                self.console.print("[yellow]No history yet[/yellow]")
            else:
                for i, entry in enumerate(self.history, 1):
                    self.console.print(f"\n[cyan]{i}. Q:[/cyan] {entry['question']}")
                    self.console.print(f"[cyan]   Expert:[/cyan] {entry['expert']}")
                    self.console.print(f"[green]   A:[/green] {entry['answer'][:100]}...")
            return True

        elif cmd == "/clear":
            self.history = []
            self.console.clear()
            self.print_banner()
            self.console.print("[green]History cleared[/green]")
            return True

        elif cmd == "/experts":
            self.print_experts()
            return True

        elif cmd == "/registry":
            self.print_registry()
            return True

        elif cmd == "/memory":
            self.print_memory()
            return True

        elif cmd == "/tools":
            self.print_tools()
            return True

        elif cmd == "/metrics":
            self._handle_metrics()
            return True

        elif cmd.startswith("/compare"):
            parts = cmd.split(None, 1)
            if len(parts) < 2:
                self.console.print("[yellow]Usage: /compare <data|analysis|visualization>[/yellow]")
            else:
                self._handle_compare(parts[1])
            return True

        elif cmd.startswith("/rollback"):
            parts = cmd.split(None, 1)
            if len(parts) < 2:
                self.console.print("[yellow]Usage: /rollback <data|analysis|visualization>[/yellow]")
            else:
                self._handle_rollback(parts[1])
            return True

        elif cmd == "/verbose":
            self.verbose = not self.verbose
            status = "enabled" if self.verbose else "disabled"
            self.console.print(f"[green]Verbose mode {status}[/green]")
            return True

        elif cmd in ["/quit", "/exit", "/q"]:
            self.console.print("\n[cyan]Thanks for using ClioAgent![/cyan]\n")
            sys.exit(0)

        return False

    def _handle_metrics(self) -> None:
        """Handle /metrics command -- show per-expert performance metrics."""
        from clio_agent.optimizer.instrumentation import MetricsAggregator

        aggregator = MetricsAggregator(self.agent.arc)

        table = Table(title="Expert Performance Metrics", show_header=True)
        table.add_column("Expert", style="cyan")
        table.add_column("Success Rate", justify="right")
        table.add_column("Avg Latency (ms)", justify="right")
        table.add_column("Total Invocations", justify="right")
        table.add_column("Cache Hit Rate", justify="right")

        has_data = False
        for expert_id in ["data", "analysis", "visualization"]:
            metrics = aggregator.compute_expert_metrics(expert_id)
            if metrics["total_invocations"] > 0:
                has_data = True
            table.add_row(
                expert_id,
                f"{metrics['success_rate']:.1%}",
                f"{metrics['avg_latency_ms']:.1f}",
                str(metrics["total_invocations"]),
                f"{metrics['cache_hit_rate']:.1%}",
            )

        if has_data:
            self.console.print(table)
        else:
            self.console.print("[yellow]No invocation data yet. Run some queries first.[/yellow]")

    def _handle_compare(self, expert_id: str) -> None:
        """Handle /compare command -- show variant comparison table for an expert."""
        from clio_agent.optimizer.variants import VariantManager

        vm = VariantManager(self.agent.arc)
        variants = vm.compare(expert_id)

        if not variants:
            self.console.print(f"[yellow]No variants found for {expert_id}.[/yellow]")
            return

        import datetime

        table = Table(title=f"Variants for {expert_id}", show_header=True)
        table.add_column("Variant ID", style="cyan")
        table.add_column("Before", justify="right")
        table.add_column("After", justify="right")
        table.add_column("Delta", justify="right")
        table.add_column("p-value", justify="right")
        table.add_column("Significant", justify="center")
        table.add_column("Active", justify="center")
        table.add_column("Created", justify="right")

        for v in variants:
            created = datetime.datetime.fromtimestamp(v.created_at).strftime("%Y-%m-%d %H:%M")
            table.add_row(
                v.variant_id,
                f"{v.before_score:.2f}",
                f"{v.after_score:.2f}",
                f"{v.improvement_delta:+.2f}",
                f"{v.p_value:.4f}",
                "[green]Yes[/green]" if v.is_significant else "[red]No[/red]",
                "[green]Yes[/green]" if v.is_active else "No",
                created,
            )

        self.console.print(table)

    def _handle_rollback(self, expert_id: str) -> None:
        """Handle /rollback command -- revert to previous variant."""
        from clio_agent.optimizer.variants import VariantManager

        vm = VariantManager(self.agent.arc)
        restored = vm.rollback(expert_id)

        if restored:
            # Load variant state into running expert
            expert_attr = {
                "data": "data_expert",
                "analysis": "analysis_expert",
                "visualization": "visualization_expert",
            }.get(expert_id)

            if expert_attr and hasattr(self.agent, expert_attr):
                try:
                    vm.load_variant(getattr(self.agent, expert_attr), restored)
                except Exception as e:
                    self.console.print(f"[yellow]Warning: Could not load variant state: {e}[/yellow]")

            self.console.print(f"[green]Rolled back to variant {restored}[/green]")
        else:
            self.console.print("[yellow]No previous variant to rollback to.[/yellow]")

    def ask_question(self, question: str) -> dict:
        """Ask ClioAgent a question via Router -> Expert/Chat dispatch.

        Flow:
            1. Router classifies intent (Literal["data", "chat"])
            2. Dispatches to DataExpert or ChatAgent
            3. Display results with expert label

        Args:
            question: User's question

        Returns:
            Dictionary with result including answer, expert, and stats
        """
        # Show processing spinner
        with self.console.status(
            "[#00B4FF]Routing query...[/#00B4FF]",
            spinner="dots"
        ):
            result = self.agent(question=question)

        return {
            "question": question,
            "expert": result.selected_expert,
            "answer": result.answer,
            "duration_ms": getattr(result, 'duration_ms', 0)
        }

    def run(self):
        """Run interactive CLI loop with enhanced UX."""
        from prompt_toolkit import prompt as pt_prompt
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import InMemoryHistory

        self.print_banner()
        self.console.print("\n[bold green]Ready[/bold green]  [dim]|[/dim]  Type [cyan]/help[/cyan] for commands\n")

        # Setup prompt toolkit for better input
        history = InMemoryHistory()

        while True:
            try:
                # Get user input with history and auto-suggest
                user_input = pt_prompt(
                    "You: ",
                    history=history,
                    auto_suggest=AutoSuggestFromHistory()
                ).strip()

                if not user_input.strip():
                    continue

                # Handle commands
                if user_input.startswith("/"):
                    if self.handle_command(user_input):
                        continue

                # Ask question via ClioAgent agent
                result = self.ask_question(user_input)

                # Show verbose info
                if self.verbose:
                    self.console.print(f"\n[#00B4FF]Router:[/#00B4FF] [bold]{result['expert']}[/bold]")
                    self.console.print(f"[#FF8800]Duration:[/#FF8800] {result['duration_ms']:.0f}ms\n")

                # Show answer with expert label in panel
                expert_label = result['expert'].upper()
                self.console.print(
                    Panel(
                        Markdown(result['answer']),
                        title=f"[bold #00FF88]CLIO[/bold #00FF88] [dim]via {expert_label}[/dim]",
                        subtitle="[dim]Router dispatch[/dim]" if not self.verbose else None,
                        border_style="#00B4FF"
                    )
                )

                # Add to history
                self.history.append(result)

            except KeyboardInterrupt:
                self.console.print("\n\n[#FF8800]Use /quit to exit gracefully[/#FF8800]")
                continue

            except EOFError:
                # Ctrl+D pressed
                self.console.print("\n[#00FF88]Goodbye![/#00FF88]\n")
                break

            except Exception as e:
                self.console.print(f"\n[red]Error: {e}[/red]")
                if self.verbose:
                    import traceback
                    traceback.print_exc()
                continue


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def run_cli(
    verbose: bool = False
):
    """Run ClioAgent CLI with agent framework.

    Args:
        verbose: Show routing reasoning and tool calls

    Example:
        >>> run_cli()  # Uses LM Studio
    """
    cli = ClioAgentCLI(
        verbose=verbose
    )
    cli.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="ClioAgent: Agent Framework for Scientific Computing (IOWarp Intelligence Layer)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show routing reasoning and tool calls"
    )
    parser.add_argument(
        "--query", "-q",
        type=str,
        help="Non-interactive mode: ask single question and exit"
    )
    parser.add_argument(
        "--session",
        type=str,
        default="cli_session",
        help="Session ID for conversation tracking (default: cli_session)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON (use with --query)"
    )
    parser.add_argument(
        "--tune",
        type=str,
        choices=["data", "analysis", "visualization"],
        metavar="EXPERT_ID",
        help="Run SIMBA optimization for an expert (data|analysis|visualization)"
    )

    args = parser.parse_args()

    # Tune mode: SIMBA optimization for an expert
    if args.tune:
        from rich.console import Console as TuneConsole

        tune_console = TuneConsole()
        expert_id = args.tune

        try:
            setup_dspy(verbose=args.verbose)
        except Exception as e:
            tune_console.print(f"[red]Error setting up LM: {e}[/red]")
            sys.exit(1)

        agent = ClioAgent(verbose=args.verbose)

        # Step 1: Generate training set
        from clio_agent.optimizer.trainer import TrainingSetGenerator

        tune_console.print(f"Generating training set for [cyan]{expert_id}[/cyan]...")
        generator = TrainingSetGenerator(agent.arc)
        try:
            trainset = generator.generate(expert_id, min_examples=30)
        except ValueError as e:
            tune_console.print(f"[red]{e}[/red]")
            sys.exit(1)

        tune_console.print(f"Found [green]{len(trainset)}[/green] training examples")

        # Step 2: Get expert module
        expert_map = {
            "data": agent.data_expert,
            "analysis": agent.analysis_expert,
            "visualization": agent.visualization_expert,
        }
        expert_module = expert_map[expert_id]

        # Step 3: Run SIMBA optimization
        from clio_agent.optimizer.runner import SIMBARunner
        from clio_agent.optimizer.variants import VariantManager

        variant_manager = VariantManager(agent.arc)
        runner = SIMBARunner(agent.arc, variant_manager)

        with tune_console.status("[cyan]Running SIMBA optimization...[/cyan]", spinner="dots"):
            try:
                result = runner.run(expert_module, expert_id, trainset)
            except Exception as e:
                tune_console.print(f"[red]Optimization error: {e}[/red]")
                sys.exit(1)

        # Step 4: Display results
        results_table = Table(title="Optimization Results", show_header=True)
        results_table.add_column("Metric", style="cyan")
        results_table.add_column("Value", justify="right")

        results_table.add_row("Before Score", f"{result['before_score']:.2f}")
        results_table.add_row("After Score", f"{result['after_score']:.2f}")
        results_table.add_row("Delta", f"{result['improvement_delta']:+.4f}")
        results_table.add_row("p-value", f"{result['p_value']:.4f}")
        results_table.add_row(
            "Significant",
            "[green]Yes[/green]" if result["is_significant"] else "[red]No[/red]",
        )
        tune_console.print(results_table)

        # Step 5: Deploy prompt
        if result["is_significant"]:
            response = input("Deploy this variant? [y/N] ").strip().lower()
            if response == "y":
                variant_manager.deploy(result["variant_record"].variant_id, expert_id)
                tune_console.print(
                    f"[green]Variant {result['variant_record'].variant_id} deployed[/green]"
                )
            else:
                tune_console.print("[yellow]Variant saved but not deployed.[/yellow]")
        else:
            tune_console.print(
                "[yellow]Improvement not statistically significant. "
                "Variant saved but not deployed.[/yellow]"
            )

        sys.exit(0)

    # Non-interactive mode
    if args.query:
        import json

        # Setup LM Studio
        try:
            setup_dspy(verbose=args.verbose)
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e), "status": "failed"}))
            else:
                print(f"Error: {e}")
            sys.exit(1)

        # Create agent
        agent = ClioAgent(verbose=args.verbose)

        # Ask question
        try:
            result = agent(question=args.query, session_id=args.session)

            if args.json:
                # JSON output
                output = {
                    "question": args.query,
                    "answer": result.answer,
                    "selected_expert": result.selected_expert,
                    "duration_ms": getattr(result, 'duration_ms', 0.0),
                    "session_id": getattr(result, 'session_id', args.session),
                    "status": "success"
                }
                print(json.dumps(output, indent=2))
            else:
                # Human-readable output
                console = Console()
                console.print(f"\n[bold cyan]Question:[/bold cyan] {args.query}")
                console.print(f"[bold green]Router:[/bold green] {result.selected_expert}")
                console.print(Panel(Markdown(result.answer), title="CLIO", border_style="green"))

        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e), "status": "failed"}))
            else:
                console = Console()
                console.print(f"[red]Error: {e}[/red]")
                if args.verbose:
                    import traceback
                    traceback.print_exc()
            sys.exit(1)

    else:
        # Interactive mode
        run_cli(verbose=args.verbose)
