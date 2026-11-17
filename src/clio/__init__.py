"""``clio`` — run CLIO agents and flows from a Python script.

Installed by ``pip install clio-agent`` (the PyPI name ``clio`` is an unrelated
package). This module re-exports :mod:`clio_agent.client`.

Example:
    >>> from clio import ClioClient, ClioOptions
    >>> with ClioClient(options=ClioOptions(workspace=".", agent="factorio")) as c:
    ...     result = c.run("Reduce the mass of the bracket in bracket.inp by 30%")
    ...     print(result.text)
"""

from clio_agent.client import *  # noqa: F403 - the public surface is clio_agent.client's
from clio_agent.client import __all__ as __all__  # noqa: PLC0414
