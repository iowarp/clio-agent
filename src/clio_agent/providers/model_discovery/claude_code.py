"""Claude Code model discovery using the maintained remote candidate catalog."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from typing import Any

from clio_agent import conf
from clio_agent.providers.model_discovery.claude_code_catalog import (
    ClaudeCodeCatalogError,
    refresh_claude_code_candidates,
)
from clio_agent.providers.model_discovery.modality_evidence import modality_evidence
from clio_agent.providers.model_discovery.overlay import (
    CLAUDE_CODE_SOURCE,
    ProviderDiscoveryResult,
    attach_context_limits,
)
from clio_agent.providers.model_discovery.probe_assets import (
    ProbeChallenge,
    build_probe_challenge,
)

#: Per-probe timeout for one claude_code CLI call (#1211 review R2/R3: the OLD
#: 60s-per-probe default gave a 5-probe (1 bare + 4 aliases) worst case of 300s.
#: The native image/PDF proof can take longer than the former text-only probe;
#: 30s preserves a bounded failure while covering observed cold SDK startup.
#: ``discover_claude_code`` also exits its loop on the FIRST inconclusive probe
#: rather than always running all 5, so the common-case worst case is much
#: tighter than ``5 * timeout``.
#:
#: Configuration, not a compiled-in constant: cold SDK startup is a property of
#: the operator's machine and account, so an install that needs longer raises
#: ``providers.claude_code.probe_timeout_s`` /
#: ``CLIO_CLAUDE_CODE_PROBE_TIMEOUT_S`` rather than patching this file. Resolved
#: at import (like ``gact/runtime/constants.py``'s ``_CTX_MAX_BYTES``) because it
#: is also this module's public default argument.
CLAUDE_CODE_PROBE_TIMEOUT_S: float = conf.resolve(
    "providers.claude_code.probe_timeout_s",
    env="CLIO_CLAUDE_CODE_PROBE_TIMEOUT_S",
    default=30.0,
    cast=conf.as_float,
)

#: The text-only probe: validates the ALIAS alone, and says nothing about
#: modalities. Used as the M3 fallback when the multimodal turn cannot run.
_TEXT_PROBE_PROMPT = "Reply with the single word: ok."

#: The multimodal probe prompt. It names exactly what a genuine reply must
#: contain, and gives the model an explicit way to say an attachment did not
#: arrive -- so "I could not see it" is an answer, not a parse failure.
_NATIVE_PROBE_PROMPT = (
    "Two attachments are included with this message: one image and one PDF. Each "
    "shows a single four-digit number. Reply with exactly one line and nothing "
    "else:\nIMAGE: <the number in the image>; PDF: <the number in the PDF>\n"
    "If an attachment did not reach you, write NONE in its place."
)

#: Parses ``IMAGE: 1234; PDF: 5678`` out of a reply, tolerating case, spacing and
#: surrounding prose. Each modality is matched independently, so a reply that
#: gets one right and one wrong evidences exactly one.
_PROBE_TOKEN_RE = re.compile(r"\b(IMAGE|PDF)\b\s*[:=]\s*([A-Za-z0-9]+)", re.IGNORECASE)

#: Rejection is a DEFINITIVE model-not-available signal -- the only api_error_status
#: this probe treats as "the account does not serve this model" (#1211 review D3).
#: Verified live, CLI 2.1.228: an unknown ``--model`` value comes back
#: ``{"is_error": true, "api_error_status": 404, "result": "There's an issue with
#: the selected model (X)..."}``.
_CLAUDE_REJECTION_STATUS = 404


class ClaudeCodeCLIUnavailableError(RuntimeError):
    """Raised when the ``claude`` binary isn't on PATH at probe time."""


def _probe_input(challenge: ProbeChallenge | None) -> str:
    """Build one Claude stream-json user message for a probe turn.

    ``challenge`` present -> the native image + PDF blocks plus the prompt that
    demands their codes back. ``None`` -> the text-only fallback turn, which
    validates the alias and claims nothing about modalities.
    """

    content: list[dict[str, Any]] = []
    if challenge is not None:
        content += [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": challenge.image_b64,
                },
            },
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": challenge.pdf_b64,
                },
            },
        ]
    content.append(
        {"type": "text", "text": _NATIVE_PROBE_PROMPT if challenge else _TEXT_PROBE_PROMPT}
    )
    payload = {
        "type": "user",
        "session_id": "",
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": content},
    }
    return json.dumps(payload, separators=(",", ":")) + "\n"


