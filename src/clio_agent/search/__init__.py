"""CLIO's web and literature search: hosted sources behind ``web_*`` and ``literature_*``.

:mod:`.settings` reads ``search.*``; :mod:`.web` and :mod:`.fetch` answer
``web_search``/``web_fetch``; :mod:`.literature` answers ``literature_search`` and
``literature_lookup``; :mod:`.broker` asks the person the questions only they may
answer. The in-process MCP servers are in :mod:`clio_agent.tools.servers`.
"""
