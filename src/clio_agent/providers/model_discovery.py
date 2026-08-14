"""Provider model-catalog discovery + the refresh overlay (iowarp/clio-agent#1211).

The static per-provider model lists in :mod:`clio_agent.providers.catalog` are a
compiled-in snapshot; CLI-routed accounts (codex, claude_code) rotate their served
model ids independently of a clio release, so a snapshot goes stale (iowarp/clio-
agent#1184: the catalog offered ``gpt-5.5``/``gpt-5.5-codex``/``gpt-5.1`` after the
ChatGPT channel had moved on to ``gpt-5.6-sol``). This module is the single owner of:

* the per-provider **discovery mechanisms**, each verified live against the
  installed CLI (#1211 issue comment, 2026-08-13/14): codex's warm ``app-server``
  exposes a real ``model/list`` JSON-RPC method (:func:`discover_codex`);
  claude_code has no enumeration endpoint, so its catalog rows ARE the CLI's
  documented ``--model`` alias vocabulary (``fable``/``opus``/``sonnet``/
  ``haiku``) and "refresh" means **probe-validating** each alias with one
  trivial ``claude -p`` turn (:func:`discover_claude_code`) — a rejected alias
  comes back as a typed 404-shaped error in the CLI's own JSON envelope, the
  universal probe-validation oracle; HTTP backends reuse the existing live
  handshake `/models` path (:func:`discover_http`).
* the **refresh overlay** persisted to the user data dir (a JSON file sibling to
  the handshake's ``model_limits.json`` — see
  :mod:`clio_agent.providers.handshake.sources.db`), read overlay-first /
  static-fallback by ``GET /v1/providers/{id}/models``
  (:mod:`clio_agent.gact.routes.providers`) and consulted — never live-reprobed —
  by the passive handshake seam (:mod:`clio_agent.providers.handshake.cli_catalog`).

No-silent-fallback (CLAUDE.md cleanup-program ground rule): a probe failure for
one provider NEVER clears that provider's existing overlay entry — the previous
good list plus a typed ``failed_reason`` are both recorded, and a malformed
on-disk overlay raises :class:`OverlayMalformedError` rather than silently
degrading to ``{}`` (the #1202 ``_read_mcp_yaml`` lesson).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clio_agent.providers.catalog import Provider, as_cloud_api_key_env, iter_providers

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: The documented Claude Code CLI model aliases (verified live via ``claude --help``
#: 2.1.228: "--model <model> ... Provide an alias for the latest model (e.g.
#: 'fable', 'opus', or 'sonnet')..."). ``fable`` is the CLI's own CURRENT default
#: (verified empirically 2026-08-14: a bare ``claude -p`` call with no ``--model``
#: resolves to ``claude-fable-5``) — probed first, and :func:`discover_claude_code`
#: also runs one bare (no ``--model``) call to learn which alias that resolves to,
#: so the reported default follows the CLI's own choice rather than a guess
#: (#1211: "the CLI's own default, not our guess").
CLAUDE_CODE_ALIAS_CANDIDATES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

CODEX_SOURCE = "codex_app_server"
CLAUDE_CODE_SOURCE = "claude_code_alias_probe"
HTTP_SOURCE = "live_handshake"

#: One trivial, cheap turn used to probe-validate a claude_code alias/model id.
_PROBE_PROMPT = "reply with the single word: ok"


class OverlayMalformedError(RuntimeError):
    """The on-disk model-catalog overlay exists but is not a valid JSON object.

    Raised instead of silently degrading to ``{}`` (the #1202 ``_read_mcp_yaml``
    lesson). The diagnostic ``GET``/``POST`` routes let this surface as a typed
    HTTP error; the passive/ambient handshake read path
    (:mod:`clio_agent.providers.handshake.cli_catalog`) catches it and falls back
    to the static registry catalog, documenting that decision explicitly.
    """


class ClaudeCodeCLIUnavailableError(RuntimeError):
    """Raised when the ``claude`` binary isn't on PATH at probe time."""


