"""claude_code model-catalog discovery: no enumeration exists, so refresh
probe-validates the documented CLI alias vocabulary (iowarp/clio-agent#1211)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from typing import Any

from clio_agent.providers.model_discovery.overlay import (
    CLAUDE_CODE_SOURCE,
    ProviderDiscoveryResult,
    attach_context_limits,
)

#: The documented Claude Code CLI model aliases (verified live via ``claude --help``
#: 2.1.228: "--model <model> ... Provide an alias for the latest model (e.g.
#: 'fable', 'opus', or 'sonnet')..."). ``fable`` is the CLI's own CURRENT default
#: (verified empirically 2026-08-14: a bare ``claude -p`` call with no ``--model``
#: resolves to ``claude-fable-5``) — probed first, and :func:`discover_claude_code`
#: also runs one bare (no ``--model``) call to learn which alias that resolves to,
#: so the reported default follows the CLI's own choice rather than a guess
#: (#1211: "the CLI's own default, not our guess").
CLAUDE_CODE_ALIAS_CANDIDATES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

#: Per-probe timeout for one claude_code CLI call (#1211 review R2/R3: the OLD
#: 60s-per-probe default gave a 5-probe (1 bare + 4 aliases) worst case of 300s.
#: The native image/PDF proof can take longer than the former text-only probe;
#: 30s preserves a bounded failure while covering observed cold SDK startup.
#: ``discover_claude_code`` also exits its loop on the FIRST inconclusive probe
#: rather than always running all 5, so the common-case worst case is much
#: tighter than ``5 * timeout``.
CLAUDE_CODE_PROBE_TIMEOUT_S = 30.0

#: One cheap native multimodal turn used to validate both the model alias and
#: the image/PDF input contract exposed by the installed Claude Code SDK/CLI.
_PROBE_PROMPT = "Reply with the single word: ok."
_PROBE_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

#: Rejection is a DEFINITIVE model-not-available signal -- the only api_error_status
#: this probe treats as "the account does not serve this model" (#1211 review D3).
#: Verified live, CLI 2.1.228: an unknown ``--model`` value comes back
#: ``{"is_error": true, "api_error_status": 404, "result": "There's an issue with
#: the selected model (X)..."}``.
_CLAUDE_REJECTION_STATUS = 404


class ClaudeCodeCLIUnavailableError(RuntimeError):
    """Raised when the ``claude`` binary isn't on PATH at probe time."""


def _probe_pdf_b64() -> str:
    """Return a valid one-page PDF for the live native-input probe."""

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1 1] /Contents 4 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(body))
        body.extend(obj)
    xref = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return base64.b64encode(bytes(body)).decode("ascii")


def _multimodal_probe_input() -> str:
    """Build one Claude stream-json user message with native image and PDF blocks."""

    payload = {
        "type": "user",
        "session_id": "",
        "parent_tool_use_id": None,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _PROBE_IMAGE_B64,
                    },
                },
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": _probe_pdf_b64(),
                    },
                },
                {"type": "text", "text": _PROBE_PROMPT},
            ],
        },
    }
    return json.dumps(payload, separators=(",", ":")) + "\n"


def _result_payload(stdout: str) -> dict[str, Any] | None:
    """Read a result envelope from either legacy JSON or stream-json output."""

    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and (
        payload.get("type") == "result" or "is_error" in payload or "modelUsage" in payload
    ):
        return payload
    result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "result":
            result = row
    return result


def _resolve_claude_binary() -> str:
    """Return an absolute path to the ``claude`` binary or raise, Windows-shim-aware.

    Prefers the Windows ``.cmd`` shim because a bare ``shutil.which`` can return
    an un-executable wrapper on Windows.
    """
    if os.name == "nt":
        cmd_path = shutil.which("claude.cmd") or shutil.which("claude.exe")
        if cmd_path:
            return cmd_path
    path = shutil.which("claude")
    if not path:
        raise ClaudeCodeCLIUnavailableError(
            "`claude` not found on PATH. Install Claude Code and run `claude login` "
            "once per machine."
        )
    return path


