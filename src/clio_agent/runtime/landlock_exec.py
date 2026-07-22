"""Landlock launcher shim: apply the fs write-fence, then ``exec`` the real argv (#976/B2).

Landlock restricts the CALLING thread before ``exec``, so a confined subprocess must apply
its own ruleset. The :mod:`clio_agent.runtime.sandbox_landlock` rung composes
``python -m clio_agent.runtime.landlock_exec <write_root>... -- <command> <args>...``; this
module parses that argv, applies a Landlock fs **write** fence granting write access ONLY
beneath the given roots (reads/execs everywhere stay allowed — an fs-write fence, matching
the rung's contract), then ``execvp``s the real command so the child runs confined.

NO SILENT FALLBACK (house rule): if the ruleset cannot be applied, the shim prints a typed
reason to stderr and exits non-zero — it NEVER ``exec``s unconfined (a silent fence hole is
the exact defect the campaign forbids). ``prctl(PR_SET_NO_NEW_PRIVS)`` is set first so
Landlock accepts an unprivileged ruleset.

Kept tiny + import-light (ctypes + os only) so it starts fast on every confined spawn. The
argv split (:func:`parse_argv`) is pure and unit-tested; the syscall application only runs
on Linux at exec time (exercised in the live gate + WSL smoke).
"""

from __future__ import annotations

import ctypes
import os
import sys

# Landlock syscall numbers (stable across arches — assigned together in 5.13).
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446
_LANDLOCK_RULE_PATH_BENEATH = 1

# prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) — required before an unprivileged restrict_self.
_PR_SET_NO_NEW_PRIVS = 38

# fs access-right bits (ABI 1). The WRITE-family set below is "handled" (restricted
# everywhere) and "allowed" beneath each root; EXECUTE(1<<0)/READ_FILE(1<<2)/READ_DIR(1<<3)
# are deliberately NOT handled, so reads + execs stay unrestricted (fs WRITE fence only).
_FS_WRITE_FILE = 1 << 1
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_HANDLED_WRITE_ACCESS = (
    _FS_WRITE_FILE
    | _FS_REMOVE_DIR
    | _FS_REMOVE_FILE
    | _FS_MAKE_CHAR
    | _FS_MAKE_DIR
    | _FS_MAKE_REG
    | _FS_MAKE_SOCK
    | _FS_MAKE_FIFO
    | _FS_MAKE_BLOCK
    | _FS_MAKE_SYM
)

#: Exit code used when the shim cannot apply the fence (a loud, typed failure — never exec).
EXIT_FENCE_FAILED = 127


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def parse_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``[<root>..., "--", <command>, <args>...]`` into ``(roots, command_argv)``.

    Pure + unit-tested. ``argv`` is the shim's own args (``sys.argv[1:]``). The FIRST ``--``
    separates the write roots from the real command argv. Raises ``ValueError`` when the
    separator or the command is missing — a malformed compose is a loud failure, never a
    guess.
    """
    if "--" not in argv:
        raise ValueError("landlock_exec argv missing '--' separator between roots and command")
    split = argv.index("--")
    roots = argv[:split]
    command_argv = argv[split + 1 :]
    if not command_argv:
        raise ValueError("landlock_exec argv has no command after '--'")
    return roots, command_argv


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(ctypes.c_long(number), *args))


def apply_write_fence(roots: list[str]) -> None:
    """Apply a Landlock fs WRITE fence granting write access only beneath ``roots``.

    Raises ``OSError`` on any syscall failure (the caller maps it to a loud, typed exit).
    Roots that do not exist on disk are skipped (a fence over a not-yet-created cache dir is
    not a failure); at least the ruleset itself is always applied, so an empty/incorrect root
    set fences MORE, never less.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    # No-new-privs first, or restrict_self is rejected for an unprivileged process.
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")

    attr = _RulesetAttr(handled_access_fs=_HANDLED_WRITE_ACCESS)
    ruleset_fd = _syscall(
        libc, _NR_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), ctypes.c_uint(0)
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        for root in roots:
            try:
                dir_fd = os.open(root, os.O_PATH)
            except OSError:
                continue  # a not-yet-created writable root fences nothing to grant; skip it
            try:
                rule = _PathBeneathAttr(allowed_access=_HANDLED_WRITE_ACCESS, parent_fd=dir_fd)
                rc = _syscall(
                    libc,
                    _NR_ADD_RULE,
                    ctypes.c_int(ruleset_fd),
                    ctypes.c_uint(_LANDLOCK_RULE_PATH_BENEATH),
                    ctypes.byref(rule),
                    ctypes.c_uint(0),
                )
                if rc != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {root}")
            finally:
                os.close(dir_fd)
        if _syscall(libc, _NR_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint(0)) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)


def main(argv: list[str] | None = None) -> int:
    """Parse argv, apply the write fence, then ``execvp`` the real command (never returns on ok).

    On a parse or fence-application failure prints ``reason=<...>`` to stderr and returns a
    non-zero exit WITHOUT exec — the confined spawn fails loud rather than running unconfined.
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        roots, command_argv = parse_argv(raw)
    except ValueError as exc:
        print(f"landlock_exec reason=argv_malformed detail={exc}", file=sys.stderr)
        return EXIT_FENCE_FAILED
    try:
        apply_write_fence(roots)
    except OSError as exc:
        print(f"landlock_exec reason=landlock_apply_failed detail={exc}", file=sys.stderr)
        return EXIT_FENCE_FAILED
    try:
        os.execvp(command_argv[0], command_argv)
    except OSError as exc:  # exec failed — report loud, do not fall through unconfined
        print(f"landlock_exec reason=exec_failed detail={exc}", file=sys.stderr)
        return EXIT_FENCE_FAILED
    return 0  # unreachable on a successful exec


if __name__ == "__main__":
    raise SystemExit(main())
