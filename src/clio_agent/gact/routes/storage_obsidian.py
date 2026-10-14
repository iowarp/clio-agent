"""Trusted Obsidian installation, sign-in and Sync selection, outside agent tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from pydantic import Field, SecretStr

from clio_agent.gact.storage.models import SourceRecord, StorageModel
from clio_agent.gact.storage.service import StorageService


class ObsidianSignIn(StorageModel):
    """Transient credentials consumed only by the official client."""

    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, exclude=True, repr=False)
    mfa: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)


class ObsidianSetup(StorageModel):
    """A selected remote identity with an optional vault encryption password."""

    vault_id: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)


Action = Callable[[Callable[[], Any]], Awaitable[Any]]


def register_storage_obsidian_routes(
    app: FastAPI, service: StorageService, run: Action, change_access: Action
) -> None:
    """Share the same instance ownership and policy boundary as other connected accounts."""
    account = service.obsidian_account

    @app.get("/v1/storage/obsidian")
    async def status() -> dict[str, Any]:
        return await run(account.status)

    @app.post("/v1/storage/obsidian/install")
    async def install() -> dict[str, Any]:
        def prepare() -> dict[str, Any]:
            for record in service.store.list("source", SourceRecord):
                if record.source.provider == "obsidian":
                    service.require_idle(record)
            service.obsidian_sync.shutdown()
            return account.install()

        return await change_access(prepare)

    @app.post("/v1/storage/obsidian/login")
    async def login(body: ObsidianSignIn) -> dict[str, Any]:
        def sign_in() -> dict[str, Any]:
            for record in service.store.list("source", SourceRecord):
                if record.source.provider == "obsidian":
                    service.require_idle(record)
            service.obsidian_sync.shutdown()
            return account.login(
                body.email, body.password.get_secret_value(), body.mfa.get_secret_value()
            )

        return await change_access(sign_in)

    @app.post("/v1/storage/obsidian/logout")
    async def logout() -> dict[str, Any]:
        def sign_out() -> dict[str, Any]:
            records = [
                row
                for row in service.store.list("source", SourceRecord)
                if row.source.provider == "obsidian"
            ]
            for record in records:
                service.require_idle(record)
            service.obsidian_sync.shutdown()
            return account.logout()

        return await change_access(sign_out)

    @app.get("/v1/storage/obsidian/vaults")
    async def vaults() -> dict[str, Any]:
        return {"vaults": await run(account.vaults)}

    @app.post("/v1/storage/obsidian/setup")
    async def setup(body: ObsidianSetup) -> dict[str, str]:
        return await change_access(
            lambda: account.setup(body.vault_id, body.password.get_secret_value())
        )
