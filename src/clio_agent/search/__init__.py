"""CLIO's web search backend: the configured search service behind the ``web_search`` tool.

:mod:`clio_agent.search.settings` reads the ``search.*`` configuration (the backend
choice and the private SearXNG's engine policy); :mod:`clio_agent.search.backend`
is the one abstraction every caller routes a search through.
"""
