"""Build PowerShell commands whose values are data, never code.

``powershell -Command <script> <value>`` does not bind ``<value>`` to
``$args``: Windows PowerShell joins every argument after ``-Command`` into the
script text, so ``$args[0]`` is empty and the value runs as a statement. Every
value a script needs is therefore embedded as a single-quoted literal, in which
PowerShell expands nothing (a quote is escaped by doubling it).
"""

from __future__ import annotations

from clio_agent.gact.infrastructure.models import CommandSpec


def literal(value: str) -> str:
    """``value`` as a single-quoted PowerShell string literal.

    Raises:
        ValueError: For NUL, CR or LF, which cannot cross a command line.
    """

    if any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError("PowerShell values cannot contain control characters")
    return "'" + value.replace("'", "''") + "'"


def array(values: list[str] | tuple[str, ...]) -> str:
    """A PowerShell array literal of single-quoted strings (``@()`` when empty)."""

    return "@(" + ",".join(literal(value) for value in values) + ")"


def command(script: str, *, timeout_seconds: float = 120.0) -> CommandSpec:
    """One non-interactive PowerShell invocation of ``script``."""

    return CommandSpec(
        program="powershell",
        args=["-NoProfile", "-NonInteractive", "-Command", script],
        timeout_seconds=timeout_seconds,
    )
