"""The remote steps of a CLIO deployment: claim, install, status, teardown.

A remote host can already run a CLIO the user did not mention: a leftover
from an earlier deploy, or another install on a shared login node holding the
API port. Starting beside it on another port leaves two servers behind;
failing against it leaves the user stuck. Before installing, the deploy
therefore *claims* the port:

* nothing listens: proceed;
* this install's own server listens, is healthy, and runs the target
  version: **adopt** it (the install and start steps are skipped);
* any other CLIO server listens (another install prefix, an old version, a
  hung server): **stop** it and report ``Stopped an old CLIO (pid N, path)``;
* anything that is not a CLIO server listens: fail with a typed line naming
  it. Unrelated processes are never touched.

A CLIO server is recognized only by what CLIO's own launcher leaves: the
command line ``<prefix>/clio-agent/.venv/bin/clio-agent serve`` confirmed by
that prefix's pidfile for this host (``clio-server.<host>.pid``; older
launchers wrote ``clio-server.pid``) or the process's working directory
``<prefix>/clio-agent``. The port alone never identifies a process.

Health checks of this node pass ``--noproxy``: cluster nodes often export
``http_proxy``, and a proxied check of ``127.0.0.1`` is answered by the proxy.

The install is exactly this CLIO's own clio-agent version, from PyPI
(:func:`install_command`), so the desktop and the remote always run the same
release. A version PyPI does not have (a development build) fails at once
with one plain line instead of installing something else. Everything the
install writes (the launcher, uv, its cache, Python and tools) stays under
the install root.

A server's state is read from a real answer: :func:`status_command` asks the
server's own ``/v1/health`` on this node and reports ``running`` only when
the CLIO API answered.

When the deploy then fails or is cancelled, :func:`teardown_command` removes
what this deploy started: the server (stopped gracefully, so it releases the
machine's shared clio-core daemon, which stops when its last client leaves),
its pid file, and the install root itself when this deploy created it.

Every step carries a ``# clio-deploy:<step>`` tag so the desktop can show it
as its own stage; the claim ends with one ``clio-deploy result=...`` line
that :func:`parse_claim` reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from clio_agent.gact.infrastructure.models import CommandSpec

# Shared by both scripts: resolve the install root the same way the install
# and start commands do, find a TCP listener's pid, and recognize a CLIO
# server process.
_COMMON = r"""
root="$1"; if [ -z "$root" ]; then root="$HOME/.local/share/clio"; fi
port="$2"
host_id="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo localhost)"
say() { printf '==> %s\n' "$*"; }
fail() { printf 'xx %s\n' "$*"; exit 75; }
listener_pid() {
  local p=""
  if command -v ss >/dev/null 2>&1; then
    p="$(ss -Hltnp "sport = :$1" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n1)"
  fi
  if [ -z "$p" ] && command -v lsof >/dev/null 2>&1; then
    p="$(lsof -nP -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -n1)"
  fi
  printf '%s' "$p"
}
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
cmdline_of() { tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null; }
# A process exists and has not exited (a zombie awaiting its parent is gone).
alive() {
  kill -0 "$1" 2>/dev/null || return 1
  ! grep -q '^State:[[:space:]]*Z' "/proc/$1/status" 2>/dev/null
}
# Echo the install prefix of a CLIO server process, or fail (return 1).
clio_prefix_of() {
  local pid="$1" line script prefix recorded cwd
  line="$(cmdline_of "$pid")"
  script="$(printf '%s\n' "$line" | grep -oE '[^ ]*/clio-agent/\.venv/bin/clio-agent serve( |$)' | head -n1)"
  [ -n "$script" ] || return 1
  script="${script% serve*}"
  prefix="${script%/clio-agent/.venv/bin/clio-agent}"
  recorded="$(cat "$prefix/clio-server.$host_id.pid" 2>/dev/null || cat "$prefix/clio-server.pid" 2>/dev/null || true)"
  cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
  if [ "$recorded" = "$pid" ] || [ "$cwd" = "$prefix/clio-agent" ]; then
    printf '%s' "$prefix"
    return 0
  fi
  return 1
}
# Stop a CLIO server: its process group (the launcher starts it with setsid),
# a graceful window so it releases clio-core, then a hard kill.
stop_clio_server() {
  local pid="$1" i
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for i in $(seq 1 120); do
    alive "$pid" || return 0
    sleep 0.25
  done
  kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  for i in $(seq 1 20); do
    alive "$pid" || return 0
    sleep 0.25
  done
  return 1
}
real() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
"""

_CLAIM = (
    "# clio-deploy:claim\n"
    + _COMMON
    + r"""
