"""CLIO-owned stand-in for the POSIX ``pwd`` module, for SearXNG on Windows.

Shipped as ``clio_pwd_shim.py`` and installed as ``sys.modules["pwd"]`` by CLIO's
SearXNG launcher on Windows only. SearXNG's ``searx.valkeydb`` imports ``pwd``
unconditionally and calls ``pwd.getpwuid(os.getuid())`` only to name the account in
a Valkey connection error; CLIO's private instance runs without Valkey. SearXNG's
own source is never modified (it is AGPL-3.0 and installed as published).
"""

from __future__ import annotations

import os
from typing import NamedTuple


class struct_passwd(NamedTuple):  # noqa: N801 - mirrors the stdlib type's name
    """The fields of the POSIX ``pwd.struct_passwd``."""

    pw_name: str
    pw_passwd: str
    pw_uid: int
    pw_gid: int
    pw_gecos: str
    pw_dir: str
    pw_shell: str


def _current(uid: int) -> struct_passwd:
    name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return struct_passwd(name, "x", uid, uid, name, home, "")


def getpwuid(uid: int) -> struct_passwd:
    """The current account, whatever ``uid`` is (Windows has no POSIX uids)."""

    return _current(int(uid))


def getpwnam(name: str) -> struct_passwd:
    """The current account when ``name`` is it; ``KeyError`` like the stdlib otherwise."""

    entry = _current(os.getpid())
    if name != entry.pw_name:
        raise KeyError(f"getpwnam(): name not found: {name!r}")
    return entry


def getpwall() -> list[struct_passwd]:
    """Only the current account is known."""

    return [_current(os.getpid())]
