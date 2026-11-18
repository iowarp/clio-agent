"""The Win32 ``ShellExecuteExW`` ``runas`` elevation: its mask, struct and launcher.

Defined only on win32 (``ctypes.wintypes`` is Windows-only) and at module scope, so the
no-class-in-function ratchet stays at 0. :mod:`clio_agent.runtime.sandbox_cli` uses
:func:`launch_elevated_codex_setup` for the one-time elevated codex sandbox setup.
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

    def launch_elevated_codex_setup(argv: list[str]) -> tuple[bool, str]:
        """Wait for the explicitly approved Windows setup helper to finish."""
        from clio_agent.runtime.sandbox_codex_probe import codex_home_env  # noqa: PLC0415

        # cmd.exe running the codex shim so a .cmd launcher resolves under elevation.
        params = " ".join(f'"{a}"' if " " in a else a for a in argv)
        # An elevated process does not inherit this environment; name the layer's home.
        if home := codex_home_env().get("CODEX_HOME"):
            params = f'set "CODEX_HOME={home}" && {params}'
        info = ShellExecuteInfoW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "runas"
        info.lpFile = "cmd.exe"
        info.lpParameters = f"/c {params}"
        info.nShow = 0  # SW_HIDE
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
            return False, (
                f"self-elevation refused or failed (GetLastError={ctypes.get_last_error()})"
            )
        ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(info.hProcess)
        if code.value != 0:
            return False, f"elevated codex sandbox setup exited {code.value}"
        return True, "codex Windows sandbox setup completed under elevation"
