"""Bounded Plan-mode review payload and CLIO-owned plan-directory preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.gact.runtime import grant_resolver


def ensure_owned_plan_directory(plan_file: str) -> None:
    """Create CLIO's plan directory without trusting arbitrary recorded paths."""

    owned_directory = grant_resolver.plans_dir().resolve(strict=False)
    if Path(plan_file).resolve(strict=False).parent == owned_directory:
        owned_directory.mkdir(parents=True, exist_ok=True)


def plan_review_content(plan_file: str) -> dict[str, Any]:
    """Read the saved plan into the durable approval record with an explicit bound."""

    limit = max(
        1,
        conf.resolve(
            "limits.plan_review_chars",
            env="CLIO_PLAN_REVIEW_CHARS",
            default=256_000,
            cast=conf.as_int,
        ),
    )
    try:
        content = Path(plan_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {
            "plan_content": "",
            "plan_content_status": "unavailable",
            "plan_content_error": type(exc).__name__,
        }
    if len(content) > limit:
        return {
            "plan_content": content[:limit],
            "plan_content_status": "truncated",
            "plan_content_chars": len(content),
            "plan_content_included_chars": limit,
        }
    return {"plan_content": content, "plan_content_status": "complete"}


__all__ = ["ensure_owned_plan_directory", "plan_review_content"]