@dataclass
class ProviderDiscoveryResult:
    """One provider's discovery outcome — the ``POST .../refresh`` per-row shape.

    ``discovered`` and ``default_model`` are only meaningful when
    ``failed_reason`` is unset; a failed probe carries an empty ``discovered``
    list and :func:`record_refresh` keeps whatever the overlay already held.
    """

    provider: str
    discovered: list[dict[str, str]]
    source: str
    default_model: str = ""
    failed_reason: str | None = None
    #: Individually-rejected candidates on an otherwise-successful probe (e.g. one
    #: claude_code alias 404s while the others validate) — informational, never
    #: silently dropped.
    rejected: list[dict[str, str]] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- #
# The overlay: read, dual-key lookup, atomic merge-write with delta reporting.
# --------------------------------------------------------------------------- #


def overlay_path() -> Path:
    """The writable overlay file: ``CLIO_MODEL_CATALOG`` env override, else the
    user data dir — a sibling of the handshake's ``model_limits.json``
    (:func:`clio_agent.providers.handshake.sources.db.db_path`)."""
    from clio_agent import (
        conf,  # noqa: PLC0415 - avoid import cycle at module load
        paths,  # noqa: PLC0415 - avoid import cycle at module load
    )

    override = conf.resolve(
        "paths.model_catalog", env="CLIO_MODEL_CATALOG", default="", cast=conf.as_str
    ).strip()
    if override:
        return Path(override).expanduser()
    return paths.user_data_dir() / "model_catalog.json"


def read_overlay() -> dict[str, dict[str, Any]]:
    """Return the raw overlay dict (``{}`` when the file is absent).

    Raises :class:`OverlayMalformedError` when the file exists but is not valid
    JSON or is not a JSON object — never silently swallowed to ``{}``.
    """
    path = overlay_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OverlayMalformedError(f"model catalog overlay unreadable at {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OverlayMalformedError(
            f"model catalog overlay at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise OverlayMalformedError(
            f"model catalog overlay at {path} must be a JSON object keyed by provider id"
        )
    return data


def overlay_models_wire(provider_id: str, provider_kind: str) -> dict[str, Any] | None:
    """Return the overlay-first ``{"models", "source", ...}`` wire dict, or ``None``.

    Looks up by preset id first, then bare provider_kind (mirrors
    :func:`clio_agent.providers.catalog.as_provider_models_dict`'s dual-keying).
    ``None`` means "no usable overlay entry" (absent, or present with an empty
    ``models`` list because this provider has never successfully discovered) —
    callers fall back to the static catalog. Propagates
    :class:`OverlayMalformedError` for a corrupt on-disk file (never silently {}).
    """
    db = read_overlay()
    entry = db.get(provider_id) or db.get(provider_kind)
    if not isinstance(entry, dict):
        return None
    models = entry.get("models")
    if not isinstance(models, list) or not models:
        return None
    return {
        "models": models,
        "source": str(entry.get("source") or "overlay"),
        "default_model": str(entry.get("default_model") or ""),
        "generated_at": str(entry.get("generated_at") or ""),
    }


def resolve_cloud_api_key(provider_kind: str) -> str:
    """Resolve a cloud provider's API key: its dedicated env var, else ``CLIO_LM_API_KEY``."""
    env_name = as_cloud_api_key_env().get(provider_kind, "")
    key = os.environ.get(env_name, "") if env_name else ""
    return key or os.environ.get("CLIO_LM_API_KEY", "")


