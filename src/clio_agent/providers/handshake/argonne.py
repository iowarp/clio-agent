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
  no models.dev fallback needed. Mapping that row onto the capability records
  is :mod:`clio_agent.providers.capabilities.dialects.alcf`'s job (reusing
  :mod:`.vllm`'s own ``max_model_len``/``root`` parsing for the fields this
  gateway shares with vanilla vLLM); this class owns only what's genuinely
  ALCF-specific -- OAuth, ``/jobs`` discovery, and the request plumbing. A
  companion ``/jobs`` endpoint reports which models are currently hot (a
  running vLLM job), which we fold into ``is_loaded``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.dialects import alcf as alcf_dialect
from clio_agent.providers.capabilities.records import ModelCapabilities
from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    DiscoveryAuthRejected,
    HandshakeContext,
    ProviderHandshake,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    DiscoveredModelFacts,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Environment variables checked (in order) for a pre-supplied bearer token.
#: These let a user or a batch job inject a token without the Globus flow.
_TOKEN_ENV_VARS: tuple[str, ...] = ("CLIO_ARGONNE_TOKEN", "ALCF_INFERENCE_TOKEN")

#: The gateway path segment that separates the public root from the
#: per-cluster routing (``.../resource_server/<cluster>/<framework>/v1``).
_RESOURCE_SERVER = "/resource_server"

logger = logging.getLogger(__name__)

#: Typed reasons the passive token lookup reports instead of a bare ``None``
#: (no-silent-fallback): the code is the queryable fact, the sentence is what a
#: person reads. ``argonne_token_missing`` is the ordinary signed-out state and
#: is carried by ``AuthState.MISSING`` alone; the other two mean a stored sign-in
#: exists but could not be used, which used to vanish into ``DEFERRED``.
PASSIVE_TOKEN_REASONS: dict[str, str] = {
    "argonne_token_missing": (
        "no ALCF token is set in the environment and no Globus sign-in is stored"
    ),
    "argonne_stored_token_unusable": (
        "a Globus sign-in is stored but could not produce an access token without signing in again"
    ),
    "argonne_stored_token_empty": "the stored Globus sign-in returned an empty access token",
    "argonne_sdk_missing": (
        "the 'argonne' extra (globus-sdk) is not installed, so the stored Globus "
        "sign-in cannot be used"
    ),
}

#: ALCF's inference API's own 401/403 body, e.g. the "high-assurance timeout"
#: policy rejection: {"error": {"code": "unauthorized", "message": "..."}}.
_ALCF_REAUTH_REASON = "argonne_reauthentication_required"


@dataclass(frozen=True)
class PassiveTokenLookup:
    """Outcome of the passive (never interactive) ALCF token lookup.

    Attributes:
        token: The bearer token, or ``None`` when none is usable right now.
        reason: A :data:`PASSIVE_TOKEN_REASONS` code when ``token`` is ``None``.
        detail: The underlying error text for an unusable stored sign-in.
    """

    token: str | None
    reason: str = ""
    detail: str = ""

    @property
    def error(self) -> str | None:
        """Typed, human-readable failure for a stored-but-unusable sign-in, else ``None``."""

        if self.token is not None or self.reason in {"", "argonne_token_missing"}:
            return None
        message = f"{self.reason}: {PASSIVE_TOKEN_REASONS[self.reason]}"
        return f"{message} ({self.detail})" if self.detail else message


