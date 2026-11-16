"""The builder plugin: /capture and /optimize turn sessions into the person's plugins.

Every agent carries it beside ``guide``, which it requires; an agent delta can drop it.
"""

from clio_agent.sdk import Plugin


class Builder(Plugin):
    id = "builder"
    version = "1.0.0"
    requires = ["guide"]
