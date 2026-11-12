"""Persist ``search.*`` changes to the user ``config.yaml`` (approvals, order, toggles).

The person's answers to search questions (approve a source, switch automatically)
and every Settings change land here, under the process-wide config lock shared
with the other owners of that file, then the config cache is reloaded so the next
search sees them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clio_agent import conf
from clio_agent.search.settings import SearchSettings, build_settings, load_search_settings
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part) for part in value] if isinstance(value, list) else []


def update_search_settings(change: Callable[[dict[str, Any]], None]) -> SearchSettings:
    """Apply ``change`` to the user file's ``search`` mapping and return the new settings.

    ``change`` edits the mapping in place. The result is validated before it is
    written, so an invalid change raises :class:`SearchConfigurationError` and the
    file stays as it was.
    """

    with USER_CONFIG_LOCK:
        path = user_config_path()
        document = read_document(path)
        section = document.get("search")
        section = dict(section) if isinstance(section, dict) else {}
        change(section)
        web = section.get("web") if isinstance(section.get("web"), dict) else {}
        literature = (
            section.get("literature") if isinstance(section.get("literature"), dict) else {}
        )
        service = section.get("service") if isinstance(section.get("service"), dict) else {}
        current = load_search_settings()
        build_settings(
            enabled=section.get("enabled", current.enabled),
            web_order=_as_list(web.get("order", list(current.web_order))),
            web_disabled=_as_list(web.get("disabled", sorted(current.web_disabled))),
            auto_switch=web.get("auto_switch", current.auto_switch),
            approved=_as_list(section.get("approved", sorted(current.approved))),
            literature_disabled=_as_list(
                literature.get("disabled", sorted(current.literature_disabled))
            ),
            service_url=str(service.get("url", current.service_url) or ""),
            contact_email=str(section.get("contact_email", current.contact_email) or ""),
        )
        document["search"] = section
        write_document(path, document)
        conf.reload()
    return load_search_settings()


def _web(section: dict[str, Any]) -> dict[str, Any]:
    web = section.get("web")
    if not isinstance(web, dict):
        web = {}
        section["web"] = web
    return web


def approve_source(source: str) -> SearchSettings:
    """Remember that the person approved sending queries to ``source``."""

    def change(section: dict[str, Any]) -> None:
        approved = _as_list(section.get("approved", sorted(load_search_settings().approved)))
        if source not in approved:
            approved.append(source)
        section["approved"] = approved

    return update_search_settings(change)


def enable_auto_switch() -> SearchSettings:
    """Switch to the next source without asking when an allowance runs out."""

    return update_search_settings(lambda section: _web(section).__setitem__("auto_switch", True))


__all__ = ["approve_source", "enable_auto_switch", "update_search_settings"]