def _alcf_error_detail(response: Any) -> str:
    """Extract ALCF's own error message from a 401/403 ``/models`` response.

    ALCF's usual shape is ``{"error": {"code": "unauthorized", "message":
    "Error: Permission denied from internal policies. This is likely due to a
    high-assurance timeout. Please logout ... and re-authenticate ..."}}`` --
    the "high-assurance timeout" case is the common one this exists for. Any
    other 401/403 body still yields a typed reason: the raw text when the body
    isn't that shape, never a bare HTTP status.
    """

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - non-JSON body: fall back to raw text
        return str(getattr(response, "text", "") or f"HTTP {response.status_code}")
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
        if payload.get("message"):
            return str(payload["message"])
    return str(payload)


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
    def _resolve_passive_token() -> PassiveTokenLookup:
        """Resolve a bearer token without any interactive or network OAuth flow.

        Checks the environment first, then an already-stored Globus token. When
        nothing is usable the lookup carries a typed :data:`PASSIVE_TOKEN_REASONS`
        code — a stored sign-in that fails to produce a token is reported (and
        logged) rather than collapsed into a bare ``None``.
        """
        for var in _TOKEN_ENV_VARS:
            value = os.environ.get(var)
            if value:
                return PassiveTokenLookup(token=value.strip())
        # Fall back to an already-stored Globus token. ``tokens_exist`` is a
        # cheap on-disk check that does not import globus-sdk; only when a token
        # is present do we ask for it (force_refresh=False never prompts).
        from clio_agent.providers import argonne_auth  # noqa: PLC0415

        if not argonne_auth.tokens_exist():
            return PassiveTokenLookup(token=None, reason="argonne_token_missing")
        try:
            token = argonne_auth.get_access_token(False, allow_interactive=False)
        except argonne_auth.GlobusUnavailable as exc:
            # Distinct from an unusable stored token: no amount of re-signing-in
            # helps here -- the 'argonne' extra itself is not installed.
            lookup = PassiveTokenLookup(token=None, reason="argonne_sdk_missing", detail=str(exc))
            logger.warning("argonne passive token lookup failed: %s", lookup.error)
            return lookup
        except Exception as exc:  # noqa: BLE001 - any refresh failure becomes a typed reason
            # A stored token that fails to validate offline (expired refresh,
            # network) is "deferred, not usable now" -- and the reason travels
            # with it instead of vanishing.
            lookup = PassiveTokenLookup(
                token=None, reason="argonne_stored_token_unusable", detail=str(exc)
            )
            logger.warning("argonne passive token lookup failed: %s", lookup.error)
            return lookup
        if not token:
            lookup = PassiveTokenLookup(token=None, reason="argonne_stored_token_empty")
            logger.warning("argonne passive token lookup failed: %s", lookup.error)
            return lookup
        return PassiveTokenLookup(token=token)

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
        # Prefer a token the caller already resolved: the bind path mints the
        # Globus bearer before the handshake and passes it via ``ctx.api_key``,
        # so reusing it avoids a redundant cold token resolution/refresh on the
        # first bind (which otherwise fell back to the static config).
        provided = (ctx.api_key or "").strip()
        if provided and provided not in {"x", "lm-studio"}:
            return ConnectivityResult(
                connectivity=ConnectivityState.OK,
                auth=AuthState.OK,
                auth_header={"Authorization": f"Bearer {provided}"},
            )
        lookup = self._resolve_passive_token()
        token = lookup.token

        if token is None and ctx.auth_mode == "active":
            # Active bind: allow a non-interactive refresh. This may use a
            # stored refresh token but must never open a browser.
            from clio_agent.providers import argonne_auth  # noqa: PLC0415

            try:
                refreshed = argonne_auth.get_access_token(False, allow_interactive=False)
            except Exception as exc:  # noqa: BLE001 - token refresh failure surfaced as SKIPPED connectivity
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
                error=lookup.error,
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
        if models_resp.status_code in (401, 403):
            raise DiscoveryAuthRejected(_ALCF_REAUTH_REASON, _alcf_error_detail(models_resp))
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
        except Exception:  # noqa: BLE001 - job listing failure yields an empty set
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
    ) -> DiscoveredModelFacts:
        """Build a :class:`DiscoveredModelFacts` from one ALCF model row.

        Every value here is self-reported by the vLLM backend the gateway
        fronts, and per brief Part 6 this is all DEPLOYMENT evidence -- how
        THIS server is currently running the model -- not a fact about the
        weights themselves. The mapping itself lives in
        :func:`clio_agent.providers.capabilities.dialects.alcf.
        parse_gateway_model_row` (reusing :mod:`.vllm`'s own ``max_model_len``/
        ``root`` reading for the fields ALCF's gateway shares with vanilla
        vLLM); this method reads and parses nothing itself. The model's own
        mechanism/ceiling stay unknown here (no HF/overlay layer exists in
        this slice); ``enrich_capabilities`` fills the ceiling from the
        community-catalog cascade.
        """
        model_id = str(raw.get("id") or "")
        observed_at = _now_iso()

        deployment = alcf_dialect.parse_gateway_model_row(
            raw, provider_id=ctx.provider_id, api_base=ctx.api_base, observed_at=observed_at
        )
        model = ModelCapabilities(model_key=deployment.model_key.value or model_id)

        reasoning_parser, tool_call_parser = alcf_dialect.gateway_row_identity(raw)
        discovered = DiscoveredModel(
            id=model_id,
            is_loaded=bool(raw.get("is_loaded")),
            raw={
                **dict(raw),
                "reasoning_parser": reasoning_parser,
                "tool_call_parser": tool_call_parser,
            },
        )
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)

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
        token = self._resolve_passive_token().token
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}