def _probe_claude(binary: str, alias: str | None, *, timeout: float) -> dict[str, Any]:
    """Run one native image/PDF turn, probing ``alias`` (or the CLI default).

    Never raises. Returns ``{"outcome", "resolved_model", "reason"}`` where
    ``outcome`` is one of:

    * ``"accepted"`` — the alias/model resolved and answered; ``resolved_model``
      carries its RESOLVED canonical model id (``modelUsage`` key), which is how
      :func:`discover_claude_code` learns the CLI's live default without guessing.
    * ``"rejected"`` — a DEFINITIVE signal the account does not serve this model
      (``api_error_status == 404`` in the CLI's own JSON error envelope). The
      ONLY outcome that may narrow a provider's overlay.
    * ``"inconclusive"`` — anything else that kept this probe from answering
      cleanly: a timeout, a launch failure, a non-JSON response, or an
      ``is_error`` body with any OTHER status (429/5xx/absent — rate limit,
      server error, or an unrecognised shape). NEVER treated as a rejection
      (#1211 review D3) — the caller must keep the provider's prior overlay
      list untouched rather than silently narrow it based on transient noise.

    Exit code is NOT a reliable signal — a rejected model still exits 0 with
    ``is_error: true`` in the body.
    """
    args = [
        binary,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if alias:
        args += ["--model", alias]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user-controlled input
            args,
            input=_multimodal_probe_input(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "outcome": "inconclusive",
            "resolved_model": "",
            "reason": f"probe timed out after {timeout}s",
        }
    except OSError as exc:
        return {
            "outcome": "inconclusive",
            "resolved_model": "",
            "reason": f"probe failed to launch: {exc}",
        }
    payload = _result_payload(proc.stdout)
    if payload is None:
        return {
            "outcome": "inconclusive",
            "resolved_model": "",
            "reason": f"non-JSON response (exit={proc.returncode}): {proc.stdout[:200]!r}",
        }
    if payload.get("is_error"):
        status = payload.get("api_error_status")
        reason = str(payload.get("result") or f"api_error_status={status}")
        outcome = "rejected" if status == _CLAUDE_REJECTION_STATUS else "inconclusive"
        return {"outcome": outcome, "resolved_model": "", "reason": reason}
    resolved = next(iter(payload.get("modelUsage") or {}), "")
    return {
        "outcome": "accepted",
        "resolved_model": str(resolved),
        "reason": "",
        "capabilities": ["text", "image", "pdf"],
    }


def discover_claude_code(
    *,
    candidates: tuple[str, ...] = CLAUDE_CODE_ALIAS_CANDIDATES,
    timeout: float = CLAUDE_CODE_PROBE_TIMEOUT_S,
) -> ProviderDiscoveryResult:
    """Refresh claude_code's alias catalog by probe-validating each documented alias.

    No enumeration endpoint exists for this channel (#1211 comment) — the catalog
    rows ARE the CLI's documented ``--model`` alias vocabulary, and "refresh"
    means running one trivial turn per alias and recording which ones the
    account currently accepts. Sequential (each is a real, billed API call).

    Runs one extra BARE call (no ``--model``) first to learn the CLI's own
    current default by resolved-canonical-id match, so ``default_model`` follows
    the CLI's choice rather than a guess (#1211); if that bare probe itself is
    inconclusive/rejected, ``default_model`` falls back to the first validated
    alias and ``default_model_reason`` records why (#1211 review N5).

    A REJECTED alias (a definitive "the account does not serve this" signal) is
    recorded in ``rejected`` (informational) without failing the whole provider,
    as long as at least one alias validates. An INCONCLUSIVE probe (timeout /
    429 / 5xx / launch failure / bad response — transient noise, never a
    rejection) — on the bare probe OR any alias — aborts the WHOLE call with a
    typed ``failed_reason`` (``discovered=[]``), so ``record_refresh`` keeps
    the provider's PRIOR overlay list untouched rather than silently narrowing
    it (#1211 review D3). The loop exits on the FIRST inconclusive probe rather
    than always running all candidates, bounding the common-case latency well
    under the ``len(candidates) + 1`` worst case.
    """
    try:
        binary = _resolve_claude_binary()
    except ClaudeCodeCLIUnavailableError as exc:
        return ProviderDiscoveryResult(
            provider="claude_code", discovered=[], source=CLAUDE_CODE_SOURCE, failed_reason=str(exc)
        )

    bare = _probe_claude(binary, None, timeout=timeout)
    if bare["outcome"] == "inconclusive":
        return ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[],
            source=CLAUDE_CODE_SOURCE,
            failed_reason=f"bare CLI-default probe inconclusive: {bare['reason']}",
        )
    cli_default_canonical = bare["resolved_model"] if bare["outcome"] == "accepted" else ""

    discovered: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    default_model = ""
    for alias in candidates:
        probe = _probe_claude(binary, alias, timeout=timeout)
        if probe["outcome"] == "inconclusive":
            return ProviderDiscoveryResult(
                provider="claude_code",
                discovered=[],
                source=CLAUDE_CODE_SOURCE,
                failed_reason=f"alias {alias!r} probe inconclusive: {probe['reason']}",
            )
        if probe["outcome"] == "accepted":
            resolved = probe["resolved_model"]
            discovered.append(
                {
                    "id": alias,
                    "name": f"Claude {alias.capitalize()} (Claude Code alias)",
                    "description": (
                        f"Resolves to {resolved}." if resolved else "Validated Claude Code alias."
                    ),
                    "capabilities": list(probe.get("capabilities") or []),
                }
            )
            if cli_default_canonical and resolved == cli_default_canonical:
                default_model = alias
        else:  # "rejected" -- definitive, informational, never aborts the provider
            rejected.append({"id": alias, "reason": probe["reason"]})

    if not discovered:
        reasons = "; ".join(f"{r['id']}: {r['reason']}" for r in rejected) or "no aliases validated"
        return ProviderDiscoveryResult(
            provider="claude_code", discovered=[], source=CLAUDE_CODE_SOURCE, failed_reason=reasons
        )
    default_model_reason = ""
    if not default_model:
        default_model = discovered[0]["id"]
        default_model_reason = (
            f"bare CLI-default probe was {bare['outcome']} ({bare['reason']}); falling back to "
            "the first validated alias"
            if bare["outcome"] != "accepted"
            else "bare CLI-default probe resolved to a model id no validated alias matched; "
            "falling back to the first validated alias"
        )
    discovered = attach_context_limits(discovered, "claude_code")
    return ProviderDiscoveryResult(
        provider="claude_code",
        discovered=discovered,
        source=CLAUDE_CODE_SOURCE,
        default_model=default_model,
        default_model_reason=default_model_reason,
        rejected=rejected,
    )


__all__ = [
    "CLAUDE_CODE_ALIAS_CANDIDATES",
    "CLAUDE_CODE_PROBE_TIMEOUT_S",
    "ClaudeCodeCLIUnavailableError",
    "discover_claude_code",
]
