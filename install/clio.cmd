@echo off
rem CLIO launcher shim - delegates to clio.ps1 next to this file so the
rem clio command works from cmd.exe and any plain shell on PATH.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0clio.ps1" %*
