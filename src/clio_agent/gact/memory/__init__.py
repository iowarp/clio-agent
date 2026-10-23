"""Long-term memory: what the agent carries forward about a user, their projects and
how they work (``docs/design/sdk/long-term-memory-plan.md``).

Documents, versions, proposals and usage counters are record kinds on clio-core in
their own namespace (:mod:`.store`); every write goes through one owner,
:class:`~.service.MemoryService`.
"""
