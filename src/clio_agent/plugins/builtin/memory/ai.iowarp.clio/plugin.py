"""The memory plugin: long-term memory and recall of past sessions.

Every agent carries it (``Agent.plugins``); an agent delta can drop it.
"""

from clio_agent.sdk import Plugin


class Memory(Plugin):
    id = "memory"
    version = "1.0.0"
