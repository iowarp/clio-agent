"""Git's ssh inside the fence: ``python -I net_git_ssh.py <shim-dir> <ssh args>``.

The fence sets this as ``GIT_SSH_COMMAND``, which outranks every other Git ssh setting, so a
person's ``core.sshCommand`` or ``GIT_SSH`` can no longer dial port 22 past the proxy. It runs
the ssh command Git would have run (the person's ``GIT_SSH_COMMAND``, ``core.sshCommand`` or
``GIT_SSH``, else ``ssh``) through :mod:`net_connect`'s ssh launcher, so a configured ssh keeps
its options and still crosses the proxy.

Like :mod:`net_connect`, it imports only the standard library and runs isolated (``-I``), so it
loads its sibling launcher by file path.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

#: The person's own ``GIT_SSH_COMMAND`` and ``GIT_SSH``, kept while the fence sets its own.
PERSON_GIT_SSH_COMMAND = "CLIO_PERSON_GIT_SSH_COMMAND"
PERSON_GIT_SSH = "CLIO_PERSON_GIT_SSH"
_LAUNCHER = "clio_agent.runtime.net_connect"


def _launcher() -> ModuleType:
    """:mod:`net_connect`, imported, or loaded from beside this file when run isolated."""
    loaded = sys.modules.get(_LAUNCHER) or sys.modules.get("net_connect")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(
        "net_connect", Path(__file__).with_name("net_connect.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError("net_connect.py is missing beside net_git_ssh.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["net_connect"] = module
    spec.loader.exec_module(module)
    return module


def _git_config_ssh_command() -> str:
    """``core.sshCommand`` as Git resolves it from this directory, or ``""``."""
    git = shutil.which("git")
    if not git:
        return ""
    try:
        done = subprocess.run(
            [git, "config", "--get", "core.sshCommand"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def person_git_ssh() -> list[str]:
    """The ssh command Git would run without the fence, as an argv prefix.

    Git's own order: ``GIT_SSH_COMMAND``, then ``core.sshCommand``, then ``GIT_SSH``, then
    ``ssh``. The two commands are shell text, split the way Git's shell would split them.
    """
    command = os.environ.get(PERSON_GIT_SSH_COMMAND, "") or _git_config_ssh_command()
    if command:
        return shlex.split(command, posix=True)
    program = os.environ.get(PERSON_GIT_SSH, "")
    return [program] if program else ["ssh"]


def git_ssh_argv(shim_dir: str, args: list[str]) -> list[str]:
    """Git's ssh call: the person's ssh command and its options, through the launcher."""
    launcher = _launcher()
    program, *options = person_git_ssh() or ["ssh"]
    real = program if os.path.dirname(program) else launcher.real_program(program, shim_dir)
    if not real:
        return []
    return launcher.ssh_argv(real, shim_dir, [*options, *args])


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: net_git_ssh.py <shim-dir> <ssh args>\n")
        return 2
    command = git_ssh_argv(argv[0], argv[1:])
    if not command:
        sys.stderr.write(f"ssh is not installed on this machine ({' '.join(person_git_ssh())})\n")
        return 127
    return _launcher()._run(command, async_stdin=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