def record_refresh(result: ProviderDiscoveryResult) -> dict[str, Any]:
    """Merge one provider's discovery result into the overlay; return its wire row.

    A probe failure (``result.failed_reason`` set, ``discovered`` empty) NEVER
    clears an existing ``models`` entry — the prior good list is kept verbatim
    and the failure is recorded alongside it (no-silent-fallback). A successful
    discovery overwrites ``models``/``source``/``default_model``/``generated_at``
    and clears any stale ``failed_reason``. ``added``/``removed``/``unchanged``
    are the model-id delta against whatever the entry held before this call —
    empty added/removed on a failed probe, since the served list didn't change.
    Propagates :class:`OverlayMalformedError` — a refresh cannot safely
    read-modify-write onto a corrupt file; it must surface, never silently
    discard whatever other providers' good data the file held.
    """
    path = overlay_path()
    with _LOCK:
        db = read_overlay()
        previous = db.get(result.provider)
        previous_models = previous.get("models") if isinstance(previous, dict) else None
        previous_ids = {
            str(m["id"]) for m in (previous_models or []) if isinstance(m, dict) and m.get("id")
        }
        entry: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
        if result.failed_reason:
            entry["failed_reason"] = result.failed_reason
            entry["last_attempt_at"] = result.generated_at
            new_ids = previous_ids  # nothing served changes on a failed probe
        else:
            entry["models"] = result.discovered
            entry["source"] = result.source
            entry["default_model"] = result.default_model
            entry["generated_at"] = result.generated_at
            entry.pop("failed_reason", None)
            new_ids = {str(m["id"]) for m in result.discovered if m.get("id")}
        db[result.provider] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(db, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    wire: dict[str, Any] = {
        "provider": result.provider,
        "discovered": entry.get("models", []),
        "source": entry.get("source", result.source),
        "default_model": entry.get("default_model", ""),
        "generated_at": result.generated_at,
        "added": sorted(new_ids - previous_ids),
        "removed": sorted(previous_ids - new_ids),
        "unchanged": sorted(new_ids & previous_ids),
    }
    if result.failed_reason:
        wire["failed_reason"] = result.failed_reason
    if result.rejected:
        wire["rejected"] = result.rejected
    return wire


# --------------------------------------------------------------------------- #
# codex: the real model/list app-server RPC.
# --------------------------------------------------------------------------- #


def discover_codex(*, timeout: float = 20.0) -> ProviderDiscoveryResult:
    """Refresh codex's live model catalog via the warm app-server's ``model/list`` RPC.

    Verified live 2026-08-14 against codex-cli 0.147.0: the account's REAL current
    catalog (``gpt-5.6-sol`` default, ``gpt-5.6-terra``, ``gpt-5.6-luna``,
    ``gpt-5.5``, ``gpt-5.4``, ``gpt-5.4-mini``, ``gpt-5.3-codex-spark``) — none of
    which is ``gpt-5.5-codex``/``gpt-5.1`` (#1184's stale, rejected pins).
    Synchronous+blocking (the app-server bridge's JSON-RPC call is a blocking
    socket read); callers on the async refresh path wrap this in
    ``asyncio.to_thread``. Uses a dedicated pool key (``model="", cwd=None``) —
    ``model/list`` needs no ``thread/start``, so this warm process is independent
    of any real-turn process.
    """
    from clio_agent.providers.codex_app_server import _APP_SERVER_POOL, CodexAppServerError
    from clio_agent.providers.codex_litellm import (  # noqa: PLC0415
        CodexCLIUnavailableError,
        _resolve_codex_binary,
    )

    try:
        binary = _resolve_codex_binary()
    except CodexCLIUnavailableError as exc:
        return ProviderDiscoveryResult(
            provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
        )
    process = _APP_SERVER_POOL.process_for(binary=binary, model="", cwd=None)
    try:
        rows = process.list_models(timeout=timeout)
    except CodexAppServerError as exc:
        return ProviderDiscoveryResult(
            provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
        )
    discovered = [
        {
            "id": str(m.get("id") or ""),
            "name": str(m.get("displayName") or m.get("id") or ""),
            "description": str(m.get("description") or ""),
        }
        for m in rows
        if isinstance(m, dict) and m.get("id")
    ]
    if not discovered:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason="codex app-server model/list returned zero models",
        )
    default_model = next(
        (str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("isDefault")),
        discovered[0]["id"],
    )
    return ProviderDiscoveryResult(
        provider="codex", discovered=discovered, source=CODEX_SOURCE, default_model=default_model
    )


# --------------------------------------------------------------------------- #
# claude_code: no enumeration -- probe-validate the documented alias vocabulary.
# --------------------------------------------------------------------------- #


