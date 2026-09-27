"""Who can use a managed model server: the per-deployment API key, or a shared server.

vLLM and llama.cpp are protected by default. On install (and reinstall, which
rotates it) CLIO generates a key for the deployment, keeps it in the durable
provider-key store (:class:`~clio_agent.providers.api_key_store.ProviderApiKeyStore`,
under :func:`deployment_key_ref` -- no new store), starts the server with it
(:mod:`~clio_agent.gact.infrastructure.secret_env`: environment, never an
argument), and sends it on every request CLIO makes to that server: its own
reads of what the server has in force, and -- through the saved server's
``credential_ref`` -- discovery, probes and chat. Nobody copies it.

A person can make a deployment shareable instead (``shareable=true``): it runs
with no key, and the UI says anyone who can reach its port can use it.
Ollama has no key support; it is shown as not protected, with that reason.

After a keyed server answers, CLIO checks the key is enforced -- a request
without it must be refused. A server that accepts one is never shown as
protected.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlsplit

import httpx

from clio_agent.gact.infrastructure.models import (
    InfrastructureTarget,
    ServiceAccess,
    ServiceRecord,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.providers.api_key_store import ProviderApiKeyStore

logger = logging.getLogger(__name__)

#: Configuration key of the "Shareable (no key)" choice (``true`` / ``false``).
SHAREABLE_FIELD = "shareable"

#: The environment variable each keyed engine reads its API key from
#: (vLLM's ``--api-key`` / llama.cpp's ``--api-key`` as environment).
KEY_VARIABLES: dict[str, str] = {"vllm": "VLLM_API_KEY", "llama_cpp": "LLAMA_API_KEY"}

#: A path every keyed engine guards with its key (``/health`` is left open by both).
_GUARDED_PATH = "/v1/chat/completions"

#: The managed model servers whose access is recorded.
MODEL_SERVERS = frozenset({*KEY_VARIABLES, "ollama"})
_REFUSED = frozenset({401, 403})


def supports_api_key(service_id: str) -> bool:
    """Whether the engine can be protected by an API key (Ollama cannot)."""

    return service_id in KEY_VARIABLES


def is_shareable(configuration: dict[str, str]) -> bool:
    """Whether the person chose to run the deployment with no key."""

    return configuration.get(SHAREABLE_FIELD, "").strip().casefold() == "true"


def deployment_key_ref(target_id: str, service_id: str) -> str:
    """The secret-store id of one deployment's key."""

    return f"managed-server:{target_id}:{service_id}"


def launch_key(
    target_id: str, service_id: str, action: str, configuration: dict[str, str]
) -> str | None:
    """The key a lifecycle action launches the server with, or ``None`` for no key.

    Install and reinstall make a new key (a reinstall rotates it); the caller
    stores it (:func:`store_key`) before the server starts, so a CLIO that
    stops mid-deploy still has the key of what it started. A start re-launches
    with the stored key. A shareable deployment has no key.
    """

    if not supports_api_key(service_id) or is_shareable(configuration):
        return None
    if action in {"install", "reinstall"}:
        return secrets.token_urlsafe(32)
    if action == "start":
        return load_key(target_id, service_id) or None
    return None


def load_key(target_id: str, service_id: str) -> str:
    """A deployment's stored key (``""`` when it has none)."""

    return ProviderApiKeyStore().load(deployment_key_ref(target_id, service_id))


def store_key(target_id: str, service_id: str, key: str) -> None:
    """Keep a deployment's key in the durable secret store (``""`` removes it)."""

    if key:
        ProviderApiKeyStore().save(deployment_key_ref(target_id, service_id), key)
    else:
        forget_key(target_id, service_id)


def forget_key(target_id: str, service_id: str) -> None:
    """Remove a deployment's key (uninstall, a failed install, a shareable reinstall)."""

    ProviderApiKeyStore().clear(deployment_key_ref(target_id, service_id))


def settle_failed_launch(
    target_id: str,
    service_id: str,
    *,
    previous: str,
    launched: bool,
    cleaned_up: bool,
) -> None:
    """Keep the right key after an install or reinstall that made one failed.

    Nothing new launched: whatever server was there keeps its previous key. The
    new server was removed again: it has no key. The new server could not be
    removed: it keeps running with the new key, which stays.
    """

    if not launched:
        store_key(target_id, service_id, previous)
    elif cleaned_up:
        forget_key(target_id, service_id)


def request_headers(record: ServiceRecord) -> dict[str, str]:
    """The headers CLIO's own requests to a managed server carry (its key, when it has one)."""

    key = ProviderApiKeyStore().load(deployment_key_ref(record.target_id, record.service_id))
    return {"Authorization": f"Bearer {key}"} if key else {}


