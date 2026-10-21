"""Plugin references and version ranges.

Agents and plugins name other plugins by id with an optional version range,
never by import: ``"manufacturing@^1"``, ``"pdf@>=1.2"``, ``"shell"``. The range
grammar is the small npm-style subset the design uses:

* ``^1.2`` -- same major, at least 1.2 (``^0.3`` stays within 0.3)
* ``~1.2`` -- same major and minor, at least 1.2
* ``>=1.2``, ``>1.2``, ``<=2``, ``<2``, ``=1.2.3`` and comma-joined combinations
* ``*`` or empty -- any version
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|[-.](?=[a-z0-9]))*$")


class VersionRangeError(ValueError):
    """A plugin reference or version range could not be parsed."""


def valid_plugin_id(value: str) -> bool:
    """Return whether ``value`` satisfies the Agent Plugins 1.0.0 name rules."""

    return 1 <= len(value) <= 64 and bool(_ID_RE.match(value))


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse a version into ``(major, minor, patch)``; missing parts are zero."""

    text = str(value or "").strip().lstrip("v")
    core = re.split(r"[-+]", text, maxsplit=1)[0]
    parts = core.split(".") if core else []
    if len(parts) > 3 or not all(part.isdigit() for part in parts):
        raise VersionRangeError(f"not a version: {value!r}")
    numbers = [int(part) for part in parts] + [0] * (3 - len(parts))
    return numbers[0], numbers[1], numbers[2]


@dataclass(frozen=True)
class PluginRef:
    """A reference to a plugin by id, with an optional version range and marketplace.

    ``<id>``, ``<id>@<range>`` or ``<marketplace>/<id>[@<range>]``: the marketplace
    qualifier picks one of several installed marketplaces that provide the id.
    """

    id: str
    range: str = ""
    marketplace: str = ""

    @classmethod
    def parse(cls, text: str) -> "PluginRef":
        raw = str(text or "").strip()
        reference, _, version_range = raw.partition("@")
        marketplace, _, plugin_id = reference.strip().rpartition("/")
        plugin_id, marketplace = plugin_id.strip(), marketplace.strip()
        if not valid_plugin_id(plugin_id) or (marketplace and not valid_plugin_id(marketplace)):
            raise VersionRangeError(f"invalid plugin id in reference {text!r}")
        version_range = version_range.strip()
        if version_range:
            _comparators(version_range)
        return cls(plugin_id, version_range, marketplace)

    def __str__(self) -> str:
        name = f"{self.marketplace}/{self.id}" if self.marketplace else self.id
        return f"{name}@{self.range}" if self.range else name

    def accepts(self, version: str) -> bool:
        """Return whether ``version`` satisfies this reference's range."""

        return satisfies(version, self.range)


def _comparators(version_range: str) -> list[tuple[str, tuple[int, int, int]]]:
    out: list[tuple[str, tuple[int, int, int]]] = []
    for clause in (part.strip() for part in version_range.split(",")):
        if not clause or clause == "*":
            continue
        match = re.fullmatch(r"(\^|~|>=|<=|>|<|=)?\s*(.+)", clause)
        if match is None:
            raise VersionRangeError(f"invalid version range {version_range!r}")
        operator, version_text = match.group(1) or "=", match.group(2)
        given = len(version_text.strip().lstrip("v").split("."))
        version = parse_version(version_text)
        major, minor, patch = version
        if operator == "^":
            out.append((">=", version))
            if major > 0 or given == 1:
                out.append(("<", (major + 1, 0, 0)))
            elif minor > 0 or given == 2:
                out.append(("<", (0, minor + 1, 0)))
            else:
                out.append(("<", (0, 0, patch + 1)))
        elif operator == "~":
            out.append((">=", version))
            out.append(("<", (major + 1, 0, 0) if given == 1 else (major, minor + 1, 0)))
        elif operator == "=" and given < 3:
            out.append((">=", version))
            out.append(("<", (major + 1, 0, 0) if given == 1 else (major, minor + 1, 0)))
        else:
            out.append((operator, version))
    return out


def satisfies(version: str, version_range: str) -> bool:
    """Return whether ``version`` satisfies ``version_range``."""

    comparators = _comparators(version_range) if version_range else []
    if not comparators:
        return True
    actual = parse_version(version)
    checks = {
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
        "=": lambda a, b: a == b,
    }
    return all(checks[operator](actual, bound) for operator, bound in comparators)
