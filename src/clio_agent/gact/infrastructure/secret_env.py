"""Hand a secret to a launched process through its environment, read from stdin.

A command's arguments are public on a shared host: any user of a cluster login
node can read every process's command line (``ps``), and a container engine
keeps the ``run`` arguments. So a secret (a model server's API key) never goes
in an argument. The launch command is wrapped instead:

- POSIX: ``sh -c 'IFS= read -r clio_secret; export NAME="$clio_secret"; ...;
  exec "$@"' sh <program> <args...>`` with the secret on stdin. ``sh`` reads
  it into its own environment and ``exec``s the real command, which inherits
  it; a Docker/Podman ``run`` then takes the variable BY NAME (``--env NAME``)
  and Apptainer takes ``APPTAINERENV_NAME`` (honoured even with
  ``--cleanenv``).
- Windows: one PowerShell invocation reads stdin into ``$env:NAME`` and runs
  the command, which inherits it.

The Desktop SSH transport delivers stdin with the shell builtin ``printf``
into a non-interactive ``sh -s`` (no process, no history); the local executor
passes it as the child's stdin.
"""

from __future__ import annotations

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.models import CommandSpec

#: The variable the wrapper reads the secret into (never exported itself).
_HOLDER = "clio_secret"


def with_secret_env(spec: CommandSpec, variable: str, value: str, *, windows: bool) -> CommandSpec:
    """``spec`` run with ``variable`` set to ``value`` in its environment, never its arguments.

    Args:
        spec: The command that needs the secret (its program and arguments are
            run unchanged).
        variable: The environment variable to set, e.g. ``LLAMA_API_KEY`` or
            ``APPTAINERENV_LLAMA_API_KEY``.
        value: The secret.
        windows: Whether the command runs on a Windows host.

    Raises:
        ValueError: For an empty secret, a secret with a line break or NUL
            (it is read as one line), or a command that already uses stdin.
    """

    if not value or any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError("a secret must be one non-empty line")
    if not variable.replace("_", "").isalnum():
        raise ValueError(f"not an environment variable name: {variable!r}")
    if spec.stdin:
        raise ValueError("the command already reads stdin")
    if windows:
        script = (
            f"$env:{variable} = [Console]::In.ReadLine(); "
            f"& {powershell.literal(spec.program)} {powershell.array(spec.args)}; "
            "exit $LASTEXITCODE"
        )
        wrapped = powershell.command(script, timeout_seconds=spec.timeout_seconds)
    else:
        wrapped = CommandSpec(
            program="sh",
            args=[
                "-c",
                f'IFS= read -r {_HOLDER}; export {variable}="${_HOLDER}"; unset {_HOLDER}; '
                'exec "$@"',
                "sh",
                spec.program,
                *spec.args,
            ],
            timeout_seconds=spec.timeout_seconds,
        )
    return wrapped.model_copy(
        update={
            "stdin": value + "\n",
            "scope": spec.scope,
            "settle_seconds": spec.settle_seconds,
            "allowed_exit_codes": list(spec.allowed_exit_codes),
        }
    )


__all__ = ["with_secret_env"]
