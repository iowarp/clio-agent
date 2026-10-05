"""Read GitHub revision choices without creating or modifying a connected source."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

import requests

from .linked import github_location

if TYPE_CHECKING:
    from .service import StorageService


def github_revisions(
    service: StorageService, url: str, kind: Literal["branch", "tag", "commit"], page: int
) -> dict[str, Any]:
    """List a single cached page with the current user's reusable GitHub account."""
    org, repo, _, _ = github_location(url)
    account = service.auth.account_login(service.principal, service.store.clio_id, "github")
    token = service.auth.token(account) if service.auth.connected(account) else None
    resource = cast(
        Literal["branches", "tags", "commits"],
        {"branch": "branches", "tag": "tags", "commit": "commits"}[kind],
    )
    info: dict[str, Any] = {}
    try:
        info = service.github.metadata(org, repo, token=token)
        rows = service.github.metadata(org, repo, resource, page=page, token=token)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 401 and token:
            service.auth.reject_token(account, token)
            service.github.clear()
        if status in {401, 403, 404}:
            raise PermissionError(
                "Check the repository address and sign in with access to it. "
                "Private repositories must also be allowed in CLIO's GitHub app."
            ) from None
        if status == 409:
            rows = []  # GitHub returns 409 for the commit list of an empty repository.
        else:
            raise ValueError("GitHub could not load revisions. Try again shortly.") from None
    except requests.RequestException:
        raise ValueError(
            "GitHub could not be reached. Check your connection and try again."
        ) from None
    return {
        "repository": f"{org}/{repo}",
        "default_branch": info.get("default_branch", ""),
        "revisions": [
            {
                "value": row["sha"] if kind == "commit" else row["name"],
                "label": (row.get("commit", {}).get("message", "").split("\n")[0][:120])
                if kind == "commit"
                else row["name"],
                "sha": row["sha"] if kind == "commit" else row.get("commit", {}).get("sha", ""),
            }
            for row in rows
        ],
        "next_page": page + 1 if len(rows) == 100 else None,
    }
