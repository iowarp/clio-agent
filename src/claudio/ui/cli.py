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
ClaudIO Command-Line Interface

Interactive ClaudIO Agent Framework TUI for scientific data I/O assistance.

Features:
- DataExpert agent with ReAct pattern (reasoning + tool calling)
- Rich TUI with syntax highlighting
- Conversation history

Example:
    # Run CLI with LM Studio
    $ uv run src/claudio/ui/cli.py

    # Or from Python
    >>> from claudio.ui.cli import run_cli
    >>> run_cli()
"""

import sys
from typing import Optional
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from rich import print as rprint

# Add src to path
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/claudio/ui/cli.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from claudio.config import setup_dspy
from claudio.claudio import ClaudIO


# ============================================================================
# CLI CLASS
# ============================================================================

class ClaudIOCLI:
    """Interactive CLI for ClaudIO data I/O expert system.

    Demonstrates:
    - ClaudIO agent with DataExpert
    - ReAct agent with MCP tools
    - Observable reasoning traces

    Attributes:
        agent: ClaudIO (main agent instance)
        console: Rich console for pretty output
        history: Conversation history
    """

    def __init__(
        self,
        verbose: bool = False
    ):
        """Initialize ClaudIO CLI.

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
            self.console.print(f"\n[red]❌ Error setting up LM: {e}[/red]")
            self.console.print("\n[yellow]Troubleshooting:[/yellow]")
            self.console.print("- Ensure LM Studio is running at http://100.127.255.164:1234")
            self.console.print("- Ensure LM Studio is running and model is loaded")
            sys.exit(1)

        # Create ClaudIO agent (already fixed above, but keeping for clarity)
        self.agent = ClaudIO(verbose=False)

    def print_banner(self):
        """Print ClaudIO welcome banner."""
        from rich.align import Align

        # ASCII art logo
        logo = """╔═╗╦  ╔═╗╦ ╦╔╦╗╦╔═╗
║  ║  ╠═╣║ ║ ║║║║ ║
╚═╝╩═╝╩ ╩╚═╝═╩╝╩╚═╝"""

        # Create centered logo
        logo_text = Text(logo, style="bold cyan", justify="center")

        # Info section with proper markup
        info = """[dim]Multi-Agent System for Scientific Computing[/dim]

[cyan]Experts:[/cyan] data • hpc • analysis • research • workflow
[green]Local LM:[/green] LM Studio • Ollama • OpenAI

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
            ("/tools", "Show available MCP tools and cache stats"),
            ("/verbose", "Toggle verbose mode (show routing details)"),
            ("/clear", "Clear conversation history"),
            ("/quit, /exit", "Exit ClaudIO"),
        ]

        for cmd, desc in commands:
            help_table.add_row(cmd, desc)

        self.console.print(help_table)

    def print_experts(self):
        """Print available experts and their capabilities."""
        from claudio.experts import get_expert_capabilities

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
A2A Protocol: Enabled

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
            self.console.print(f"\n[green]✓ Cache hit rate ({hit_rate:.1%}) exceeds target (85%)[/green]")
        else:
            self.console.print(f"\n[yellow]⚠ Cache hit rate ({hit_rate:.1%}) below target (85%)[/yellow]")

    def print_tools(self):
        """Display available IOWarp MCP tools and cache statistics."""
        from claudio.tools.mcp_connector import IOWarpMCPTools

        # Initialize connector
        mcp_tools = IOWarpMCPTools()

        # Get available servers
        servers = mcp_tools.get_available_servers()

        # Create table
        table = Table(title="Available IOWarp MCP Servers")
        table.add_column("Server", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Tools", style="yellow")

        for server in servers:
            # Display server info (actual tool count would require connection)
            table.add_row(server.upper(), "Ready", "~5-10 tools")

        self.console.print(table)

        # Show cache stats if ARC enabled
        try:
            if hasattr(self.agent, 'arc') and self.agent.arc:
                stats = self.agent.arc.get_tool_cache_stats()
                info = f"""[bold]Tool Cache Statistics[/bold]

Hit Rate: {stats['tool_cache_hit_rate']:.2%}
Hits: {stats['tool_cache_hits']}
Misses: {stats['tool_cache_misses']}
Cache Size: {stats['tool_cache_size']}
Target: {stats['target_hit_rate']:.0%}

