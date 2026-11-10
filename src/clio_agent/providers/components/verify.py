"""Fresh-interpreter provider check run after a component update.

``python -m clio_agent.providers.components.verify <provider_kind>`` imports
the just-installed component in a NEW process (the updating process still holds
the old modules) and re-runs the provider's own check and model discovery there.
It prints one JSON line ``{"ok", "code", "detail", "client"}`` and exits 0 only
when the update left a working provider.

What counts as working is "the component loads and the provider answers": a
signed-out account or an empty model list is the account's state, not a broken
install, so those pass with their code recorded. A failure to import, to read
the runtime version, or to complete the discovery exchange fails the update
(which then rolls back).
"""

from __future__ import annotations

import json
import sys
from typing import Any

#: Codex direct model-list outcomes that prove the runtime version was read and
#: the backend answered (the account's state, not a broken install).
_CODEX_WORKING_CODES = frozenset({"codex_direct_signed_out", "codex_direct_zero_models"})


def _code(failed_reason: str) -> str:
    return failed_reason.split(":", 1)[0].strip() if failed_reason else ""


def verify_codex() -> dict[str, Any]:
    """Import the Codex runtime and ask the backend for the models its version unlocks."""
    import codex_cli_bin  # noqa: F401, PLC0415

    from clio_agent.runtime.codex_windows_helpers import (  # noqa: PLC0415
        CodexWindowsHelpersError,
        ensure_bundled_codex_windows_helpers,
    )

    try:
        ensure_bundled_codex_windows_helpers()
    except CodexWindowsHelpersError as exc:
        return {
            "ok": False,
            "code": "codex_windows_helpers_unavailable",
            "detail": str(exc),
            "client": None,
        }

    from clio_agent.providers.codex.model_list import (  # noqa: PLC0415
        CodexModelListError,
        fetch_direct_models,
    )

    try:
        listed = fetch_direct_models()
    except CodexModelListError as exc:
        return {
            "ok": exc.code in _CODEX_WORKING_CODES,
            "code": exc.code,
            "detail": str(exc),
            "client": None,
        }
    return {
        "ok": True,
        "code": "codex_direct_models_listed",
        "detail": f"{len(listed.models)} models for client_version {listed.client_version}",
        "client": None,
    }


def verify_claude_code() -> dict[str, Any]:
    """Import the Agent SDK, run its CLI, and re-run the Claude Code sign-in check."""
    import claude_agent_sdk  # noqa: F401, PLC0415

    from clio_agent.providers.components.client_binary import (  # noqa: PLC0415
        bundled_claude_path,
        claude_client,
        probe_version,
    )
    from clio_agent.providers.model_discovery.claude_code import (
        discover_claude_code,  # noqa: PLC0415
    )
    from clio_agent.runtime.progress import (  # noqa: PLC0415
        ProbeUnresponsiveError,
    )

    bundled = bundled_claude_path()
    try:
        bundled_version = probe_version(str(bundled)) if bundled is not None else "-"
    except ProbeUnresponsiveError as exc:
        return {
            "ok": False,
            "code": "claude_bundled_cli_unresponsive",
            "detail": str(exc),
            "client": claude_client().to_wire(),
        }
    if not bundled_version:
        return {
            "ok": False,
            "code": "claude_bundled_cli_unrunnable",
            "detail": f"{bundled} did not report a version",
            "client": claude_client().to_wire(),
        }
    selection = claude_client()
    if selection.client is None:
        return {
            "ok": False,
            "code": selection.reason,
            "detail": "no Claude Code CLI",
            "client": selection.to_wire(),
        }
    result = discover_claude_code()
    return {
        "ok": True,
        "code": _code(result.failed_reason or "") or "claude_code_checked",
        "detail": result.failed_reason or f"{len(result.discovered)} models",
        "client": selection.to_wire(),
    }


def verify_copilot() -> dict[str, Any]:
    """Import the Copilot SDK, find its runtime, and re-read the account's model list.

    This fresh process has no CLIO GitHub token source, so the read normally ends
    signed out: that is the account's state and passes with its code. A runtime
    that is missing or fails the exchange fails the update.
    """
    import copilot  # noqa: F401, PLC0415

    from clio_agent.providers.copilot.errors import (  # noqa: PLC0415
        contains_copilot_auth_required,
        contains_copilot_dependency_error,
    )
    from clio_agent.providers.copilot.runtime import copilot_client  # noqa: PLC0415
    from clio_agent.providers.model_discovery.copilot import (  # noqa: PLC0415
        NO_ENABLED_MODELS_MESSAGE,
        discover_copilot,
    )

    selection = copilot_client()
    if selection.client is None:
        return {
            "ok": False,
            "code": selection.reason,
            "detail": "no GitHub Copilot runtime",
            "client": selection.to_wire(),
        }
    result = discover_copilot()
    failure = result.failed_reason or ""
    if not failure:
        code = "copilot_models_listed"
    elif contains_copilot_auth_required(failure):
        code = "copilot_signed_out"
    elif failure == NO_ENABLED_MODELS_MESSAGE:
        code = "copilot_zero_models"
    elif contains_copilot_dependency_error(failure):
        code = "copilot_runtime_missing"
    else:
        code = "copilot_exchange_failed"
    return {
        "ok": code in {"copilot_models_listed", "copilot_signed_out", "copilot_zero_models"},
        "code": code,
        "detail": failure or f"{len(result.discovered)} models",
        "client": selection.to_wire(),
    }


_VERIFIERS = {"codex": verify_codex, "claude_code": verify_claude_code, "copilot": verify_copilot}


def main(argv: list[str]) -> int:
    """Run the verifier for ``argv[0]`` and print its JSON line."""
    kind = argv[0] if argv else ""
    verifier = _VERIFIERS.get(kind)
    if verifier is None:
        print(json.dumps({"ok": False, "code": "provider_has_no_components", "detail": kind}))
        return 2
    try:
        outcome = verifier()
    except Exception as exc:  # noqa: BLE001 - any failure here is the verdict itself
        outcome = {"ok": False, "code": "provider_check_crashed", "detail": repr(exc)}
    print(json.dumps(outcome))
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