def _where(port: int | None, target: InfrastructureTarget | None) -> str:
    host = (
        target.ssh.host.strip()
        if target is not None and target.kind == "ssh" and target.ssh is not None
        else "this computer"
    )
    return f"port {port} on {host}" if port else f"its port on {host}"


def _root(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return f"{parts.scheme.lower()}://{(parts.netloc or '').lower()}{path}"


async def check_access(
    record: ServiceRecord,
    *,
    port: int | None,
    target: InfrastructureTarget | None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> ServiceAccess:
    """Who can use ``record``'s server, checked against the running server.

    A keyed server must refuse a request without its key and accept CLIO's;
    one that accepts a request without it is reported ``unprotected``.
    """

    where = _where(port, target)
    if not supports_api_key(record.service_id):
        label = "Ollama" if record.service_id == "ollama" else record.service_id
        return ServiceAccess(
            mode="unprotected",
            detail=(
                f"Not protected: {label} has no API key support, so anyone who can reach "
                f"{where} can use it."
            ),
        )
    if is_shareable(record.configuration):
        return ServiceAccess(
            mode="shared",
            detail=f"Shared with no key: anyone who can reach {where} can use it.",
        )
    protected = (
        "Protected by a key CLIO made for this deployment. CLIO sends it on every request; "
        "nobody needs to copy it."
    )
    headers = request_headers(record)
    if not headers:
        logger.warning(
            "managed server access: reason=api_key_missing service=%s target=%s",
            record.service_id,
            record.target_id,
        )
        return ServiceAccess(
            mode="unprotected",
            detail=f"Not protected: CLIO has no key for this server, so anyone who can reach "
            f"{where} may be able to use it. Reinstall it to protect it again.",
        )
    if not record.connection_url:
        return ServiceAccess(
            mode="api_key", detail=f"{protected} CLIO could not check it yet.", verified=False
        )
    url = _root(record.connection_url) + _GUARDED_PATH
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=http_transport) as client:
            without = await client.post(url, json={})
            with_key = await client.post(url, json={}, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning(
            "managed server access: reason=access_check_unreachable service=%s target=%s: %s",
            record.service_id,
            record.target_id,
            type(exc).__name__,
        )
        return ServiceAccess(
            mode="api_key", detail=f"{protected} CLIO could not check it yet.", verified=False
        )
    if without.status_code not in _REFUSED:
        logger.warning(
            "managed server access: reason=api_key_not_enforced service=%s target=%s status=%s",
            record.service_id,
            record.target_id,
            without.status_code,
        )
        return ServiceAccess(
            mode="unprotected",
            detail=(
                "Not protected: the server accepted a request without its key, so anyone who "
                f"can reach {where} can use it."
            ),
        )
    if with_key.status_code in _REFUSED:
        logger.warning(
            "managed server access: reason=api_key_rejected service=%s target=%s",
            record.service_id,
            record.target_id,
        )
        return ServiceAccess(
            mode="api_key",
            detail=(
                "Protected by a key, but the server refused the key CLIO has for it. "
                "Reinstall it to make a new key."
            ),
            verified=False,
        )
    return ServiceAccess(mode="api_key", detail=protected, verified=True)


def managed_credential_ref(store: InfrastructureStore, preset_id: str, address: str) -> str:
    """The key ref of the managed deployment at ``address`` reached as ``preset_id``, or ``""``.

    Used when a deployment is saved as a model server ("Use in Models"): the
    saved server then resolves that deployment's key. Only a keyed deployment
    of the same engine at the same address matches -- a key is never handed
    to another server.
    """

    wanted = _root(address)
    for record in store.services():
        if (
            record.service_id == preset_id
            and supports_api_key(record.service_id)
            and not is_shareable(record.configuration)
            and record.connection_url
            and _root(record.connection_url) == wanted
        ):
            return deployment_key_ref(record.target_id, record.service_id)
    return ""


class ServerAccessMixin:
    """Record who can use a managed model server once it runs (a mixin of the runtime)."""

    store: InfrastructureStore
    _http_transport: httpx.AsyncBaseTransport | None

    async def _record_access(self, target_id: str, service_id: str, port: int | None) -> None:
        record = self.store.service(target_id, service_id)
        if record is None or service_id not in MODEL_SERVERS or record.state != "running":
            return
        access = await check_access(
            record,
            port=port,
            target=self.store.target(target_id),
            http_transport=self._http_transport,
        )
        self.store.update_service(target_id, service_id, access=access)


__all__ = [
    "KEY_VARIABLES",
    "MODEL_SERVERS",
    "ServerAccessMixin",
    "launch_key",
    "load_key",
    "settle_failed_launch",
    "store_key",
    "SHAREABLE_FIELD",
    "check_access",
    "deployment_key_ref",
    "forget_key",
    "is_shareable",
    "managed_credential_ref",
    "request_headers",
    "supports_api_key",
]
