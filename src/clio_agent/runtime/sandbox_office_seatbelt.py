"""macOS Seatbelt policy for CLIO's own LibreOffice worker (iowarp/clio-agent#2139).

LibreOffice's macOS port has one window-system plugin (``libvclplug_osxlo``), and even
``--headless`` it creates ``NSApplication``, which registers the process with the window
server and LaunchServices. Codex's Seatbelt profile denies those two mach services and has
no key to allow a service, so a fenced ``soffice`` aborts in ``_RegisterApplication``
(``SAL_USE_VCLPLUGIN=svp`` does not help: the macOS build ships no svp plugin).

Only CLIO's document worker (``prepare_document`` render/recalculate and the PDF rendition
of an Office file) runs under this policy, with a fixed argv. Agent shell commands keep the
Codex fence: the LaunchServices service can open other applications outside any sandbox.

The policy is Codex's own base policy (read-anywhere, deny-by-default), the same write
territory and protected storage roots as :func:`sandbox_codex.synthesize_codex_profile`,
AF_UNIX sockets (LibreOffice's single-instance pipe, as Codex allows with networking on),
no IP networking, and exactly the mach services in :data:`OFFICE_MACH_SERVICES`.

LibreOffice also creates its single-instance pipe ``OSL_PIPE_<uid>_SingleOfficeIPC_<hash>``
in ``/tmp``, else ``/var/tmp``, else its ``OSL_SOCKET_PATH`` bootstrap variable (default
``/``). The fence makes all three read-only, and ``soffice`` then exits 0 at once without
converting anything. Each fenced launch therefore gets a fresh private directory, short
enough for ``sun_path``, granted as one more write root and named by ``OSL_SOCKET_PATH``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Sequence

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: The only additions to Codex's base policy. Each one was required: without the window
#: server ``soffice`` aborts (exit 134); without LaunchServices it hangs in registration.
OFFICE_MACH_SERVICES: tuple[str, ...] = (
    # NSApplication's window-server connection, made even in headless mode.
    "com.apple.windowserver.active",
    # _RegisterApplication: LaunchServices check-in for any AppKit process.
    "com.apple.coreservices.launchservicesd",
)

# Codex 0.167 ``seatbelt_base_policy.sbpl`` plus its unrestricted-read preferences block,
# verbatim (Apache-2.0, https://github.com/openai/codex). Keep in step with the bundled
# Codex when it changes.
_CODEX_BASE_POLICY = r"""
(version 1)

; inspired by Chrome's sandbox policy:
; https://source.chromium.org/chromium/chromium/src/+/main:sandbox/policy/mac/common.sb;l=273-319;drc=7b3962fe2e5fc9e2ee58000dc8fbf3429d84d3bd
; https://source.chromium.org/chromium/chromium/src/+/main:sandbox/policy/mac/renderer.sb;l=64;drc=7b3962fe2e5fc9e2ee58000dc8fbf3429d84d3bd

; start with closed-by-default
(deny default)

; child processes inherit the policy of their parent
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))

; process-info
(allow process-info* (target same-sandbox))

(allow file-write-data
  (require-all
    (path "/dev/null")
    (vnode-type CHARACTER-DEVICE)))

