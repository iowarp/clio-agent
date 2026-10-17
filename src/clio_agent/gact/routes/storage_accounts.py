"""Provider definitions and account lifecycle shared by workspace source connections."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.routes.storage_inputs import CompleteSignIn, StartSignIn
from clio_agent.gact.storage.auth import application
from clio_agent.gact.storage.github_oauth import account_url
from clio_agent.gact.storage.models import SourceRecord
from clio_agent.gact.storage.ndp_oauth import configuration as ndp_configuration
from clio_agent.gact.storage.service import StorageService, provider_capabilities

Action = Callable[[Callable[[], Any]], Awaitable[Any]]


def register_storage_account_routes(
    app: FastAPI,
    service: StorageService,
    run: Action,
    change_access: Action,
    start_browser_auth: Callable[[Request, SourceRecord, StartSignIn | None], dict[str, Any]],
) -> None:
    """Register reusable provider logins independently of selected workspace data."""

    @app.get("/v1/storage/providers")
    async def providers() -> dict[str, Any]:
        """Return reusable provider definitions, including honest setup prerequisites."""
        definitions = []
        for identifier, label, logo in [
            ("local", "Files on this CLIO", "folder"),
            ("obsidian", "Obsidian", "obsidian"),
            ("sftp", "SSH / SFTP", "ssh"),
            ("google_drive", "Google Drive", "google-drive"),
            ("globus", "Globus", "globus"),
            ("github", "GitHub", "github"),
            ("ndp", "National Data Platform", "ndp"),
        ]:
            browser = identifier in {"google_drive", "globus", "github", "ndp"}
            config = application(identifier) if browser and identifier != "ndp" else None
            configured = (
                bool(ndp_configuration()[1])
                if identifier == "ndp"
                else not config or bool(config.client_id and config.redirect_uri)
            )
            definitions.append(
                {
                    "id": identifier,
                    **({"account_url": account_url()} if identifier == "github" else {}),
                    "name": label,
                    "account_name": service.auth.account_name(
                        service.principal, service.store.clio_id, identifier
                    ),
                    "logo": logo,
                    "authentication": "browser"
                    if browser
                    else "ssh_profile"
                    if identifier == "sftp"
                    else "none",
                    "configured": configured,
                    "authenticated": browser
                    and service.auth.account_connected(
                        service.principal, service.store.clio_id, identifier
                    ),
                    "capabilities": provider_capabilities(identifier).model_dump(),
                    "setup_requirement": None
                    if configured
                    else "Sign-in is not available in this build. No account setup is needed from you.",
                }
            )
        return {"clio_id": service.store.clio_id, "host_id": "local", "providers": definitions}

    @app.delete("/v1/storage/accounts/{provider}")
    async def sign_out_account(provider: str) -> dict[str, bool]:
        """Sign this user out of a provider on this CLIO, across workspaces."""
        if provider not in {"google_drive", "globus", "github", "ndp"}:
            raise HTTPException(404, "Connected account not found")

        def sign_out() -> None:
            for record in service.store.list("source", SourceRecord):
                if record.principal == service.principal and record.source.provider == provider:
                    service.require_idle(record)
            service.auth.sign_out(service.principal, service.store.clio_id, provider)
            if provider == "github":
                service.github.clear()

        await change_access(sign_out)
        return {"signed_out": True}

    @app.post("/v1/storage/accounts/{provider}/auth/start")
    async def start_account_auth(
        provider: str, request: Request, body: StartSignIn | None = None
    ) -> dict[str, Any]:
        """Start a reusable login independently of connecting a folder."""
        return await run(
            lambda: start_browser_auth(
                request,
                service.auth.account_login(service.principal, service.store.clio_id, provider),
                body,
            )
        )

    @app.post("/v1/storage/accounts/{provider}/auth/complete")
    async def complete_account_auth(provider: str, body: CompleteSignIn) -> dict[str, bool]:
        """Finish provider sign-in without creating a source or workspace attachment."""
        completed = await change_access(
            lambda: service.auth.complete(
                service.auth.account_login(service.principal, service.store.clio_id, provider),
                body.flow_id,
                body.callback_url,
            )
        )
        return {"authenticated": completed}
