"""Hugging Face discovery with immutable revisions and observable download requirements."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
from clio_schemas.model_capabilities import HF_PIPELINE_TAGS
from pydantic import BaseModel, ConfigDict, Field, field_validator

from clio_agent.gact.infrastructure.operation_models import ReuseReport
from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.capabilities.facts_wire import model_facts
from clio_agent.providers.capabilities.hf_repo import (
    HfRepoCatalogSource,
    RepoResolution,
    fetch_model_card,
    model_card_failure,
    resolve_repo,
    resolve_repo_card,
)
from clio_agent.providers.capabilities.model_sources import resolve_model_capabilities
from clio_agent.providers.capabilities.tags import capability_tags


class ModelAcquisition(BaseModel):
    """Last observed target receipt, with the immutable location used to reconcile it."""

    id: str
    target_id: str
    storage_root: str
    repository: str
    requested_revision: str
    revision: str | None = None
    destination: str
    state: Literal[
        "queued", "running", "ready", "failed", "cancelled", "interrupted", "stale", "removed"
    ]
    phase: str
    bytes_done: int = 0
    bytes_total: int | None = None
    created_at: float
    updated_at: float
    observed_at: float = Field(default_factory=time.time)
    error: str | None = None
    # Typed cause: worker_exited (exit status known), worker_lost (killed/host restart),
    # receipt_missing (this host holds no receipt for the record).
    error_code: str | None = None
    exit_code: int | None = None
    log_tail: list[str] = Field(default_factory=list)
    log_path: str | None = None
    files: list[str] = Field(default_factory=list)
    file_path: str | None = None
    #: Set on the reply to a download whose verified revision was already there.
    reuse: ReuseReport | None = None
    #: ``hf_cache``: a snapshot found in a shared Hugging Face hub cache. CLIO holds no
    #: receipt for it and never hashed its files, so it cannot retry or cancel it.
    origin: Literal["receipt", "hf_cache"] = "receipt"


class ModelDownloadRequest(BaseModel):
    """A model acquisition request; serving it is a separate operation."""

    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=1, max_length=180)
    revision: str = Field(default="main", min_length=1, max_length=120)
    destination: str = Field(default="", max_length=4096)
    # Exact repository file names to fetch (e.g. one GGUF quantization);
    # empty fetches the whole revision.
    files: list[str] = Field(default_factory=list, max_length=64)
    #: Download again even when this verified revision is already present.
    from_scratch: bool = False

    @field_validator("files")
    @classmethod
    def file_names(cls, value: list[str]) -> list[str]:
        """Accept relative repository file names only, never patterns or escapes."""
        for name in value:
            if not re.fullmatch(r"[\w.+-]+(?:/[\w.+-]+)*", name) or ".." in name.split("/"):
                raise ValueError(f"Invalid model file name: {name!r}")
        return sorted(set(value))

    @field_validator("repository")
    @classmethod
    def repository_id(cls, value: str) -> str:
        """Only accept registry repository identities, never URLs or shell arguments."""
        if not re.fullmatch(r"[A-Za-z0-9][\w.-]*(?:/[A-Za-z0-9][\w.-]*)?", value):
            raise ValueError("Use a Hugging Face repository such as organization/model")
        return value

    @field_validator("revision")
    @classmethod
    def revision_name(cls, value: str) -> str:
        """Accept ordinary tags/branches or a resolved commit without control characters."""
        if not re.fullmatch(r"[\w./-]+", value) or ".." in value:
            raise ValueError("Invalid model revision")
        return value


async def search_models(
    query: str, *, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """Search the first bounded public Hub page, preserving unknown facts as unknown."""
    page = await search_models_page(query, client=client)
    return page["models"]


_SEARCH_LIMIT = 40
_SEARCH_EXPAND = [
    "safetensors",
    "config",
    "cardData",
    "pipeline_tag",
    "tags",
    "downloads",
    "sha",
    "createdAt",
    "library_name",
    "gated",
]
_HF_MODEL_API = "https://huggingface.co/api/models"


def _search_params(query: str, filters: list[str]) -> dict[str, str | int | list[str]]:
    """Build supported Hub-side filters from picker tokens; other tokens stay local."""
    params: dict[str, str | int | list[str]] = {
        "limit": _SEARCH_LIMIT,
        "sort": "downloads",
        "direction": -1,
        "expand[]": _SEARCH_EXPAND,
    }
    if query.strip():
        params["search"] = query[:180]
    task_ids = sorted(
        {
            token.removeprefix("task:")
            for token in filters
            if token.startswith("task:")
            and re.fullmatch(r"task:[a-z0-9][a-z0-9:-]{0,100}", token)
            and token.removeprefix("task:") in HF_PIPELINE_TAGS
        }
    )
    if len(task_ids) == 1:
        params["pipeline_tag"] = task_ids[0]
    providers = sorted(
        {
            token.removeprefix("provider:")
            for token in filters
            if token.startswith("provider:")
            and re.fullmatch(r"provider:[a-z0-9][a-z0-9.-]{0,100}", token)
        }
    )
    if len(providers) == 1:
        params["author"] = providers[0]
    modalities = {
        token.partition(":")[2]
        for token in filters
        if token
        in {
            "input:image",
            "input:audio",
            "input:video",
            "output:image",
            "output:audio",
            "output:video",
        }
    }
    # Hub's public tag filter is a broad server-side scope; the returned
    # capability evidence remains derived from structured metadata/files.
    if len(modalities) == 1:
        params["filter"] = next(iter(modalities))
    baseline_text = (bool(filters) and set(filters).issubset({"input:text", "output:text"})) or (
        not query.strip() and not filters
    )
    if "filter" not in params and baseline_text:
        params["filter"] = "text-generation"
    return params


def _decode_search_cursor(cursor: str, query: str, filters: list[str]) -> str:
    """Decode a continuation token bound to its original query and filters."""
    if len(cursor) > 8192:
        raise ValueError("Invalid Hugging Face search cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Hugging Face search cursor") from exc
    if (
        not isinstance(value, dict)
        or value.get("query") != query[:180]
        or value.get("filters") != sorted(set(filters))
        or not isinstance(value.get("cursor"), str)
        or not value["cursor"]
        or len(value["cursor"]) > 4096
    ):
        raise ValueError("Hugging Face search cursor does not match this query")
    return value["cursor"]


def _next_hub_cursor(
    response: httpx.Response, expected: dict[str, str | int | list[str]]
) -> str | None:
    """Accept only Hugging Face's same-origin Link continuation for this query."""
    for link in response.headers.get_list("link"):
        match = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", link, re.IGNORECASE)
        if not match:
            continue
        target = urlsplit(match.group(1))
        if (
            target.scheme != "https"
            or target.hostname != "huggingface.co"
            or target.port is not None
            or target.username is not None
            or target.password is not None
            or target.path != "/api/models"
            or target.fragment
        ):
            return None
        actual: dict[str, list[str]] = {}
        for key, value in parse_qsl(target.query, keep_blank_values=True):
            actual.setdefault(key.removesuffix("[]"), []).append(value)
        cursors = actual.pop("cursor", [])
        expected_values = {
            key.removesuffix("[]"): [str(item) for item in value]
            if isinstance(value, list)
            else [str(value)]
            for key, value in expected.items()
        }
        if actual != expected_values or len(cursors) != 1 or not cursors[0]:
            return None
        return cursors[0]
    return None


