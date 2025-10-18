#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
#   "rich>=13.7.0",
#   "prompt-toolkit>=3.0.43",
#   "openai>=1.12.0",
# ]
# ///

"""
Warpio DSPy POC - Chat TUI

Interactive chat interface with DSPy orchestrator.
Run with: uv run chat.py
"""

import dspy
import os
import sys
from typing import List, Dict
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

# Import orchestrator (inline for UV script)
sys.path.insert(0, os.path.dirname(__file__))
from orchestrator import WarpioOrchestrator
from experts import get_expert_capabilities
from config import setup_dspy, LMStudioConfig


# ============================================================================
# CHAT SESSION
# ============================================================================

class ChatSession:
    """Manages chat session state and history."""

    def __init__(self):
        self.history: List[Dict[str, str]] = []
        self.console = Console()
        self.orchestrator = None

    def add_message(self, role: str, content: str, expert: str = None):
        """Add message to history."""
        self.history.append({
            "role": role,
            "content": content,
            "expert": expert
        })

    def get_history_context(self) -> str:
        """Get recent history for context."""
        recent = self.history[-5:]  # Last 5 messages
        return "\n".join([
            f"{msg['role']}: {msg['content']}"
            for msg in recent
        ])


# ============================================================================
# TUI COMPONENTS
# ============================================================================

def print_header(console: Console):
    """Print Warpio header."""
    header = """
    ╦ ╦╔═╗╦═╗╔═╗╦╔═╗  ╔╦╗╔═╗╔═╗╦ ╦  ╔═╗╔═╗╔═╗
    ║║║╠═╣╠╦╝╠═╝║║ ║   ║║╚═╗╠═╝╚╦╝  ╠═╝║ ║║
    ╚╩╝╩ ╩╩╚═╩  ╩╚═╝  ═╩╝╚═╝╩   ╩   ╩  ╚═╝╚═╝
    """
    console.print(Panel(
        Text(header, style="bold cyan", justify="center"),
        title="[bold white]Warpio DSPy Proof of Concept[/]",
        subtitle="[dim]Type 'help' for commands, 'exit' to quit[/]",
        border_style="cyan"
    ))


def print_experts(console: Console):
    """Print available experts."""
    table = Table(title="Available Experts", border_style="cyan")
    table.add_column("Expert", style="bold yellow")
    table.add_column("Description", style="white")

    capabilities = get_expert_capabilities()
    for expert_id, caps in capabilities.items():
        table.add_row(caps["name"], caps["description"])

    console.print(table)


def print_help(console: Console):
    """Print help message."""
    help_text = """
**Available Commands:**

- `help` - Show this help message
- `experts` - List available experts
- `clear` - Clear screen
- `history` - Show conversation history
- `exit` or `quit` - Exit the chat

**Tips:**

- Ask questions naturally - the orchestrator will route to the right expert
- Questions about code → Code Helper
- Questions about data → Data Analyst
- Everything else → General Assistant
"""
    console.print(Panel(Markdown(help_text), border_style="yellow", title="Help"))


def print_history(console: Console, session: ChatSession):
    """Print conversation history."""
    if not session.history:
        console.print("[dim]No history yet[/]")
        return

    console.print("\n[bold cyan]Conversation History:[/]\n")
    for i, msg in enumerate(session.history, 1):
        role_color = "green" if msg["role"] == "User" else "blue"
        console.print(f"[bold {role_color}]{i}. {msg['role']}[/]", end="")
        if msg.get("expert"):
            console.print(f" [dim](via {msg['expert']})[/]", end="")
        console.print(f": {msg['content'][:100]}...")


# ============================================================================
# MAIN CHAT LOOP
# ============================================================================

def main():
    """Main chat application."""
    console = Console()
    session = ChatSession()

    # Configure DSPy with LM Studio
    try:
        console.print("\n[cyan]Initializing Warpio with LM Studio...[/]\n")
        lm = setup_dspy(use_lm_studio=True)
        session.orchestrator = WarpioOrchestrator()
    except Exception as e:
        console.print(f"[bold red]Error initializing DSPy: {e}[/]")
        console.print("\n[yellow]Troubleshooting:[/]")
        console.print("1. Make sure LM Studio is running at http://127.0.0.1:1234")
        console.print("2. Ensure model 'openai/gpt-oss-20b' is loaded")
        console.print("3. Check that the API server is enabled in LM Studio\n")
        sys.exit(1)

    # Print header
    console.clear()
    print_header(console)
    console.print("\n[green]✓[/] Warpio DSPy initialized successfully!\n")

    # Setup prompt toolkit
    history = InMemoryHistory()

    # Main chat loop
    try:
        while True:
            try:
                # Get user input
                user_input = pt_prompt(
                    "You: ",
                    history=history,
                    auto_suggest=AutoSuggestFromHistory(),
                ).strip()

                if not user_input:
                    continue

                # Handle commands
                if user_input.lower() in ['exit', 'quit']:
                    console.print("\n[cyan]Goodbye! 👋[/]\n")
                    break

                elif user_input.lower() == 'help':
                    print_help(console)
                    continue

                elif user_input.lower() == 'experts':
                    print_experts(console)
                    continue

                elif user_input.lower() == 'clear':
                    console.clear()
                    print_header(console)
                    continue

                elif user_input.lower() == 'history':
                    print_history(console, session)
                    continue

                # Add to history
                session.add_message("User", user_input)

                # Process with orchestrator
                with Live(
                    Spinner("dots", text="[cyan]Thinking...[/]"),
                    console=console,
                    transient=True
                ):
                    result = session.orchestrator(question=user_input)

                # Display response
                console.print(f"\n[bold blue]Warpio[/] [dim](via {result.selected_expert})[/]:")
                console.print(Panel(
                    Markdown(result.answer),
                    border_style="blue",
                    subtitle=f"[dim]Routing: {result.routing_reasoning[:50]}...[/]"
                ))
                console.print()

                # Add to history
                session.add_message("Warpio", result.answer, result.selected_expert)

            except KeyboardInterrupt:
                console.print("\n\n[yellow]Use 'exit' to quit gracefully.[/]\n")
                continue

            except EOFError:
                # Ctrl+D pressed
                console.print("\n[cyan]Goodbye! 👋[/]\n")
                break

            except Exception as e:
                console.print(f"\n[bold red]Error: {e}[/]\n")
                continue

    except KeyboardInterrupt:
        # Final Ctrl+C - exit immediately
        console.print("\n\n[cyan]Goodbye! 👋[/]\n")
        sys.exit(0)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()