version="$3"
existing_root=0; [ -d "$root" ] && existing_root=1
if ! port_busy "$port"; then
  say "Port $port is free"
  printf 'clio-deploy result=free existing_root=%s\n' "$existing_root"
  exit 0
fi
pid="$(listener_pid "$port")"
if [ -z "$pid" ]; then
  fail "Port $port is used by a process this account cannot inspect (another user's program)"
fi
owner="$(clio_prefix_of "$pid")" || fail "Port $port is used by another program (pid $pid: $(cmdline_of "$pid" | cut -c1-160))"
if [ "$(real "$owner")" = "$(real "$root")" ]; then
  installed="$("$root/clio-agent/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("clio-agent"))' 2>/dev/null || true)"
  code="$(curl --noproxy '*' -sS -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/v1/health" 2>/dev/null || true)"
  if [ "$installed" = "$version" ] && { [ "$code" = "200" ] || [ "$code" = "503" ]; }; then
    say "Reusing the running CLIO (pid $pid, $owner)"
    printf 'clio-deploy result=adopted existing_root=%s pid=%s\n' "$existing_root" "$pid"
    exit 0
  fi
fi
stop_clio_server "$pid" || fail "An old CLIO (pid $pid, $owner) did not stop"
for i in $(seq 1 20); do
  port_busy "$port" || break
  sleep 0.25
done
port_busy "$port" && fail "Port $port is still in use after stopping an old CLIO (pid $pid, $owner)"
say "Stopped an old CLIO (pid $pid, $owner)"
printf 'clio-deploy result=stopped existing_root=%s pid=%s\n' "$existing_root" "$pid"
"""
)

_TEARDOWN = (
    "# clio-deploy:teardown\n"
    + _COMMON
    + r"""
purge_root="$3"
did=0
pidfile="$root/clio-server.$host_id.pid"
[ -f "$pidfile" ] || pidfile="$root/clio-server.pid"
pid="$(cat "$pidfile" 2>/dev/null || true)"
if [ -n "$pid" ] && alive "$pid"; then
  owner="$(clio_prefix_of "$pid" || true)"
  if [ -n "$owner" ] && [ "$(real "$owner")" = "$(real "$root")" ]; then
    # Graceful first: the server releases this machine's shared clio-core
    # daemon, which stops once its last client has gone.
    stop_clio_server "$pid" || fail "The CLIO this deploy started (pid $pid) did not stop"
    say "Stopped the CLIO this deploy started (pid $pid)"
    did=1
  fi
fi
rm -f "$pidfile"
if [ "$purge_root" = "1" ] && [ -f "$root/.clio-managed-install" ]; then
  rm -rf -- "$root"
  say "Removed the install this deploy created ($root)"
  did=1