async def search_models_page(
    query: str,
    *,
    cursor: str | None = None,
    filters: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Return one bounded Hub page plus a safe, query-bound continuation token."""
    selected_filters = sorted(set(filters or []))
    if len(selected_filters) > 24 or any(
        len(token) > 180 or any(ord(char) < 32 for char in token) for token in selected_filters
    ):
        raise ValueError("Model search accepts up to 24 picker filters of at most 180 characters")
    if client is None:
        async with httpx.AsyncClient(timeout=20) as owned:
            return await search_models_page(
                query, cursor=cursor, filters=selected_filters, client=owned
            )
    params = _search_params(query, selected_filters)
    if cursor:
        params["cursor"] = _decode_search_cursor(cursor, query, selected_filters)
    response = await client.get(_HF_MODEL_API, params=params)
    response.raise_for_status()
    next_value = _next_hub_cursor(
        response, {key: value for key, value in params.items() if key != "cursor"}
    )
    next_cursor = None
    if next_value:
        body = json.dumps(
            {"query": query[:180], "filters": selected_filters, "cursor": next_value},
            separators=(",", ":"),
        ).encode()
        next_cursor = base64.urlsafe_b64encode(body).decode().rstrip("=")
    applied = [key for key in ("pipeline_tag", "author", "filter") if key in params]
    scope = f"Hugging Face public model results; {_SEARCH_LIMIT} rows per page"
    if applied:
        scope += "; Hub filters: " + ", ".join(f"{key}={params[key]}" for key in applied)
    applied_tokens: set[str] = set()
    if "pipeline_tag" in params:
        applied_tokens.add(f"task:{params['pipeline_tag']}")
    if "author" in params:
        applied_tokens.add(f"provider:{params['author']}")
    if "filter" in params:
        if params["filter"] == "text-generation":
            applied_tokens.update({"input:text", "output:text"})
        else:
            applied_tokens.update(
                token
                for token in selected_filters
                if token.partition(":")[2] == params["filter"]
                and token.partition(":")[0] in {"input", "output"}
            )
    local_filters = sorted(set(selected_filters) - applied_tokens)
    if local_filters:
        scope += "; picker filters applied locally: " + ", ".join(local_filters)
    rows = response.json()
    return {
        "models": [_search_row(row) for row in rows if isinstance(row, dict) and row.get("id")],
        "next_cursor": next_cursor,
        "scope": scope,
    }


def _search_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project already-returned Hub facts through the common resolver, without per-row fetches."""
    repository = row["id"]
    resolution = RepoResolution(
        repo=repository,
        sha=row.get("sha") or "",
        requested=repository,
        pipeline_tag=row.get("pipeline_tag") or "",
        metadata=row,
        architectures=tuple(
            str(name) for name in row["config"].get("architectures", []) if isinstance(name, str)
        )
        if isinstance(row.get("config"), dict)
        and isinstance(row["config"].get("architectures"), list)
        else (),
        siblings=frozenset(
            item["rfilename"]
            for item in row.get("siblings") or []
            if isinstance(item, dict) and isinstance(item.get("rfilename"), str)
        ),
    )
    model = HfRepoCatalogSource(allow_fetch=False).facts_from_resolution(
        repository, resolution, read_files=False
    )
    effective = combine_capabilities(model, None, None)
    return {
        "repository": row["id"],
        "revision": row.get("sha"),
        "task": row.get("pipeline_tag"),
        "gated": row.get("gated", False),
        "downloads": row.get("downloads"),
        "size_bytes": None,
        "hub_tags": [tag for tag in row.get("tags", []) if isinstance(tag, str)],
        "library": row.get("library_name"),
        "card_url": f"https://huggingface.co/{row['id']}",
        "capability_tags": capability_tags(effective, model_key=repository).model_dump(mode="json"),
        "model_facts": model_facts(effective, model_key=repository),
    }


def model_details(repository: str) -> dict[str, Any]:
    """Reuse the picker capability resolver and pinned Hub card, with no runtime claims."""
    ModelDownloadRequest(repository=repository)
    resolution = resolve_repo(repository)
    if resolution is None:
        raise ValueError("The model's registry metadata is unavailable")
    model = resolve_model_capabilities(repository, hf_repo=HfRepoCatalogSource())
    effective = combine_capabilities(model, None, None)
    thinking = model.thinking.value if model.thinking.known else None
    card_status = "unavailable"
    card_markdown = ""
    card_revision = ""
    card_chars = 0
    card_resolution = resolve_repo_card(repository)
    if card_resolution is not None:
        card_revision = card_resolution.sha
        if not card_resolution.lists("README.md"):
            card_status = "missing"
        else:
            card = fetch_model_card(card_resolution)
            if card is not None:
                card_markdown = card
                card_chars = len(card)
                card_status = "complete"
            else:
                failure = model_card_failure(card_resolution)
                card_status = failure
    card_url = (
        f"https://huggingface.co/{repository}/blob/{card_revision}/README.md"
        if card_revision
        else f"https://huggingface.co/{repository}"
    )
    return {
        "repository": repository,
        "metadata_repository": resolution.repo,
        "revision": resolution.sha,
        "capability_tags": capability_tags(effective, model_key=repository).model_dump(mode="json"),
        "model_facts": model_facts(effective, model_key=repository),
        "context_window": model.context_max.value if model.context_max.known else None,
        "reasoning": {"mechanism": thinking.mechanism, "levels": list(thinking.levels)}
        if thinking is not None
        else None,
        "native_tool_calling": model.tools.value if model.tools.known else None,
        "hub_tags": resolution.metadata.get("tags", []),
        "card_repository": repository,
        "card_revision": card_revision,
        "card_status": card_status,
        "card_chars": card_chars,
        "card_url": card_url,
        "card_markdown": card_markdown,
    }