def _evidenced_modalities(
    reply: str, challenge: ProbeChallenge
) -> tuple[list[str], dict[str, Any]]:
    """Return ``(capabilities, capability_evidence)`` for one native probe reply.

    Each modality is judged INDEPENDENTLY on whether the reply quotes back that
    attachment's own code, so a CLI that forwards the image but strips the PDF is
    recorded as image-capable and pdf-unreported rather than as either extreme.
    ``text`` is always evidenced — the model answered.
    """

    tokens = {
        match.group(1).lower(): match.group(2) for match in _PROBE_TOKEN_RE.finditer(reply or "")
    }
    expected = {"image": challenge.image_code, "pdf": challenge.pdf_code}
    capabilities = ["text"]
    unevidenced: list[str] = []
    for modality, code in expected.items():
        if tokens.get(modality, "").strip().lower() == code.lower():
            capabilities.append(modality)
        else:
            unevidenced.append(modality)
    if not unevidenced:
        return capabilities, modality_evidence(
            source="claude_code_native_probe", reason="modality_reported"
        )
    return capabilities, modality_evidence(
        source="claude_code_native_probe",
        reason="modality_probe_unevidenced",
        unevidenced=unevidenced,
        detail=f"reply did not quote the attached code(s): {(reply or '')[:200]!r}",
    )


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
    sdk_spec = importlib.util.find_spec("claude_agent_sdk")
    sdk_origin = getattr(sdk_spec, "origin", None)
    if sdk_origin:
        bundled_name = "claude.exe" if os.name == "nt" else "claude"
        bundled = os.path.join(os.path.dirname(sdk_origin), "_bundled", bundled_name)
        if os.path.isfile(bundled):
            return bundled
    if os.name == "nt":
        cmd_path = shutil.which("claude.cmd") or shutil.which("claude.exe")
        if cmd_path:
            return cmd_path
    path = shutil.which("claude")
    if not path:
        raise ClaudeCodeCLIUnavailableError(
            "Claude Code runtime is unavailable. Install Claude Code support and sign in "
            "once on the connected agent."
        )
    return path


