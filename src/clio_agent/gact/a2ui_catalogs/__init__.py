"""A2UI catalog registry, validation, and blueprint-declared catalog support.

Owner package for docs/design/a2ui-compat-campaign-2026-09.md slice S2: the
server no longer trusts one hard-coded catalog by string equality
(``CLIO_A2UI_CATALOG_ID``, deleted) — it is a registry consumer keyed
``(catalogId, protocolVersion)`` over the two builtin catalogs plus every
catalog an Agent Blueprint pack declares and ships.

See ``registry.py`` for :class:`CatalogEntry` / :class:`CatalogRegistry`,
``builtin.py`` for the two in-process catalogs, ``blueprint.py`` for pack
declaration/loading/validation, ``activation.py`` for session producibility,
``validation.py`` for schema + safety-walk validation, and ``reasons.py`` for
the typed degradation catalog.
"""

from __future__ import annotations

from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry, CatalogRegistry, CatalogResolver

__all__ = ["CatalogEntry", "CatalogRegistry", "CatalogResolver"]
