"""Connectors: what a person enables in CLIO (``mcp-apps-surfaces-plan.md``, CN1 to CN12).

A connector is data: one entry with who it is, where its server is, what it needs
and the plugin that follows it. Entries live in ``catalogs/connectors/*.yaml``
(:mod:`.entries`); a person's choices (enabled, the values they gave) live in
:mod:`.state`, secrets in the credential store (:mod:`.secrets`). An enabled
connector is saved as the person's MCP connection of the same name
(:mod:`.materialize`); it reaches a conversation only when the person adds its
plugin there (:mod:`.follow`).

Nothing here names a service: adding a connector is adding an entry and a logo.
"""
