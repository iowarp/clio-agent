"""Argonne / ALCF inference-gateway handshake.

ALCF fronts a fleet of vLLM-backed model servers (Sophia today; Metis, Polaris
and Aurora behind the same gateway) at

    https://inference-api.alcf.anl.gov/resource_server/<cluster>/<framework>/v1

Reaching them requires a short-lived Globus bearer token tied to an
``anl.gov`` / ``alcf.anl.gov`` identity. Two facts shape this handshake:

* **Auth is OAuth and must never block.** ``passive`` mode (health / doctor)
  may only resolve a token that is *already* available — from the environment
  (``CLIO_ARGONNE_TOKEN`` / ``ALCF_INFERENCE_TOKEN``) or an already-stored
  Globus refresh token — and otherwise reports ``SKIPPED`` without touching the
  network. ``active`` mode (explicit bind) may ask :mod:`argonne_auth` for a
  *non-interactive* token refresh, but still never pops a browser.

* **The model list is unusually rich.** Unlike a stock OpenAI ``/models``,
  the ALCF gateway returns per-model vLLM config — ``max_model_len``,
  ``reasoning_parser``, ``tool_call_parser``, ``enable_auto_tool_choice`` — so
  ``context_window`` and the reasoning / native-tool flags resolve *live* with
  no models.dev fallback needed. A companion ``/jobs`` endpoint reports which
  models are currently hot (a running vLLM job), which we fold into
  ``is_loaded``.
"""

from __future__ import annotations

import os
from typing import Any

from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    HandshakeContext,
    ProviderHandshake,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    ModelProfile,
)

#: Environment variables checked (in order) for a pre-supplied bearer token.
#: These let a user or a batch job inject a token without the Globus flow.
_TOKEN_ENV_VARS: tuple[str, ...] = ("CLIO_ARGONNE_TOKEN", "ALCF_INFERENCE_TOKEN")

#: The gateway path segment that separates the public root from the
#: per-cluster routing (``.../resource_server/<cluster>/<framework>/v1``).
_RESOURCE_SERVER = "/resource_server"


