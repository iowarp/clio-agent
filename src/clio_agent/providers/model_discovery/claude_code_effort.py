"""Claude Code per-model effort levels, read from the CLI's own model catalog.

The Claude Code CLI answers the Agent SDK's ``initialize`` control request with
a ``models`` list whose rows carry ``supportedEffortLevels`` (a subset of
``low|medium|high|xhigh|max``) and ``supportsAdaptiveThinking`` -- the same
per-model truth the CLI's own ``/model`` picker uses (verified on the bundled
CLI of claude-agent-sdk 0.2.156: Opus/Sonnet/Fable report all five, Haiku
reports none). Reading it opens one SDK session and closes it; no model turn
runs. The maintained catalog stays the source of which models exist; this only
annotates those rows with their effort levels.

A failed read never invents levels: every row carries a typed
``effort_evidence_failure`` and the catalog then offers the thinking-budget
ladder, which is what the transport really sends without effort evidence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Seconds the CLI ``initialize`` read may take before it is abandoned.
CLI_MODEL_CATALOG_TIMEOUT_S = 30.0

_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


async def _initialize_models(timeout: float) -> list[dict[str, Any]]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # noqa: PLC0415

    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        skills=[],
        plugins=[],
        setting_sources=[],
        permission_mode="bypassPermissions",
    )

    async def _read() -> list[dict[str, Any]]:
        async with ClaudeSDKClient(options) as client:
            info = await client.get_server_info() or {}
        models = info.get("models")
        return [row for row in models if isinstance(row, dict)] if isinstance(models, list) else []

    return await asyncio.wait_for(_read(), timeout=timeout)


def read_cli_model_catalog(
    timeout: float = CLI_MODEL_CATALOG_TIMEOUT_S,
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(cli_models, failure)``; ``failure`` is a typed reason or ``""``."""

    try:
        models = asyncio.run(_initialize_models(timeout))
    except Exception as exc:  # noqa: BLE001 - any read failure becomes a typed reason
        failure = f"claude_code_cli_model_catalog_unavailable: {exc}"
        logger.warning("claude_code effort levels: %s", failure)
        return [], failure
    if not models:
        return [], "claude_code_cli_model_catalog_empty: initialize reported no models"
    return models, ""


def _wire_id(value: str) -> str:
    return value.removesuffix("[1m]")


def attach_effort_levels(
    rows: list[dict[str, Any]], cli_models: list[dict[str, Any]], failure: str
) -> list[dict[str, Any]]:
    """Annotate catalog rows with the CLI's per-model effort evidence.

    A row matches a CLI model by its canonical wire id (``resolvedModel``) or by
    the model's selectable value. Matched rows get ``supported_effort_levels``
    (empty when the CLI reports no effort support) and the CLI ``cli_values``
    that select them (e.g. ``sonnet``), so a configured alias resolves to its
    row. Unmatched rows, or every row after a failed read, carry a typed
    ``effort_evidence_failure``.
    """

    annotated: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        row_id = str(row.get("id") or "")
        matches = [
            model
            for model in cli_models
            if row_id
            and row_id
            in {
                _wire_id(str(model.get("resolvedModel") or "")),
                _wire_id(str(model.get("value") or "")),
            }
        ]
        if failure:
            row["effort_evidence_failure"] = failure
        elif not matches:
            row["effort_evidence_failure"] = (
                "claude_code_cli_model_unlisted: the Claude Code CLI does not list this model"
            )
        else:
            levels = {
                str(level)
                for model in matches
                if model.get("supportsEffort")
                for level in model.get("supportedEffortLevels") or []
            }
            row["supported_effort_levels"] = [lvl for lvl in _EFFORT_LEVELS if lvl in levels]
            row["cli_values"] = sorted(
                {
                    str(model.get("value"))
                    for model in matches
                    if model.get("value") and model.get("value") != "default"
                }
            )
            row.pop("effort_evidence_failure", None)
        annotated.append(row)
    return annotated


__all__ = ["CLI_MODEL_CATALOG_TIMEOUT_S", "attach_effort_levels", "read_cli_model_catalog"]
