"""Apply an inherited read/execute grant across a Windows tree in parallel.

``icacls ROOT /grant USER:(OI)(CI)(RX)`` writes the root ACE, then Windows
re-derives every descendant's inherited ACEs one object at a time. On a
runtime of about 27,000 files that serial walk dominates the access step.

This module produces the same security descriptors with the same Windows
inheritance engine, split into independent subtrees:

1. Count the tree once. Directories whose subtree exceeds a fair share of the
   pool's work are *intermediates*; smaller directories, and every file inside
   an intermediate, are *frontier* items.
2. Before changing anything, capture each object's explicit ACEs as Windows
   identifies them against its current parent.
3. Write the new root DACL without propagation.
4. Recompute each intermediate's inherited ACEs from its updated parent, top
   down, again without propagation.
5. Reapply each frontier item's explicit ACEs with ``SetNamedSecurityInfoW``.
   Windows derives the inherited ACEs from the updated parent and propagates
   them through that item's subtree. Frontier items run in a thread pool.

Protected objects keep their DACL, and nothing below them changes, as with
``icacls``. Any error raises. The caller then runs ``icacls``, which rewrites
the whole tree serially, so a partial parallel pass never persists.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.runtime.fleet_runtime_paths import FleetGrant

logger = logging.getLogger(__name__)

#: ``(RX)`` as icacls writes it: read, execute, read attributes/EA, synchronize.
READ_EXECUTE = 0x001200A9
DEFAULT_WORKERS = 8
#: Expanded directories hold more than 1/(workers * SHARES_PER_WORKER) of the tree.
SHARES_PER_WORKER = 4
#: Never expand a subtree this small: one propagation is cheaper than many named sets.
MIN_SUBTREE = 64

_DACL = 0x4
_ALL_INFO = 0x7  # owner, group, DACL
_UNPROTECTED_DACL = 0x20000000
_SE_FILE_OBJECT = 1
_PROTECTED = 0x1000
_AUTO_INHERITED = 0x0400
_AUTO_INHERIT_REQ = 0x0100
_INHERITED_ACE = 0x10
_GRANT_ACCESS = 1
_SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
_TRUSTEE_IS_NAME = 1
# SEF_DACL_AUTO_INHERIT | SEF_AVOID_PRIVILEGE_CHECK | SEF_AVOID_OWNER_CHECK
_SEF_FLAGS = 0x19


class ParallelGrantUnavailable(RuntimeError):
    """The tree has a shape this module does not handle; use icacls instead."""


#: What the Windows path raises: a refused shape, a Win32 or filesystem error, or a ctypes
#: argument conversion. Any of them sends the grant to ``icacls``.
FALLBACK_ERRORS = (ParallelGrantUnavailable, OSError, ValueError, ctypes.ArgumentError)


def apply_grant(
    grant: FleetGrant,
    argv: list[str],
    run: Callable[[list[str]], tuple[int, str]],
    parallel: Callable[[str, Sequence[str]], Any] | None,
) -> tuple[int, str]:
    """Apply one grant, preferring the parallel inherited grant; ``icacls`` repairs any failure.

    ``icacls`` rewrites the whole tree, so a parallel pass that stopped partway never persists.
    """
    if parallel is not None and grant.inherit:
        started = time.monotonic()
        try:
            stats = parallel(grant.path, grant.users)
        except FALLBACK_ERRORS as exc:
            logger.info(
                "fleet-runtime parallel grant unavailable label=%s, using icacls: %s: %s",
                grant.label,
                type(exc).__name__,
                exc,
            )
        else:
            logger.info(
                "fleet-runtime parallel grant label=%s seconds=%.2f %s",
                grant.label,
                time.monotonic() - started,
                stats,
            )
            return 0, ""
    return run(argv)


def grant_inherited_read_execute(
    root: str,
    users: Sequence[str],
    *,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, int]:
    """Grant ``users`` ``(OI)(CI)(RX)`` on ``root``, as ``icacls /grant`` does.

    Returns counts for the trace. Raises on any failure or unsupported shape.
    """
    if sys.platform != "win32":
        raise ParallelGrantUnavailable("not Windows")
    if not users:
        raise ParallelGrantUnavailable("no users")
    workers = max(1, workers)
    intermediates, frontier = plan_split(root, workers)
    api = _Api()
    current: dict[str, Any] = {}
    try:
        with ThreadPoolExecutor(workers) as pool:
            # Capture before any write: afterwards Windows would compare these
            # objects against the new parent and report every ACE as explicit.
            for level in intermediates:
                for path, _parent in level:
                    current[path] = api.named_sd(path)
            explicit = dict(zip(frontier, pool.map(api.explicit_acl, frontier), strict=True))
            api.grant_root(root, users)
            blocked: set[str] = set()
            for level in intermediates:
                todo = []
                for path, parent in level:
                    if parent in blocked or api.is_protected(current[path]):
                        blocked.add(path)
                    else:
                        todo.append((path, parent))
                list(pool.map(lambda item: api.reinherit(item[0], item[1], current[item[0]]), todo))

            def reapply(path: str) -> None:
                acl = explicit[path]
                if acl is not None and os.path.dirname(path) not in blocked:
                    api.reapply(path, acl)

            list(pool.map(reapply, frontier))
    finally:
        for sd in current.values():
            api.free(sd)
    return {
        "intermediates": sum(len(level) for level in intermediates),
        "frontier": len(frontier),
    }


def plan_split(root: str, workers: int) -> tuple[list[list[tuple[str, str]]], list[str]]:
    """Split the tree into intermediates, by level below the root, and frontier items.

    A directory is expanded into an intermediate only while its subtree is larger
    than a fair share of the pool's work; smaller directories and every file at an
    expanded level become frontier items. Intermediates are ``(path, parent)``.
    """
    sizes: dict[str, int] = {}
    children: dict[str, list[str]] = {}

    def count(path: str) -> int:
        total = 1
        kids: list[str] = []
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    # Windows does not follow reparse points while propagating,
                    # but a named set on one would change its target.
                    raise ParallelGrantUnavailable(f"reparse point: {entry.path}")
                kids.append(entry.path)
                total += count(entry.path) if entry.is_dir(follow_symlinks=False) else 1
        children[path] = kids
        sizes[path] = total
        return total

    threshold = max(MIN_SUBTREE, count(root) // (workers * SHARES_PER_WORKER))
    levels: list[list[tuple[str, str]]] = []
    frontier: list[str] = []
    expand = [root]
    while expand:
        level: list[tuple[str, str]] = []
        for parent in expand:
            for child in children[parent]:
                if sizes.get(child, 1) > threshold:
                    level.append((child, parent))
                else:
                    frontier.append(child)
        if level:
            levels.append(level)
        expand = [path for path, _parent in level]
    return levels, frontier


if sys.platform == "win32":  # pragma: no cover - live win32 only
    from ctypes import wintypes as _wt

    class _Trustee(ctypes.Structure):
        _fields_ = [
            ("multiple", ctypes.c_void_p),
            ("operation", ctypes.c_int),
            ("form", ctypes.c_int),
            ("type", ctypes.c_int),
            ("name", _wt.LPWSTR),
        ]

    class _ExplicitAccess(ctypes.Structure):
        _fields_ = [
            ("permissions", _wt.DWORD),
            ("mode", ctypes.c_int),
            ("inheritance", _wt.DWORD),
            ("trustee", _Trustee),
        ]

    class _GenericMapping(ctypes.Structure):
        _fields_ = [(name, _wt.DWORD) for name in ("read", "write", "execute", "all")]


class _Api:  # pragma: no cover - live win32 only
    """The advapi32 calls the parallel grant needs, with 64-bit-safe signatures."""

    ct: Any
    wt: Any
    adv: Any
    k32: Any
    ExplicitAccess: Any
    file_mapping: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ParallelGrantUnavailable("not Windows")
        import ctypes.wintypes as wt  # noqa: PLC0415

        self.ct, self.wt = ctypes, wt
        v = ctypes.c_void_p
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.LocalFree.argtypes = [v]
        k32.CreateFileW.restype = v
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, v, wt.DWORD, wt.DWORD, v]
        k32.CloseHandle.argtypes = [v]
        adv.GetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.DWORD] + [v] * 5
        adv.GetNamedSecurityInfoW.restype = wt.DWORD
        adv.SetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.DWORD] + [v] * 4
        adv.SetNamedSecurityInfoW.restype = wt.DWORD
        adv.SetEntriesInAclW.argtypes = [wt.ULONG, v, v, v]
        adv.SetEntriesInAclW.restype = wt.DWORD
        adv.GetSecurityDescriptorControl.argtypes = [v, v, v]
        adv.SetSecurityDescriptorControl.argtypes = [v, wt.WORD, wt.WORD]
        adv.GetSecurityDescriptorDacl.argtypes = [v, v, v, v]
        adv.GetSecurityDescriptorOwner.argtypes = [v, v, v]
        adv.GetSecurityDescriptorGroup.argtypes = [v, v, v]
        adv.SetSecurityDescriptorDacl.argtypes = [v, wt.BOOL, v, wt.BOOL]
        adv.SetSecurityDescriptorOwner.argtypes = [v, v, wt.BOOL]
        adv.SetSecurityDescriptorGroup.argtypes = [v, v, wt.BOOL]
        adv.InitializeSecurityDescriptor.argtypes = [v, wt.DWORD]
        adv.InitializeAcl.argtypes = [v, wt.DWORD, wt.DWORD]
        adv.GetAce.argtypes = [v, wt.DWORD, v]
        adv.AddAce.argtypes = [v, wt.DWORD, wt.DWORD, v, wt.DWORD]
        adv.SetKernelObjectSecurity.argtypes = [v, wt.DWORD, v]
        adv.GetFileSecurityW.argtypes = [wt.LPCWSTR, wt.DWORD, v, wt.DWORD, v]
        adv.SetFileSecurityW.argtypes = [wt.LPCWSTR, wt.DWORD, v]
        adv.CreatePrivateObjectSecurityEx.argtypes = [v, v, v, v, wt.BOOL, wt.ULONG, v, v]
        adv.DestroyPrivateObjectSecurity.argtypes = [v]
        self.adv, self.k32 = adv, k32
        if sys.platform == "win32":  # always true here; lets mypy skip the win32-only structs
            self.ExplicitAccess = _ExplicitAccess
            self.file_mapping = _GenericMapping(0x00120089, 0x00120116, 0x001200A0, 0x001F01FF)

    def _fail(self) -> OSError:
        return self.ct.WinError(self.ct.get_last_error())

    def free(self, pointer: Any) -> None:
        self.k32.LocalFree(pointer)

    def named_sd(self, path: str) -> Any:
        sd = self.ct.c_void_p()
        rc = self.adv.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _ALL_INFO, None, None, None, None, self.ct.byref(sd)
        )
        if rc:
            raise self.ct.WinError(rc)
        return sd

    def _control(self, sd: Any) -> int:
        control, revision = self.wt.WORD(), self.wt.DWORD()
        if not self.adv.GetSecurityDescriptorControl(
            sd, self.ct.byref(control), self.ct.byref(revision)
        ):
            raise self._fail()
        return control.value

    def is_protected(self, sd: Any) -> bool:
        return bool(self._control(sd) & _PROTECTED)

    def _dacl(self, sd: Any) -> Any:
        present, dacl, defaulted = self.wt.BOOL(), self.ct.c_void_p(), self.wt.BOOL()
        if not self.adv.GetSecurityDescriptorDacl(
            sd, self.ct.byref(present), self.ct.byref(dacl), self.ct.byref(defaulted)
        ):
            raise self._fail()
        if not present.value or not dacl.value:
            raise ParallelGrantUnavailable("object has no DACL")
        return dacl

    def _explicit(self, dacl: Any) -> Any:
        """Copy only the ACEs that are not marked inherited."""
        ct = self.ct
        keep: list[bytes] = []
        count = ct.c_ushort.from_address(dacl.value + 4).value
        for index in range(count):
            ace = ct.c_void_p()
            if not self.adv.GetAce(dacl, index, ct.byref(ace)):
                raise self._fail()
            if not ct.c_ubyte.from_address(ace.value + 1).value & _INHERITED_ACE:
                keep.append(ct.string_at(ace.value, ct.c_ushort.from_address(ace.value + 2).value))
        size = 8 + sum(len(raw) for raw in keep)
        acl = ct.create_string_buffer(size)
        if not self.adv.InitializeAcl(acl, size, 2):
            raise self._fail()
        for raw in keep:
            if not self.adv.AddAce(acl, 2, 0xFFFFFFFF, raw, len(raw)):
                raise self._fail()
        return acl

    def explicit_acl(self, path: str) -> Any:
        """The explicit ACEs of an unprotected object, or ``None`` for a protected one."""
        sd = self.named_sd(path)
        try:
            return None if self.is_protected(sd) else self._explicit(self._dacl(sd))
        finally:
            self.free(sd)

    def grant_root(self, root: str, users: Sequence[str]) -> None:
        """Merge the grant into the root DACL without touching existing children."""
        ct = self.ct
        sd = self.named_sd(root)
        new_acl = ct.c_void_p()
        try:
            # icacls applies each user in turn, inserting each ACE ahead of the previous one.
            ordered = list(reversed(users))
            entries = (self.ExplicitAccess * len(ordered))()
            for entry, user in zip(entries, ordered, strict=True):
                entry.permissions = READ_EXECUTE
                entry.mode = _GRANT_ACCESS
                entry.inheritance = _SUB_CONTAINERS_AND_OBJECTS_INHERIT
                entry.trustee.form = _TRUSTEE_IS_NAME
                entry.trustee.name = user
            rc = self.adv.SetEntriesInAclW(len(ordered), entries, self._dacl(sd), ct.byref(new_acl))
            if rc:
                raise ct.WinError(rc)
            desc = ct.create_string_buffer(64)
            self.adv.InitializeSecurityDescriptor(desc, 1)
            if not self.adv.SetSecurityDescriptorDacl(desc, True, new_acl, False):
                raise self._fail()
            # Keep a protected root protected and mark the DACL auto-inherited, as icacls does.
            bits = _AUTO_INHERITED | _AUTO_INHERIT_REQ | (self._control(sd) & _PROTECTED)
            mask = _AUTO_INHERITED | _AUTO_INHERIT_REQ | _PROTECTED
            if not self.adv.SetSecurityDescriptorControl(desc, mask, bits):
                raise self._fail()
            # READ_CONTROL | WRITE_DAC, FILE_FLAG_BACKUP_SEMANTICS opens directories.
            handle = self.k32.CreateFileW(root, 0x00060000, 7, None, 3, 0x02000000, None)
            if not handle or handle == ct.c_void_p(-1).value:
                raise self._fail()
            try:
                # The handle-level kernel set does not propagate to existing children.
                if not self.adv.SetKernelObjectSecurity(handle, _DACL, desc):
                    raise self._fail()
            finally:
                self.k32.CloseHandle(handle)
        finally:
            if new_acl.value:
                self.free(new_acl)
            self.free(sd)

    def _raw_sd(self, path: str) -> Any:
        need = self.wt.DWORD()
        self.adv.GetFileSecurityW(path, _ALL_INFO, None, 0, self.ct.byref(need))
        buf = self.ct.create_string_buffer(need.value)
        if not self.adv.GetFileSecurityW(path, _ALL_INFO, buf, need, self.ct.byref(need)):
            raise self._fail()
        return buf

    def reinherit(self, path: str, parent: str, current: Any) -> None:
        """Rebuild one directory's inherited ACEs from its parent, without propagating."""
        ct = self.ct
        owner, group, flag = ct.c_void_p(), ct.c_void_p(), self.wt.BOOL()
        self.adv.GetSecurityDescriptorOwner(current, ct.byref(owner), ct.byref(flag))
        self.adv.GetSecurityDescriptorGroup(current, ct.byref(group), ct.byref(flag))
        explicit = self._explicit(self._dacl(current))
        creator = ct.create_string_buffer(64)
        self.adv.InitializeSecurityDescriptor(creator, 1)
        self.adv.SetSecurityDescriptorOwner(creator, owner, False)
        self.adv.SetSecurityDescriptorGroup(creator, group, False)
        self.adv.SetSecurityDescriptorDacl(creator, True, explicit, False)
        new = ct.c_void_p()
        if not self.adv.CreatePrivateObjectSecurityEx(
            self._raw_sd(parent),
            creator,
            ct.byref(new),
            None,
            True,
            _SEF_FLAGS,
            None,
            ct.byref(self.file_mapping),
        ):
            raise self._fail()
        try:
            self.adv.SetSecurityDescriptorControl(new, _AUTO_INHERIT_REQ, _AUTO_INHERIT_REQ)
            if not self.adv.SetFileSecurityW(path, _DACL, new):
                raise self._fail()
        finally:
            self.adv.DestroyPrivateObjectSecurity(ct.byref(new))

    def reapply(self, path: str, explicit: Any) -> None:
        """Re-derive inheritance from the parent and propagate it through this subtree."""
        rc = self.adv.SetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _DACL | _UNPROTECTED_DACL, None, None, explicit, None
        )
        if rc:
            raise self.ct.WinError(rc)