class ArgonneHandshake(ProviderHandshake):
    """Handshake for the ALCF / Argonne inference gateway.

    Connectivity is auth-mode aware and OAuth-safe; model discovery reads the
    gateway's rich per-model vLLM config and marks hot models from ``/jobs``.
    """

    @staticmethod
    def _split_api_base(api_base: str) -> tuple[str, str]:
        """Split an ALCF ``api_base`` into ``(gateway_root, cluster)``.

        ``gateway_root`` is the URL up to and including ``/resource_server``;
        ``cluster`` is the path segment immediately after it (e.g. ``sophia``).

        Args:
            api_base: e.g.
                ``https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1``.

        Returns:
            ``(gateway_root, cluster)``. ``cluster`` is ``""`` when the base
            does not extend past ``/resource_server``.

        Raises:
            ValueError: ``api_base`` does not contain ``/resource_server``.
        """
        marker_idx = api_base.find(_RESOURCE_SERVER)
        if marker_idx == -1:
            raise ValueError(f"Argonne api_base must contain '{_RESOURCE_SERVER}': {api_base!r}")
        root_end = marker_idx + len(_RESOURCE_SERVER)
        gateway_root = api_base[:root_end]
        remainder = api_base[root_end:].strip("/")
        cluster = remainder.split("/", 1)[0] if remainder else ""
        return gateway_root, cluster

    @staticmethod
    def _resolve_passive_token() -> str | None:
        """Resolve a bearer token without any interactive or network OAuth flow.

        Checks the environment first, then an already-stored Globus token.
        Returns ``None`` when nothing is available — the caller must then report
        ``SKIPPED`` rather than probe.
        """
        for var in _TOKEN_ENV_VARS:
            value = os.environ.get(var)
            if value:
                return value.strip()
        # Fall back to an already-stored Globus token. ``tokens_exist`` is a
        # cheap on-disk check that does not import globus-sdk; only when a token
        # is present do we ask for it (force_refresh=False never prompts).
        from clio_agent.providers import argonne_auth  # noqa: PLC0415

        if not argonne_auth.tokens_exist():
            return None
        try:
            token = argonne_auth.get_access_token(False)
        except Exception:
            # A stored token that fails to validate offline (expired refresh,
            # missing globus-sdk) is treated as "deferred, not usable now".
            return None
        return token or None

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Auth-mode-aware, OAuth-safe connectivity probe.

        ``passive``: resolve a token only from the environment or an
        already-stored Globus token; if none is available, return ``SKIPPED``
        (``DEFERRED`` when a stored token exists but could not be used, else
        ``MISSING``) and make **no** network call.

        ``active``: in addition to the passive sources, may ask
        :mod:`argonne_auth` for a non-interactive token refresh.

        With a usable token we set the ``Authorization: Bearer`` header and
        report ``OK`` so the later phases authenticate once.
        """
        token = self._resolve_passive_token()

        if token is None and ctx.auth_mode == "active":
            # Active bind: allow a non-interactive refresh. This may use a
            # stored refresh token but must never open a browser.
            from clio_agent.providers import argonne_auth  # noqa: PLC0415

            try:
                refreshed = argonne_auth.get_access_token(False)
            except Exception as exc:
                return ConnectivityResult(
                    connectivity=ConnectivityState.SKIPPED,
                    auth=AuthState.MISSING,
                    error=f"argonne token unavailable: {exc}",
                )
            token = (refreshed or "").strip() or None

        if token is None:
            from clio_agent.providers import argonne_auth  # noqa: PLC0415

            stored = argonne_auth.tokens_exist()
            return ConnectivityResult(
                connectivity=ConnectivityState.SKIPPED,
                auth=AuthState.DEFERRED if stored else AuthState.MISSING,
            )

        return ConnectivityResult(
            connectivity=ConnectivityState.OK,
            auth=AuthState.OK,
            auth_header={"Authorization": f"Bearer {token}"},
        )

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """List the gateway's models, marking hot ones via ``/jobs``.

        ``GET {gateway_root}/{cluster}/models`` returns the rich per-model vLLM
        config rows. A best-effort ``GET {gateway_root}/{cluster}/jobs`` reports
        which models are currently served by a running vLLM job; their ids are
        comma-joined in each ``running[].Models`` string, and we set
        ``is_loaded`` on the matching rows.
        """
        gateway_root, cluster = self._split_api_base(ctx.api_base)
        headers = self._auth_header(ctx)

        models_resp = await client.get(f"{gateway_root}/{cluster}/models", headers=headers)
        models_resp.raise_for_status()
        rows = models_resp.json()
        if not isinstance(rows, list):
            raise ValueError(f"argonne /models returned non-list payload: {type(rows).__name__}")

        hot = await self._discover_hot_models(client, gateway_root, cluster, headers)
        if hot:
            for row in rows:
                if isinstance(row, dict) and row.get("id") in hot:
                    row["is_loaded"] = True
        return rows

    async def _discover_hot_models(
        self,
        client: Any,
        gateway_root: str,
        cluster: str,
        headers: dict[str, str],
    ) -> set[str]:
        """Best-effort set of model ids currently served by a running job.

        Failures here are swallowed: a missing or erroring ``/jobs`` endpoint
        must not sink model discovery — we just lose the hot/cold annotation.
        """
        try:
            jobs_resp = await client.get(f"{gateway_root}/{cluster}/jobs", headers=headers)
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json()
        except Exception:
            return set()

        hot: set[str] = set()
        running = jobs.get("running", []) if isinstance(jobs, dict) else []
        for job in running:
            if not isinstance(job, dict):
                continue
            joined = job.get("Models", "")
            if not isinstance(joined, str):
                continue
            for model_id in joined.split(","):
                cleaned = model_id.strip()
                if cleaned:
                    hot.add(cleaned)
        return hot

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        """Build a :class:`ModelProfile` from one ALCF model row.

        Mapping (all values self-reported by the vLLM backend, so
        ``context_source`` stays ``"live"``):

        * ``max_model_len`` -> ``context_window``
        * non-empty ``reasoning_parser`` -> ``is_reasoning=True`` +
          ``reasoning_param=<value>``
        * ``tool_call_parser`` present **or** ``enable_auto_tool_choice`` truthy
          -> ``native_tool_calling=True`` (``tool_call_parser`` carries the
          parser name when present)
        """
        model_id = raw.get("id", "")

        context_window = raw.get("max_model_len")
        if context_window is not None:
            context_window = int(context_window)

        reasoning_parser = raw.get("reasoning_parser") or None
        is_reasoning = reasoning_parser is not None

        tool_call_parser = raw.get("tool_call_parser") or None
        auto_tool = bool(raw.get("enable_auto_tool_choice"))
        native_tool_calling = tool_call_parser is not None or auto_tool

        return ModelProfile(
            id=model_id,
            context_window=context_window,
            is_reasoning=is_reasoning,
            reasoning_param=reasoning_parser,
            native_tool_calling=native_tool_calling,
            tool_call_parser=tool_call_parser,
            is_loaded=bool(raw.get("is_loaded")),
            context_source="live",
            raw=dict(raw),
        )

    # ------------------------------------------------------------------ helpers
    def _auth_header(self, ctx: HandshakeContext) -> dict[str, str]:
        """Resolve the bearer header for discovery from the context or env.

        ``ctx.extra["auth_header"]`` (carried over from the connectivity phase,
        which the dispatcher threads through) is preferred; otherwise we fall
        back to ``ctx.api_key`` or a passive token so discovery still works when
        invoked directly in a test.
        """
        carried = ctx.extra.get("auth_header")
        if isinstance(carried, dict) and carried:
            return dict(carried)
        if ctx.api_key:
            return {"Authorization": f"Bearer {ctx.api_key}"}
        token = self._resolve_passive_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}
