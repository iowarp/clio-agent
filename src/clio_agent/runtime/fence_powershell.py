"""The person's PowerShell execution policy, carried into a Windows MXC child.

Windows PowerShell checks its execution policy before it runs any ``.ps1`` file. That
includes the ``npm.ps1``, ``npx.ps1``, ``pnpm.ps1`` and ``yarn.ps1`` shims, which it
prefers over their ``.cmd`` siblings, so a bare ``npm`` takes this path and ``npm.cmd``
does not. It reads the scopes in precedence order: Group Policy (MachinePolicy,
UserPolicy), Process (``PSExecutionPolicyPreference``), CurrentUser
(``HKCU\\...\\PowerShell\\1\\ShellIds\\Microsoft.PowerShell``), then LocalMachine. The MXC
container token may not open the person's CurrentUser key. The check then throws
*AuthorizationManager check failed* (inner ``SecurityException``: *Requested registry
access is not allowed*), and every ``.ps1`` refuses to run although the person's policy
allows it.

CLIO asks the person's own PowerShell for the effective policy and gives the child that
value as its Process scope. Process ranks above CurrentUser, so the fenced PowerShell
stops there without opening the key, and Group Policy still outranks it. The child runs
under the policy the person already has, never a wider one such as ``Bypass``.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

POLICY_ENV = "PSExecutionPolicyPreference"
_POLICIES = frozenset(
    {"AllSigned", "Bypass", "Default", "RemoteSigned", "Restricted", "Unrestricted"}
)


@functools.lru_cache(maxsize=1)
def _person_policy() -> str:
    exe = shutil.which("powershell.exe")
    if not exe:
        logger.warning("powershell execution policy not carried reason=powershell_not_found")
        return ""
    try:
        done = subprocess.run(
            [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "powershell execution policy not carried reason=policy_query_failed error=%s: %s",
            type(exc).__name__,
            exc,
        )
        return ""
    policy = done.stdout.strip()
    if done.returncode != 0 or policy not in _POLICIES:
        logger.warning(
            "powershell execution policy not carried reason=policy_unreadable rc=%s out=%r err=%r",
            done.returncode,
            policy[:80],
            done.stderr.strip()[:200],
        )
        return ""
    return policy


def execution_policy_env(platform: str = sys.platform) -> dict[str, str]:
    """``{PSExecutionPolicyPreference: <person's effective policy>}`` for an MXC child.

    Empty off Windows, and when the person's environment already sets a Process scope,
    which the child inherits unchanged.
    """

    if not platform.startswith("win") or os.environ.get(POLICY_ENV):
        return {}
    policy = _person_policy()
    return {POLICY_ENV: policy} if policy else {}
