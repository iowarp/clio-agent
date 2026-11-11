"""Run install/install.sh against fixture downloads and logging uv/git shims.

Nothing is downloaded or installed: `curl` serves files from a fixture directory
(PyPI's release list, a release tag's pyproject/uv.lock/.python-version), `uv` and
`git` record every call, and the run stops at the final dependency install with exit
code 77 so a test reads exactly what would have been installed, and how
(``TEST_INSTALL_CONTINUE=1`` lets it succeed, to read what the installer writes after).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STOP = 77
# A failed release install rolls back and reports 1; source sync preserves the
# dependency command's status. Both shims still stop at the install boundary.
RELEASE_STOP = 1


def shell_path(path: Path) -> str:
    """A path understood by the platform's Bash interpreter."""
    value = path.as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


_CURL = r"""#!/usr/bin/env bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac
  shift
done
echo "curl $url" >> "$TEST_LOG"
case "$url" in
  https://api.github.com/repos/iowarp/clio-agent/releases*) src="$TEST_FIXTURES/releases.json" ;;
  https://astral.sh/uv/install.sh) src="$TEST_FIXTURES/uv-install.sh" ;;
  https://raw.githubusercontent.com/iowarp/clio-agent/*) rest="${url#https://raw.githubusercontent.com/iowarp/clio-agent/}"; src="$TEST_FIXTURES/$rest" ;;
  *) exit 22 ;;
esac
[ -f "$src" ] || exit 22
if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi
"""

_UV = r"""#!/usr/bin/env bash
echo "uv $* (in $PWD)" >> "$TEST_LOG"
case "$1" in
  run)
    code=""; rest=()
    while [ $# -gt 0 ]; do
      if [ "$1" = -c ]; then code="$2"; shift 2; rest=("$@"); break; fi
      shift
    done
    exec "$TEST_PYTHON" -c "$code" "${rest[@]}" ;;
  export)
    while [ $# -gt 0 ]; do [ "$1" = -o ] && { echo "dspy==3.4.0" > "$2"; }; shift; done
    exit 0 ;;
  venv) exit 0 ;;
  pip|sync) [ -n "${TEST_INSTALL_CONTINUE:-}" ] && exit 0; exit 77 ;;
esac
exit 0
"""

# uv's own installer, for runs without uv: it installs the uv shim once the installer
# has asked for the release's lock (or after 10 s), so the log shows what overlapped it.
_UV_INSTALLER = r"""#!/bin/sh
for _ in $(seq 100); do
  grep -q '^curl https://raw.githubusercontent.com/.*/uv.lock' "$TEST_LOG" && break
  sleep 0.1
done
[ -z "${TEST_UV_INSTALL_FAILS:-}" ] || exit 1
cp "$TEST_FIXTURES/uv" "$UV_INSTALL_DIR/uv" && chmod +x "$UV_INSTALL_DIR/uv"
echo "uv-installer done" >> "$TEST_LOG"
"""

_GIT = r"""#!/usr/bin/env bash
echo "git $*" >> "$TEST_LOG"
if [ "$1" = --exec-path ]; then  # a full git: it has the git-submodule script
  mkdir -p "$TEST_FIXTURES/git-core" && : > "$TEST_FIXTURES/git-core/git-submodule"
  echo "$TEST_FIXTURES/git-core"
  exit 0
fi
if [ "$1" = clone ]; then
  ref=""; dest=""
  args=("$@")
  for ((i=0; i<${#args[@]}; i++)); do
    [ "${args[$i]}" = --branch ] && ref="${args[$((i+1))]}"
  done
  dest="${args[$((${#args[@]}-1))]}"
  case "$ref" in [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) exit 128 ;; esac
  mkdir -p "$dest" && printf '3.14\n' > "$dest/.python-version"
  exit 0
fi
if [ "$1" = init ]; then mkdir -p "${@: -1}"; printf '3.14\n' > "${@: -1}/.python-version"; fi
if [ "$1" = -C ] && [ "$3" = rev-parse ]; then echo main; fi
exit 0
"""

_LN = r"""#!/usr/bin/env bash
echo "ln $*" >> "$TEST_LOG"
[ -x /usr/bin/ln ] && exec /usr/bin/ln "$@"
exec /bin/ln "$@"
"""


def write_release(fixtures: Path, tag: str, *, python: str | None) -> None:
    """A release tag's files as raw.githubusercontent.com would serve them."""
    root = fixtures / tag
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "clio-agent"\n')
    (root / "uv.lock").write_text("version = 1\n")
    if python is not None:
        (root / ".python-version").write_text(f"{python}\n")


