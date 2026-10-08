"""Readiness commands for managed model runtime containers (health and identity)."""

from __future__ import annotations

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.models import CommandSpec
from clio_agent.gact.infrastructure.secret_env import with_secret_env


def health_command(url: str, windows: bool) -> CommandSpec:
    """Readiness by the health URL alone (servers with no per-launch key)."""

    if windows:
        return powershell.command(
            f"try {{ Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 {powershell.literal(url)} "
            "| Out-Null; 'ready' } catch { 'waiting' }",
            timeout_seconds=30,
        )
    # A host with neither curl nor wget can never answer "ready": say so with a
    # typed line instead of waiting forever.
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            "if command -v curl >/dev/null 2>&1; then "
            'curl -fsS -m 5 -o /dev/null --noproxy "*" "$0" 2>/dev/null && echo ready || echo waiting; '
            "elif command -v wget >/dev/null 2>&1; then "
            'wget -q -T 5 -O /dev/null "$0" 2>/dev/null && echo ready || echo waiting; '
            "else echo no_http_client; fi",
            url,
        ],
        timeout_seconds=30,
    )


def identity_health_command(
    health_url: str, models_url: str, served: str, variable: str, api_key: str, windows: bool
) -> CommandSpec:
    """Readiness that proves the answering server is ours, not just healthy (F026, F010).

    The endpoint must refuse ``/v1/models`` without the per-launch key, answer
    its health URL, and, given the key (held in ``variable``, read from
    stdin), list ``served`` (any model when empty). A server that accepts a
    keyless request is someone else's: the check never reports it ready.
    """

    if windows:
        script = (
            f"$m = {powershell.literal(models_url)}; $u = {powershell.literal(health_url)}; "
            f"$s = {powershell.literal(served)}; "
            "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 $m | Out-Null; "
            "'foreign_endpoint'; exit 0 } catch { "
            "$c = 0; if ($_.Exception.Response) { $c = [int]$_.Exception.Response.StatusCode }; "
            "if ($c -ne 401 -and $c -ne 403) { 'waiting'; exit 0 } }; "
            "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 $u | Out-Null } "
            "catch { 'waiting'; exit 0 }; "
            "try { $b = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "
            f'-Headers @{{Authorization = "Bearer $env:{variable}"}} $m).Content }} '
            "catch { 'waiting'; exit 0 }; "
            "if ($s -and -not $b.Contains('\"' + $s + '\"')) { 'waiting' } else { 'ready' }"
        )
        command = powershell.command(script, timeout_seconds=30)
    else:
        # The key reaches curl as a header read from stdin (-H @-), never as an
        # argument; printf is a shell builtin.
        command = CommandSpec(
            program="sh",
            args=[
                "-c",
                "command -v curl >/dev/null 2>&1 || { echo no_http_client; exit 0; }; "
                'code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" --noproxy "*" "$1"); '
                'case "$code" in 401|403) ;; 2??) echo foreign_endpoint; exit 0 ;; '
                "*) echo waiting; exit 0 ;; esac; "
                'curl -fsS -m 5 -o /dev/null --noproxy "*" "$0" 2>/dev/null '
                "|| { echo waiting; exit 0; }; "
                f'body=$(printf "Authorization: Bearer %s\\n" "${variable}" '
                '| curl -fsS -m 5 --noproxy "*" -H @- "$1" 2>/dev/null) '
                "|| { echo waiting; exit 0; }; "
                '[ -z "$2" ] || printf %s "$body" | grep -F -q "\\"$2\\"" '
                "|| { echo waiting; exit 0; }; "
                "echo ready",
                health_url,
                models_url,
                served,
            ],
            timeout_seconds=30,
        )
    return with_secret_env(command, variable, api_key, windows=windows)
