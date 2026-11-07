"""GitHub Copilot provider: the Copilot SDK driven as a model transport only.

Text tools stay in CLIO's gateway; every Copilot built-in tool, skill, MCP
server, memory and file hook is switched off (:mod:`.options`). Sign-in is
CLIO's own GitHub account (:mod:`.auth`), never the runtime's stored login.
"""