; sysctls permitted.
(allow sysctl-read
  (sysctl-name "hw.activecpu")
  (sysctl-name "hw.busfrequency_compat")
  (sysctl-name "hw.byteorder")
  (sysctl-name "hw.cacheconfig")
  (sysctl-name "hw.cachelinesize_compat")
  (sysctl-name "hw.cpufamily")
  (sysctl-name "hw.cpufrequency_compat")
  (sysctl-name "hw.cputype")
  (sysctl-name "hw.l1dcachesize_compat")
  (sysctl-name "hw.l1icachesize_compat")
  (sysctl-name "hw.l2cachesize_compat")
  (sysctl-name "hw.l3cachesize_compat")
  (sysctl-name "hw.logicalcpu_max")
  (sysctl-name "hw.machine")
  (sysctl-name "hw.model")
  (sysctl-name "hw.memsize")
  (sysctl-name "hw.ncpu")
  (sysctl-name "hw.nperflevels")
  ; Chrome locks these CPU feature detection down a bit more tightly,
  ; but mostly for fingerprinting concerns which isn't an issue for codex.
  (sysctl-name-prefix "hw.optional.arm.")
  (sysctl-name-prefix "hw.optional.armv8_")
  (sysctl-name "hw.packages")
  (sysctl-name "hw.pagesize_compat")
  (sysctl-name "hw.pagesize")
  (sysctl-name "hw.physicalcpu")
  (sysctl-name "hw.physicalcpu_max")
  (sysctl-name "hw.logicalcpu")
  (sysctl-name "hw.cpufrequency")
  (sysctl-name "hw.tbfrequency_compat")
  (sysctl-name "hw.vectorunit")
  (sysctl-name "machdep.cpu.brand_string")
  (sysctl-name "kern.argmax")
  (sysctl-name "kern.hostname")
  (sysctl-name "kern.maxfilesperproc")
  (sysctl-name "kern.maxproc")
  (sysctl-name "kern.osproductversion")
  (sysctl-name "kern.osrelease")
  (sysctl-name "kern.ostype")
  (sysctl-name "kern.osvariant_status")
  (sysctl-name "kern.osversion")
  (sysctl-name "kern.secure_kernel")
  ; Python's ProcessPoolExecutor queries this through sysconf(_SC_SEM_NSEMS_MAX).
  (sysctl-name "kern.sysv.semmns")
  (sysctl-name "kern.usrstack64")
  (sysctl-name "kern.version")
  (sysctl-name "sysctl.proc_cputype")
  (sysctl-name "vm.loadavg")
  (sysctl-name-prefix "hw.perflevel")
  (sysctl-name-prefix "kern.proc.pgrp.")
  (sysctl-name-prefix "kern.proc.pid.")
  (sysctl-name-prefix "net.routetable.")
)

; Allow Java to read some CPU info. This is misclassified as a "write" because
; userspace passes a memory buffer to the sysctl, but conceptually it is a read.
(allow sysctl-write
  (sysctl-name "kern.grade_cputype"))

; IOKit
(allow iokit-open
  (iokit-registry-entry-class "RootDomainUserClient")
)

; needed to look up user info, see https://crbug.com/792228
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
)

; Needed for python multiprocessing on MacOS for the SemLock
(allow ipc-posix-sem)