def write_releases(fixtures: Path, tags: list[str], *, drafts: tuple[str, ...] = ()) -> None:
    """GitHub's release list, published in the order given (plus an unrelated data release)."""
    releases = [
        {"tag_name": tag, "draft": tag in drafts, "published_at": f"2026-10-{10 + i:02d}T00:00:00Z"}
        for i, tag in enumerate(tags)
    ]
    releases.append({"tag_name": "ndp-scientific-conversations-2026-10-19", "draft": False})
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "releases.json").write_text(json.dumps(releases))


def run_installer(
    tmp_path: Path,
    *args: str,
    script: Path | None = None,
    env: dict[str, str] | None = None,
    system: str = "Linux",
    real_git: bool = False,
    terminal_input: str | None = None,
    uv_installed: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the installer with the shims; return the result and the logged calls.

    ``real_git`` leaves git unshimmed (the host's git on ``/usr/bin``), for tests that
    read what the installer does to a real clone; its calls are then not logged.
    ``terminal_input`` runs it on a terminal of its own (a pseudo-terminal that is its
    controlling terminal, so ``/dev/tty`` answers) with that text typed ahead; the
    result's ``stdout`` is everything the terminal showed. ``uv_installed=False`` starts
    without uv, which the installer then installs with uv's own installer.
    """
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    assert bash is not None
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    uname = f'#!/usr/bin/env bash\nif [ "$1" = -s ]; then echo {system}; else echo arm64; fi\n'
    shimmed = [("curl", _CURL), ("uname", uname), ("ln", _LN)]
    if uv_installed:
        shimmed.append(("uv", _UV))
    else:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir(exist_ok=True)
        (fixtures / "uv").write_text(_UV, newline="\n")
        (fixtures / "uv-install.sh").write_text(_UV_INSTALLER, newline="\n")
    if not real_git:
        shimmed.append(("git", _GIT))
    elif (host_git := shutil.which("git")) and Path(host_git) != Path("/usr/bin/git"):
        # PATH below has /usr/bin first; on a Mac without the Command Line Tools its git
        # is a placeholder that asks to install them. Use the git these tests found.
        shimmed.append(("git", f'#!/bin/sh\nexec "{shell_path(Path(host_git))}" "$@"\n'))
    for name, body in shimmed:
        shim = shims / name
        shim.write_text(body, newline="\n")
        shim.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    base = {
        key: value for key, value in os.environ.items() if not key.startswith(("CLIO_", "GACT_"))
    }
    base.update(
        HOME=shell_path(tmp_path / "home"),
        TEST_LOG=shell_path(log),
        TEST_FIXTURES=shell_path(tmp_path / "fixtures"),
        TEST_PYTHON=shell_path(Path(sys.executable)),
        TEST_PATH=f"{shell_path(shims)}:/usr/bin:/bin",
        CLIO_PREFIX=shell_path(tmp_path / "prefix"),
        CLIO_BIN_DIR=shell_path(tmp_path / "bin"),
    )
    base.update(env or {})
    command = [
        bash,
        "-c",
        'export PATH="$TEST_PATH"; exec bash "$@"',
        "installer-test",
        shell_path(script or ROOT / "install/install.sh"),
        *args,
    ]
    if terminal_input is not None:
        return _on_a_terminal(command, base, terminal_input), log.read_text().splitlines()
    result = subprocess.run(
        command,
        env=base,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, log.read_text().splitlines()


def _on_a_terminal(
    command: list[str], env: dict[str, str], typed: str
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with a pseudo-terminal as its controlling terminal (POSIX only)."""
    import fcntl  # noqa: PLC0415 - POSIX only
    import pty  # noqa: PLC0415
    import termios  # noqa: PLC0415

    leader, follower = pty.openpty()

    def take_terminal() -> None:  # in the child, after setsid: the pty becomes /dev/tty
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(  # noqa: S603 - the test's own command
        command,
        env=env,
        stdin=follower,
        stdout=follower,
        stderr=follower,
        start_new_session=True,
        preexec_fn=take_terminal,  # noqa: PLW1509 - single-threaded child setup
    )
    os.close(follower)
    os.write(leader, typed.encode())
    shown = bytearray()
    while True:
        try:
            chunk = os.read(leader, 65536)
        except OSError:  # EIO: the terminal's last writer exited
            break
        if not chunk:
            break
        shown += chunk
    os.close(leader)
    code = process.wait(timeout=60)
    return subprocess.CompletedProcess(command, code, shown.decode(errors="replace"), "")
