"""The core plugin: carried by every agent and never removable."""

from clio_agent.sdk import Plugin


class Core(Plugin):
    id = "core"
    version = "1.0.0"
