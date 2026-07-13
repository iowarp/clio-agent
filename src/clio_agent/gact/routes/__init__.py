"""Router-factory package for the GACT server (#714).

The gact decomposition carves the 133 ``@app.<verb>`` handlers out of the
``build_app`` closure in :mod:`clio_agent.gact.app` and regroups them, one
cohesive module per concern, behind ``register_<concern>_routes(app, deps)``
factories.

Each factory still defines its handlers *inside* the function so they
legitimately close over the ``app`` argument FastAPI's decorators require, but
reaches every cross-concern seam (shared ``build_app``-local helpers/closures)
through the explicit :class:`~clio_agent.gact.routes.deps.GactDeps` dataclass
instead of closing over ``build_app`` locals. ``app.state`` access is unchanged.
"""

from __future__ import annotations
