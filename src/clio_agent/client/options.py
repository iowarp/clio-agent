"""What a script sets once for its CLIO client: :class:`ClioOptions`.

Example:
    >>> from clio import ClioClient, ClioOptions
    >>> options = ClioOptions(workspace=".", agent="factorio")
    >>> with ClioClient(options=options) as c:
    ...     print(c.run("Summarize bracket.inp").text)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

#: The variable for a bearer token set by hand.
TOKEN_ENV = "CLIO_GACT_BEARER_TOKEN"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def default_url() -> str:
    """The configured local CLIO server (``gact.port``, ``CLIO_PORT``, else 17800)."""

    from clio_agent.server_port import configured_port  # noqa: PLC0415 - light, lazy

    return f"http://127.0.0.1:{configured_port()}"


@dataclass(frozen=True)
class ClioOptions:
    """Everything a script sets once; ``run``/``run_flow``/``session`` may override
    ``agent``, ``model``, ``max_turns`` and ``max_budget_usd``.

    A script run approves every permission and asks nothing (the server runs it
    under ``bypass``); a person's explicit deny rules still apply.

    Attributes:
        url: Where to look for a CLIO server. Default: the configured local port.
        token: Bearer token for that server. Default: the server's credential record
            for that port, then ``CLIO_GACT_BEARER_TOKEN``.
        launch: Start a local server when none answers (never for a remote ``url``).
        workspace: Folder the runs work in (on the server's machine for a remote
            ``url``). Default: the current folder.
        agent: Agent id such as ``"factorio"`` (or ``marketplace/id``). Default: the
            server's default agent.
        model: ``account/model`` id, as ``list_models`` gives it. Default: the
            session default.
        max_turns: Most model turns (model steps) a run may take, over its whole
            session tree. Unset: no limit.
        max_budget_usd: Most a run may spend, over its whole session tree.
        banner: Print the one startup banner (and each plugin trusted) to stderr.
    """

    url: str = ""
    token: str | None = None
    launch: bool = True
    workspace: str = "."
    agent: str | None = None
    model: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    banner: bool = True

    def __post_init__(self) -> None:
        if self.max_turns is not None and self.max_turns < 1:
            raise ValueError(f"max_turns must be at least 1; got {self.max_turns}")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise ValueError(f"max_budget_usd must be positive; got {self.max_budget_usd}")
        if self.model is not None and "/" not in self.model:
            raise ValueError(
                f"model must be an account/model id as list_models gives it; got {self.model!r}"
            )

    @property
    def base_url(self) -> str:
        """The server origin, without a trailing slash."""

        return (self.url or default_url()).rstrip("/")

    @property
    def is_local(self) -> bool:
        """Whether :attr:`base_url` is on this machine (the only place a server starts)."""

        return (urlsplit(self.base_url).hostname or "") in _LOCAL_HOSTS

    @property
    def port(self) -> int:
        """The server's port."""

        parts = urlsplit(self.base_url)
        return parts.port or (443 if parts.scheme == "https" else 80)

    def with_overrides(self, **overrides: object) -> ClioOptions:
        """A copy with the per-call overrides that are not ``None`` applied."""

        return replace(self, **{k: v for k, v in overrides.items() if v is not None})

    def resolved_token(self) -> str | None:
        """The bearer to present: explicit, else the port's credential record (local
        servers only), else ``CLIO_GACT_BEARER_TOKEN``."""

        if self.token:
            return self.token
        if self.is_local:
            recorded = _recorded_token(self.port)
            if recorded:
                return recorded
        return os.environ.get(TOKEN_ENV) or None

    def workspace_path(self) -> str:
        """The workspace folder as the server should see it (absolute when local)."""

        if not self.is_local:
            return self.workspace
        return os.path.abspath(os.path.expanduser(self.workspace or "."))


def _recorded_token(port: int) -> str | None:
    """The bearer a local server on ``port`` published in its owner-only record."""

    import json  # noqa: PLC0415

    from clio_agent.gact.server_credentials import credential_record_path  # noqa: PLC0415

    try:
        record = json.loads(credential_record_path(port).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = record.get("bearer_token") if isinstance(record, dict) else None
    return token if isinstance(token, str) and token else None


__all__ = ["TOKEN_ENV", "ClioOptions", "default_url"]