def _probe_claude(
    binary: str,
    alias: str | None,
    *,
    timeout: float,
    challenge: ProbeChallenge | None = None,
) -> dict[str, Any]:
    """Run one probe turn against ``alias`` (or the CLI default).

    ``challenge`` present -> the native image + PDF turn whose reply must quote
    both codes back; ``None`` -> the text-only turn that validates the alias and
    claims nothing about modalities.

    Never raises. Returns ``{"outcome", "resolved_model", "reason",
    "capabilities", "capability_evidence"}`` where ``outcome`` is one of:

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
            input=_probe_input(challenge),
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
    if challenge is None:
        # A text-only turn evidences the ALIAS, nothing more. Stamping image/pdf
        # here (the old behaviour, on ANY non-error reply) was pure fabrication:
        # a CLI that stripped the attachments answered identically.
        capabilities = ["text"]
        evidence = modality_evidence(
            source="claude_code_native_probe",
            reason="modality_probe_unavailable",
            unevidenced=["image", "pdf"],
        )
    else:
        capabilities, evidence = _evidenced_modalities(str(payload.get("result") or ""), challenge)
    return {
        "outcome": "accepted",
        "resolved_model": str(resolved),
        "reason": "",
        "capabilities": capabilities,
        "capability_evidence": evidence,
    }


def _probe_alias(binary: str, alias: str | None, *, timeout: float) -> dict[str, Any]:
    """Probe one alias multimodally, falling back to a text-only turn (M3).

    A multimodal probe that cannot answer must NOT reject the model or sink the
    whole discovery run: an account or CLI build that cannot carry attachments is
    a modality fact about that model, not evidence it is unavailable. So an
    inconclusive native turn is retried once as text-only; when THAT validates,
    the alias is accepted with text-only capabilities and a typed
    ``modality_probe_unavailable`` reason carrying the native failure. Only a
    probe that could not answer at all stays inconclusive.

    A REJECTION (a definitive 404) is returned as-is and never retried — the
    account does not serve the model, and no fallback changes that.
    """

    native = _probe_claude(binary, alias, timeout=timeout, challenge=build_probe_challenge())
    if native["outcome"] != "inconclusive":
        return native
    fallback = _probe_claude(binary, alias, timeout=timeout, challenge=None)
    if fallback["outcome"] != "accepted":
        return native
    evidence = dict(fallback.get("capability_evidence") or {})
    evidence["detail"] = f"native probe was inconclusive: {native['reason']}"
    return {**fallback, "capability_evidence": evidence}


def discover_claude_code(
    *,
    candidates: tuple[str, ...] | None = None,
    timeout: float = CLAUDE_CODE_PROBE_TIMEOUT_S,
) -> ProviderDiscoveryResult:
    """Fetch current model IDs, then validate them against the signed-in CLI.

    Claude Code offers no account model-enumeration endpoint. The maintained
    GitHub catalog supplies candidates, never availability: each model requires
    a real (potentially billed) CLI probe. The bare CLI invocation identifies
    the account's default. If it matches no verified model, no default is
    selected. A transient catalog or CLI failure returns a typed failure and
    never promotes a cached list to current availability.
    """
    if candidates is None:
        try:
            catalog = refresh_claude_code_candidates()
        except ClaudeCodeCatalogError as exc:
            return ProviderDiscoveryResult(
                provider="claude_code",
                discovered=[],
                source=CLAUDE_CODE_SOURCE,
                failed_reason=str(exc),
            )
    else:
        # An explicit list is used by diagnostic callers and bounded live tests.
        catalog = [{"id": item, "name": item} for item in candidates]

    try:
        binary = _resolve_claude_binary()
    except ClaudeCodeCLIUnavailableError as exc:
        return ProviderDiscoveryResult(
            provider="claude_code", discovered=[], source=CLAUDE_CODE_SOURCE, failed_reason=str(exc)
        )

    bare = _probe_alias(binary, None, timeout=timeout)
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
    for candidate in catalog:
        model_id = candidate["id"]
        probe = _probe_alias(binary, model_id, timeout=timeout)
        if probe["outcome"] == "inconclusive":
            return ProviderDiscoveryResult(
                provider="claude_code",
                discovered=[],
                source=CLAUDE_CODE_SOURCE,
                failed_reason=f"model {model_id!r} probe inconclusive: {probe['reason']}",
            )
        if probe["outcome"] == "accepted":
            resolved = probe["resolved_model"]
            discovered.append(
                {
                    "id": model_id,
                    "name": candidate["name"],
                    "description": (
                        f"Resolves to {resolved}." if resolved else "Validated Claude Code model."
                    ),
                    "capabilities": list(probe.get("capabilities") or []),
                    "capability_evidence": probe.get("capability_evidence") or {},
                }
            )
            if cli_default_canonical and resolved == cli_default_canonical:
                default_model = model_id
        else:  # "rejected" -- definitive, informational, never aborts the provider
            rejected.append({"id": model_id, "reason": probe["reason"]})

    if not discovered:
        reasons = "; ".join(f"{r['id']}: {r['reason']}" for r in rejected) or "no models validated"
        return ProviderDiscoveryResult(
            provider="claude_code", discovered=[], source=CLAUDE_CODE_SOURCE, failed_reason=reasons
        )
    default_model_reason = ""
    if not default_model:
        default_model_reason = (
            f"bare CLI-default probe was {bare['outcome']} ({bare['reason']}); "
            "no account default was discovered"
            if bare["outcome"] != "accepted"
            else "bare CLI-default probe resolved to a model id not in the validated catalog; "
            "no account default was discovered"
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
    "CLAUDE_CODE_PROBE_TIMEOUT_S",
    "ClaudeCodeCLIUnavailableError",
    "discover_claude_code",
]
