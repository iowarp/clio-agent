"""Generic expert support package.

CLIO core no longer exports production native domain expert classes. Domain
behavior comes from marketplace plugins and the Python agent classes that select
them (``clio_agent.sdk``); reusable Python code in this package must remain
generic tool/runtime support.
"""

__all__: list[str] = []