def _resolve_claude_binary() -> str:
    """Return an absolute path to the ``claude`` binary or raise, Windows-shim-aware.

    Mirrors :func:`clio_agent.providers.codex_litellm._resolve_codex_binary`'s
    ``.cmd``-preference reasoning (a bare ``shutil.which`` can return an
    un-exec-able wrapper on Windows).
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
    """Run one trivial ``claude -p`` turn, probing ``alias`` (or the bare CLI default).

    Never raises. Parses the CLI's own ``--output-format json`` envelope — the
    universal probe-validation oracle (#1211 comment): ``is_error`` +
    ``api_error_status`` + a human ``result`` message on rejection (live example,
    2026-08-14, CLI 2.1.228, ``--model definitely-not-a-real-model-xyz``:
    ``{"is_error": true, "api_error_status": 404, "result": "There's an issue
    with the selected model (definitely-not-a-real-model-xyz). It may not exist
    or you may not have access to it. Run --model to pick a different
    model."}``); on acceptance ``modelUsage`` is keyed by the RESOLVED canonical
    model id (e.g. alias ``"haiku"`` resolves to
    ``"claude-haiku-4-5-20251001"``), which is how :func:`discover_claude_code`
    learns the CLI's live default without guessing. Exit code is NOT a reliable
    signal — a rejected model still exits 0 with ``is_error: true`` in the body.
    """
    args = [binary, "-p", _PROBE_PROMPT]
    if alias:
        args += ["--model", alias]
    args += ["--output-format", "json"]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user-controlled input
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "resolved_model": "", "reason": f"probe timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "resolved_model": "", "reason": f"probe failed to launch: {exc}"}
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return {
            "ok": False,
            "resolved_model": "",
            "reason": f"non-JSON response (exit={proc.returncode}): {proc.stdout[:200]!r}",
        }
    if payload.get("is_error"):
        status = payload.get("api_error_status")
        reason = str(payload.get("result") or f"api_error_status={status}")
        return {"ok": False, "resolved_model": "", "reason": reason}
    resolved = next(iter(payload.get("modelUsage") or {}), "")
    return {"ok": True, "resolved_model": str(resolved), "reason": ""}


def discover_claude_code(
    *,
    candidates: tuple[str, ...] = CLAUDE_CODE_ALIAS_CANDIDATES,
    timeout: float = 60.0,
) -> ProviderDiscoveryResult:
    """Refresh claude_code's alias catalog by probe-validating each documented alias.

    No enumeration endpoint exists for this channel (#1211 comment) — the catalog
    rows ARE the CLI's documented ``--model`` alias vocabulary, and "refresh"
    means running one trivial turn per alias and recording which ones the
    account currently accepts. Sequential (each is a real, billed API call).
    Runs one extra BARE call (no ``--model``) first to learn the CLI's own
    current default by resolved-canonical-id match, so ``default_model`` follows
    the CLI's choice rather than a guess (#1211). A rejected alias is recorded
    in ``rejected`` (informational) without failing the whole provider, as long
    as at least one alias validates; zero validating aliases is a typed
    ``failed_reason``.
    """
    try:
        binary = _resolve_claude_binary()
    except ClaudeCodeCLIUnavailableError as exc:
        return ProviderDiscoveryResult(
            provider="claude_code", discovered=[], source=CLAUDE_CODE_SOURCE, failed_reason=str(exc)
        )

    bare = _probe_claude(binary, None, timeout=timeout)
    cli_default_canonical = bare["resolved_model"] if bare["ok"] else ""

    discovered: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    default_model = ""
    for alias in candidates:
        probe = _probe_claude(binary, alias, timeout=timeout)
        if probe["ok"]:
            resolved = probe["resolved_model"]
            discovered.append(
                {
                    "id": alias,
                    "name": f"Claude {alias.capitalize()} (Claude Code alias)",
                    "description": (
                        f"Resolves to {resolved}." if resolved else "Validated Claude Code alias."
                    ),
                }
            )
            if cli_default_canonical and resolved == cli_default_canonical:
                default_model = alias
        else:
            rejected.append({"id": alias, "reason": probe["reason"]})

    if not discovered:
        reasons = "; ".join(f"{r['id']}: {r['reason']}" for r in rejected) or "no aliases validated"
        return ProviderDiscoveryResult(
            provider="claude_code", discovered=[], source=CLAUDE_CODE_SOURCE, failed_reason=reasons
        )
    if not default_model:
        default_model = discovered[0]["id"]
    return ProviderDiscoveryResult(
        provider="claude_code",
        discovered=discovered,
        source=CLAUDE_CODE_SOURCE,
        default_model=default_model,
        rejected=rejected,
    )


# --------------------------------------------------------------------------- #
# HTTP backends: reuse the existing live handshake path.
# --------------------------------------------------------------------------- #


async def discover_http(preset: Provider, *, api_key: str) -> ProviderDiscoveryResult:
    """Refresh an HTTP-backed provider's catalog via the existing live handshake path.

    Reuses :func:`clio_agent.providers.handshake.run_handshake` — the SAME
    mechanism ``GET /v1/providers/{id}/models`` already calls — with
    ``force=True``: a refresh explicitly bypasses the handshake TTL cache.
    """
    from clio_agent.providers.handshake import HandshakeContext, run_handshake  # noqa: PLC0415

    ctx = HandshakeContext(
        provider_id=preset.id,
        provider_kind=preset.provider_kind,
        api_base=preset.api_base,
        api_key=api_key,
        auth_mode="passive",
        allow_external_sources=True,
    )
    report = await run_handshake(ctx, force=True)
    wire = report.to_models_wire()
    models = wire.get("models") or []
    if not models:
        reason = report.error or (
            f"connectivity={report.connectivity.value} auth={report.auth.value}"
        )
        return ProviderDiscoveryResult(
            provider=preset.id, discovered=[], source=HTTP_SOURCE, failed_reason=reason
        )
    discovered = [
        {"id": str(m["id"]), "name": str(m.get("name") or m["id"]), "description": ""}
        for m in models
        if m.get("id")
    ]
    return ProviderDiscoveryResult(provider=preset.id, discovered=discovered, source=HTTP_SOURCE)


# --------------------------------------------------------------------------- #
# The refresh action: every configured provider, concurrently.
# --------------------------------------------------------------------------- #


async def refresh_all(presets: list[Provider] | None = None) -> list[dict[str, Any]]:
    """Probe every catalog preset concurrently; merge each result into the overlay.

    Runs one discovery coroutine per preset (CLI probes for codex/claude_code,
    the live handshake for everything else) via ``asyncio.gather`` so wall-clock
    is bounded by the SLOWEST single provider, not their sum — claude_code's five
    sequential CLI turns, codex's single RPC, and every HTTP handshake's bounded
    timeout all run in parallel. A crash inside one provider's coroutine (the
    ``discover_*`` functions are designed to never raise, but a defensive
    backstop) is caught and reported as a typed ``failed_reason`` rather than
    aborting the other providers' refresh.
    """
    all_presets = list(presets) if presets is not None else list(iter_providers())

    async def _one(preset: Provider) -> ProviderDiscoveryResult:
        try:
            if preset.provider_kind == "codex":
                return await asyncio.to_thread(discover_codex)
            if preset.provider_kind == "claude_code":
                return await asyncio.to_thread(discover_claude_code)
            return await discover_http(preset, api_key=resolve_cloud_api_key(preset.provider_kind))
        except Exception as exc:  # noqa: BLE001 - one provider's crash must not sink the refresh
            logger.warning("model discovery crashed for provider=%s: %s", preset.id, exc)
            return ProviderDiscoveryResult(
                provider=preset.id,
                discovered=[],
                source="error",
                failed_reason=f"discovery crashed: {exc}",
            )

    results = await asyncio.gather(*(_one(p) for p in all_presets))
    return [record_refresh(r) for r in results]


__all__ = [
    "CLAUDE_CODE_ALIAS_CANDIDATES",
    "ClaudeCodeCLIUnavailableError",
    "OverlayMalformedError",
    "ProviderDiscoveryResult",
    "discover_claude_code",
    "discover_codex",
    "discover_http",
    "overlay_models_wire",
    "overlay_path",
    "read_overlay",
    "record_refresh",
    "refresh_all",
    "resolve_cloud_api_key",
]
