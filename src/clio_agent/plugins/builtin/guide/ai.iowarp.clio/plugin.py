"""The guide plugin: CLIO's own documentation for the installed version.

Every agent carries it (``Agent.plugins``); an agent delta can drop it. The pages
under ``skills/clio/docs/`` and the captures under ``assets/captures/`` are written by
``site/scripts/export-docs-markdown.mjs``; do not edit them by hand.
"""

from clio_agent.sdk import Plugin


class Guide(Plugin):
    id = "guide"
    version = "1.0.0"