; Needed for PyTorch/libomp on macOS to register OpenMP runtimes.
(allow ipc-posix-shm-read-data
  ipc-posix-shm-write-create
  ipc-posix-shm-write-unlink
  (ipc-posix-name-regex #"^/__KMP_REGISTERED_LIB_[0-9]+$"))

(allow mach-lookup
  (global-name "com.apple.PowerManagement.control")
)

; allow openpty()
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write*
  (require-all
    (regex #"^/dev/ttys[0-9]+")
    (extension "com.apple.sandbox.pty")))
; PTYs created before entering seatbelt may lack the extension; allow ioctl
; on those slave ttys so interactive shells detect a TTY and remain functional.
(allow file-ioctl (regex #"^/dev/ttys[0-9]+"))
; Preferences IPC can expose data outside the filesystem read roots.
; Include this policy only when filesystem reads are unrestricted.
(allow ipc-posix-shm-read* (ipc-posix-name-prefix "apple.cfprefs."))
(allow mach-lookup
  (global-name "com.apple.cfprefsd.daemon")
  (global-name "com.apple.cfprefsd.agent")
  (local-name "com.apple.cfprefsd.agent"))
(allow user-preference-read)
"""


#: ``sockaddr_un.sun_path`` on macOS, including the terminating NUL.
SUN_PATH_MAX = 104
#: Parent of the per-launch pipe directory; workspace scratch paths exceed ``sun_path``.
PIPE_DIR_PARENT = "/private/tmp"
#: LibreOffice's bootstrap variable for the pipe directory, read from argv or the environment.
OSL_SOCKET_PATH = "OSL_SOCKET_PATH"
_OSL_SOCKET_ARGUMENT = f"-env:{OSL_SOCKET_PATH}="
# The pipe name ends in a 32-character hex digest of the installation (osl/unx/pipe.cxx).
_PIPE_DIGEST_LENGTH = 32
_SOFFICE_NAMES = frozenset({"soffice", "soffice.bin"})


def office_pipe_name_length(uid: int | None = None) -> int:
    """Length of LibreOffice's single-instance pipe name for ``uid`` (default: this user)."""
    uid = os.getuid() if uid is None else uid
    return len(f"OSL_PIPE_{uid}_SingleOfficeIPC_") + _PIPE_DIGEST_LENGTH


def pipe_dir_fits(path: str, uid: int | None = None) -> bool:
    """Whether ``<path>/<pipe name>`` plus its NUL fits ``sun_path``."""
    return len(os.fsencode(path)) + 1 + office_pipe_name_length(uid) < SUN_PATH_MAX


def create_office_pipe_dir(parent: str | None = None) -> str:
    """Create a fresh mode-0700 directory for one launch's pipe; the caller removes it."""
    for prefix in ("clio-lo-", "lo-"):
        path = tempfile.mkdtemp(prefix=prefix, dir=parent or PIPE_DIR_PARENT)
        if pipe_dir_fits(path):
            return path
        os.rmdir(path)
    raise ValueError(
        f"no LibreOffice pipe directory under {parent or PIPE_DIR_PARENT} fits sun_path"
    )


def remove_office_pipe_dir(path: str) -> None:
    """Remove a pipe directory and any socket LibreOffice left in it."""
    shutil.rmtree(path, ignore_errors=True)


def osl_socket_argument(pipe_dir: str) -> str:
    """The ``soffice`` bootstrap argument naming the pipe directory."""
    return f"{_OSL_SOCKET_ARGUMENT}{pipe_dir}"


def _variants(path: str) -> list[str]:
    """The path as given and as Seatbelt sees it (``/tmp`` is ``/private/tmp``)."""
    resolved = str(Path(path).resolve())
    return [path] if resolved == path else [path, resolved]


def office_policy(writes: int, read_only: int, denied: int) -> str:
    """The SBPL text; paths arrive as ``W<n>``, ``R<n>`` and ``D<n>`` parameters."""

    def subpaths(prefix: str, count: int) -> str:
        return " ".join(f'(subpath (param "{prefix}{index}"))' for index in range(count))

    rules = [_CODEX_BASE_POLICY.strip(), "(allow file-read*)"]
    if writes:
        rules.append(f"(allow file-write* {subpaths('W', writes)})")
    if read_only:
        rules.append(f"(deny file-write* {subpaths('R', read_only)})")
    if denied:
        rules.append(f"(deny file-read* file-write* {subpaths('D', denied)})")
    rules += [
        "(allow system-socket (socket-domain AF_UNIX))",
        "(allow network-bind (local unix-socket))",
        "(allow network-outbound (remote unix-socket))",
        "(allow mach-lookup "
        + " ".join(f'(global-name "{name}")' for name in OFFICE_MACH_SERVICES)
        + ")",
    ]
    return "\n".join(rules) + "\n"


def office_seatbelt_prefix(
    write_roots: Sequence[str],
    command: str,
    args: Sequence[str],
    *,
    pipe_dir: str | None = None,
) -> tuple[str, list[str]]:
    """``sandbox-exec`` argv running ``command`` under the Office policy.

    Seatbelt applies the last matching rule, so protected roots stay read-only or denied
    even inside the write territory, matching the Codex profile's precedence.

    ``pipe_dir`` (from :func:`create_office_pipe_dir`) becomes one more write root. When
    ``command`` is ``soffice`` itself, its ``-env:OSL_SOCKET_PATH`` argument follows the
    executable; a helper that launches ``soffice`` reads the caller's environment instead.
    """
    from clio_agent.runtime.storage_access import storage_access_roots  # noqa: PLC0415

    if not write_roots:
        raise ValueError("the Office sandbox needs a write territory")
    read_only, denied = storage_access_roots()
    writes = [w for root in write_roots for w in _variants(str(root))]
    launch = [command, *args]
    if pipe_dir is not None:
        if not pipe_dir_fits(pipe_dir):
            raise ValueError(f"LibreOffice pipe directory exceeds sun_path: {pipe_dir}")
        writes += _variants(pipe_dir)
        if Path(command).name in _SOFFICE_NAMES:
            kept = [arg for arg in args if not arg.startswith(_OSL_SOCKET_ARGUMENT)]
            launch = [command, osl_socket_argument(pipe_dir), *kept]
    protected = [r for root in read_only for r in _variants(str(root))]
    blocked = [d for root in denied for d in _variants(str(root))]
    params: list[str] = []
    for prefix, paths in (("W", writes), ("R", protected), ("D", blocked)):
        params += [f"-D{prefix}{index}={path}" for index, path in enumerate(paths)]
    policy = office_policy(len(writes), len(protected), len(blocked))
    return SANDBOX_EXEC, ["-p", policy, *params, *launch]


__all__ = [
    "OFFICE_MACH_SERVICES",
    "OSL_SOCKET_PATH",
    "PIPE_DIR_PARENT",
    "SANDBOX_EXEC",
    "SUN_PATH_MAX",
    "create_office_pipe_dir",
    "office_pipe_name_length",
    "office_policy",
    "office_seatbelt_prefix",
    "osl_socket_argument",
    "pipe_dir_fits",
    "remove_office_pipe_dir",
]