{"[green]✓ Above target[/green]" if stats['tool_cache_hit_rate'] >= stats['target_hit_rate'] else "[yellow]⚠ Below target[/yellow]"}"""
                self.console.print(Panel(info, title="Cache Performance", border_style="blue"))
        except Exception as e:
            self.console.print(f"[dim]Cache stats unavailable: {e}[/dim]")

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
            self.console.print("[green]✓ History cleared[/green]")
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

        elif cmd == "/verbose":
            self.verbose = not self.verbose
            status = "enabled" if self.verbose else "disabled"
            self.console.print(f"[green]✓ Verbose mode {status}[/green]")
            return True

        elif cmd in ["/quit", "/exit", "/q"]:
            self.console.print("\n[cyan]Thanks for using ClaudIO![/cyan]\n")
            sys.exit(0)

        return False

    def ask_question(self, question: str) -> dict:
        """Ask ClaudIO a question via intelligent agent orchestration.

        Flow:
            1. ClaudIO (ChainOfThought) → selects expert
            2. Expert (ReAct) → reasons + calls tools → answer
            3. Display results with reasoning traces

        Args:
            question: User's question

        Returns:
            Dictionary with result including expert, answer, reasoning
        """
        # Show processing spinner
        with self.console.status(
            f"[#00B4FF]Analyzing with ClaudIO agents...[/#00B4FF]",
            spinner="dots"
        ):
            result = self.agent(question=question)

        return {
            "question": question,
            "expert": result.selected_expert,
            "answer": result.answer,
            "routing_reasoning": getattr(result, 'routing_reasoning', ''),
            "expert_reasoning": getattr(result, 'expert_reasoning', ''),
            "tool_calls": getattr(result, 'tool_calls', [])
        }

    def run(self):
        """Run interactive CLI loop with enhanced UX."""
        from prompt_toolkit import prompt as pt_prompt
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from rich.live import Live
        from rich.spinner import Spinner

        self.print_banner()
        self.console.print("\n[bold green]●[/bold green] Ready  [dim]|[/dim]  Type [cyan]/help[/cyan] for commands\n")

        # Setup prompt toolkit for better input
        history = InMemoryHistory()

        while True:
            try:
                # Get user input with history and auto-suggest (POC pattern)
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

                # Ask question via ClaudIO agent
                result = self.ask_question(user_input)

                # Show verbose info (routing details)
                if self.verbose:
                    self.console.print(f"\n[#00B4FF]→ Expert Selected:[/#00B4FF] [bold]{result['expert']}[/bold]")

                    if result['routing_reasoning']:
                        self.console.print(f"[dim]→ Routing: {result['routing_reasoning'][:150]}...[/dim]\n")

                    if result.get('tool_calls'):
                        self.console.print(f"[#FF8800]→ Tools Used:[/#FF8800] {len(result['tool_calls'])} calls\n")

                # Show answer with POC-style panel
                expert_label = result['expert'].upper()
                self.console.print(
                    Panel(
                        Markdown(result['answer']),
                        title=f"[bold #00FF88]ClaudIO[/bold #00FF88] [dim]via {expert_label} Expert[/dim]",
                        subtitle=f"[dim]Intelligent multi-agent routing[/dim]" if not self.verbose else None,
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
                self.console.print(f"\n[red]❌ Error: {e}[/red]")
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
    """Run ClaudIO CLI with agent framework.

    Args:
        verbose: Show routing reasoning and tool calls

    Example:
        >>> run_cli()  # Uses LM Studio
    """
    cli = ClaudIOCLI(
        verbose=verbose
    )
    cli.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="ClaudIO: Agent Framework for Scientific Computing (IOWarp Intelligence Layer)"
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

    args = parser.parse_args()

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
                print(f"❌ Error: {e}")
            sys.exit(1)

        # Create agent
        agent = ClaudIO(verbose=args.verbose)

        # Ask question
        try:
            result = agent(question=args.query, session_id=args.session)

            if args.json:
                # JSON output
                output = {
                    "question": args.query,
                    "selected_expert": result.selected_expert,
                    "answer": result.answer,
                    "confidence": getattr(result, 'confidence', 0.0),
                    "duration_ms": getattr(result, 'duration_ms', 0.0),
                    "status": "success"
                }
                print(json.dumps(output, indent=2))
            else:
                # Human-readable output
                console = Console()
                console.print(f"\n[bold cyan]Question:[/bold cyan] {args.query}")
                console.print(f"[bold green]Expert:[/bold green] {result.selected_expert}")
                console.print(f"[bold yellow]Confidence:[/bold yellow] {getattr(result, 'confidence', 0.0):.2f}\n")
                console.print(Panel(Markdown(result.answer), title="Answer", border_style="green"))

        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e), "status": "failed"}))
            else:
                console = Console()
                console.print(f"[red]❌ Error: {e}[/red]")
                if args.verbose:
                    import traceback
                    traceback.print_exc()
            sys.exit(1)

    else:
        # Interactive mode
        run_cli(verbose=args.verbose)
