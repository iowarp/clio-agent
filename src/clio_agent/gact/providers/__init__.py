"""GACT server-side provider / LM-bind sub-package (#714 decomposition).

This package holds the *provider-bind concern* carved out of the
``clio_agent.gact.app`` monolith: the read-only helpers that compute the
effective LM configuration and translate catalog preset ids to runtime provider
kinds, the LM Studio native-REST helpers, and the runtime credential helpers
(API-key placeholder detection + ALCF/Argonne short-lived-token refresh).

It is DISTINCT from the top-level :mod:`clio_agent.providers` package, which
holds the real provider *implementations* (registry, presets, Argonne auth).
Modules here are the gact-server-side *wiring* around those implementations:

* :mod:`clio_agent.gact.providers.config` -- read-only effective-LM-config and
  catalog-id -> runtime-provider-kind resolution.
* :mod:`clio_agent.gact.providers.lmstudio` -- LM Studio native-REST root/header
  helpers and CLIO-owned-instance release.
* :mod:`clio_agent.gact.providers.auth` -- runtime credential helpers
  (placeholder-key detection, ALCF token resolve/refresh).

Modules here import the real provider implementations from
:mod:`clio_agent.providers` (lazily, inside functions) plus stdlib, and read
``app.state`` through the ``app`` argument -- never ``gact.app`` at module load
-- so the dependency graph stays acyclic. The heavier ``PUT /v1/providers/lm``
bind path (``_apply_lm_provider`` and its sibling closures) lives *inside* the
provider route handler in ``gact.app`` and moves with the route extraction
(decomposition step 7), not here.
"""

from __future__ import annotations