fi
[ "$did" = "1" ] || say "Nothing to clean up"
printf 'clio-deploy result=cleaned\n'
"""
)


@dataclass(frozen=True)
class ClaimResult:
    """What the claim step found on the API port."""

    result: Literal["free", "adopted", "stopped"]
    existing_root: bool


_RESULT_LINE = re.compile(r"clio-deploy result=(\w+)((?: \w+=\S+)*)")


def claim_command(root: str, port: int, version: str) -> CommandSpec:
    """The adopt-or-stop step that runs before installing."""

    return CommandSpec(
        program="bash",
        args=["-lc", _CLAIM, "clio", root, str(port), version],
        timeout_seconds=90,
    )


def teardown_command(root: str, port: int, *, purge_root: bool) -> CommandSpec:
    """Undo what a failed or cancelled deploy started."""

    return CommandSpec(
        program="bash",
        args=["-lc", _TEARDOWN, "clio", root, str(port), "1" if purge_root else "0"],
        timeout_seconds=90,
    )


# Resolve the install root the same way every step does, and keep the
# launcher and agent data inside it, so removing the root removes the install.
LAUNCHER_PRELUDE = (
    'root="$1"; if [ -z "$root" ]; then root="$HOME/.local/share/clio"; fi; bin="$root/bin"; '
    'export CLIO_PREFIX="$root" CLIO_BIN_DIR="$bin" CLIO_DATA_DIR="$root/data"; '
)

PYPI_RELEASE_URL = "https://pypi.org/pypi/clio-agent/{version}/json"
INSTALLER_URL = "https://raw.githubusercontent.com/iowarp/clio-agent/v{version}/install/install.sh"

_INSTALL = (
    "# clio-deploy:install\n"
    + LAUNCHER_PRELUDE
    + r"""
set -o pipefail
version="$2"
export UV_INSTALL_DIR="$bin" UV_PYTHON_INSTALL_DIR="$root/uv-python" UV_CACHE_DIR="$root/uv-cache"
export UV_TOOL_DIR="$root/uv-tools" UV_TOOL_BIN_DIR="$bin" UV_NO_MODIFY_PATH=1
export PATH="$bin:$PATH"
mkdir -p "$bin" || exit 75
touch "$root/.clio-managed-install"
# The remote runs exactly this CLIO's version; nothing else is installed.
code="$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "$3" 2>/dev/null || true)"
case "$code" in
  200) ;;
  404)
    printf "CLIO %s isn't published; deploy from a released CLIO.\n" "$version"
    exit 78
    ;;
  *)
    printf 'Could not reach PyPI to check CLIO %s (HTTP %s).\n' "$version" "${code:-none}"
    exit 75
    ;;
esac
if ! command -v uv >/dev/null 2>&1; then
  printf '==> Installing uv into %s\n' "$bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh || { printf 'Could not install uv.\n'; exit 75; }
fi
export CLIO_VERSION="$version" CLIO_INSTALLER_REF="v$version"
curl -fsSL "$4" | bash
"""
)


def install_command(root: str, version: str) -> CommandSpec:
    """Install exactly ``version`` of clio-agent from PyPI under ``root``.

    Checks PyPI first: a version it does not have fails the step with one
    plain line (``CLIO 0.9.4.99 isn't published; deploy from a released
    CLIO.``). Then runs that release's own installer (its tag on GitHub)
    with the version pinned; any failure fails the step.
    """

    return CommandSpec(
        program="bash",
        args=[
            "-lc",
            _INSTALL,
            "clio",
            root,
            version,
            PYPI_RELEASE_URL.format(version=version),
            INSTALLER_URL.format(version=version),
        ],
        timeout_seconds=900,
    )


_STATUS = (
    "# clio-deploy:status\n"
    + LAUNCHER_PRELUDE
    + r"""
code="$(curl --noproxy '*' -sS -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$2/v1/health" 2>/dev/null || true)"
case "$code" in
  200|503) echo running ;;
  *) echo stopped ;;
esac
"""
)


def status_command(root: str, port: int) -> CommandSpec:
    """``running`` when this node's CLIO API answers ``/v1/health``, else ``stopped``.

    A 503 is a CLIO that answered while a dependency is still coming up; any
    other outcome (nothing listening, a timeout) is ``stopped``.
    """

    return CommandSpec(
        program="bash",
        args=["-lc", _STATUS, "clio", root, str(port)],
        timeout_seconds=30,
    )


def parse_claim(stdout: str) -> ClaimResult | None:
    """Read the claim step's result line, or None when it has none."""

    for line in reversed(stdout.splitlines()):
        match = _RESULT_LINE.search(line)
        if match and match.group(1) in {"free", "adopted", "stopped"}:
            fields = dict(item.split("=", 1) for item in match.group(2).split())
            return ClaimResult(
                result=match.group(1),  # type: ignore[arg-type]
                existing_root=fields.get("existing_root") == "1",
            )
    return None
