"""The Win32 ``ShellExecuteExW`` mask and ``SHELLEXECUTEINFOW`` struct for ``runas`` elevation.

Defined only on win32 (``ctypes.wintypes`` is Windows-only) and at module scope, so the
no-class-in-function ratchet stays at 0. :mod:`clio_agent.runtime.sandbox_cli` uses it for the
one-time elevated codex sandbox setup.
"""

from __future__ import annotations

import sys

SEE_MASK_NOCLOSEPROCESS = 0x00000040

if sys.platform == "win32":  # pragma: no cover - win32 live gate only (CI runs on Linux)
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfoW(ctypes.Structure):
        """The Win32 ``SHELLEXECUTEINFOW`` struct for the ``runas`` self-elevation."""

        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]
