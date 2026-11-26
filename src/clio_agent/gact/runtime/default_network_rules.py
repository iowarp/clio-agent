"""Network allow rules CLIO ships, seeded once as ordinary permission policies.

Each default is DATA: a ``host:port`` domain rule for every workspace (``scope_id "*"``) with a
reason. Seeding appends it to ``permission_policies.json`` like a person's own answer, so it shows
in Settings > Permissions, where it can be edited or removed. The file records every seeded id
under ``seeded_default_rules`` (id -> the CLIO version that seeded it), so a removed or edited
default is never seeded again and a later release seeds only the ids it adds.

Rules matter where the chokepoint gates: a deny-mode workspace's connections and the write
gate's plain-HTTP writes. Anything they do not cover still asks. They share the bottom priority
band (``SHIPPED_RULE_PRIORITY``), so any rule the person adds or answers outranks them. ``protocol``
describes the traffic for the person; the chokepoint relays an opaque tunnel and does not enforce it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.grant_resolver import ANY_WORKSPACE, KIND_DOMAIN, SHIPPED_RULE_PRIORITY

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

SOURCE = "clio_default"
SEEDED_KEY = "seeded_default_rules"


@dataclass(frozen=True)
class DefaultNetworkRule:
    """One shipped allow rule: a stable id, its ``host:port``, the traffic and why it ships."""

    id: str
    host_pattern: str
    protocol: str
    reason: str


DEFAULT_NETWORK_RULES: tuple[DefaultNetworkRule, ...] = (
    DefaultNetworkRule(
        "github-ssh", "github.com:22", "ssh", "git and gh over SSH to GitHub repositories"
    ),
    DefaultNetworkRule(
        "github-ssh-443",
        "ssh.github.com:443",
        "ssh",
        "git over SSH to GitHub through port 443, where port 22 is blocked",
    ),
    DefaultNetworkRule(
        "github-https", "github.com:443", "https", "git and gh over HTTPS to GitHub repositories"
    ),
    DefaultNetworkRule(
        "github-api", "api.github.com:443", "https", "the GitHub API used by gh and CLIO storage"
    ),
    DefaultNetworkRule(
        "github-codeload",
        "codeload.github.com:443",
        "https",
        "GitHub archive downloads used to install plugins and skills",
    ),
    DefaultNetworkRule(
        "github-objects",
        "objects.githubusercontent.com:443",
        "https",
        "GitHub release assets and git LFS objects",
    ),
    DefaultNetworkRule(
        "pypi", "pypi.org:443", "https", "the Python package index used by uv and pip"
    ),
    DefaultNetworkRule(
        "pypi-files", "files.pythonhosted.org:443", "https", "Python package downloads from PyPI"
    ),
    DefaultNetworkRule(
        "npm-registry",
        "registry.npmjs.org:443",
        "https",
        "npm package installs for skills and tools",
    ),
)


def _policy(rule: DefaultNetworkRule, version: str) -> dict[str, Any]:
    return {
        "kind": KIND_DOMAIN,
        "scope": "workspace",
        "scope_id": ANY_WORKSPACE,
        "host_pattern": rule.host_pattern,
        "action": "allow",
        "priority": SHIPPED_RULE_PRIORITY,
        "metadata": {
            "source": SOURCE,
            "default_id": rule.id,
            "protocol": rule.protocol,
            "reason": rule.reason,
            "since": version,
        },
    }


def seed_default_network_rules(
    app: FastAPI, rules: tuple[DefaultNetworkRule, ...] = DEFAULT_NETWORK_RULES
) -> list[str]:
    """Append each shipped rule this policy file has never seeded; return the seeded ids.

    Runs at the real server's boot only, against the file the app persists to. A rule already
    recorded is skipped even when the person has removed or edited it. A file that cannot be
    read is left alone (no seed, logged): rewriting it could drop the person's own rules.
    """

    from clio_agent import __version__  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
        _flush_permission_policies,
    )

    path = getattr(app.state, "permission_policies_path", None)
    if path is None:
        return []
    seeded = read_seeded(Path(path))
    if seeded is None:
        logger.warning("reason=default_network_rules_unread path=%s", path)
        return []
    added: list[str] = []
    policies = app.state.permission_policies
    for rule in rules:
        if rule.id in seeded:
            continue
        policies.append(_policy(rule, __version__))
        seeded[rule.id] = __version__
        added.append(rule.id)
    if added:
        app.state.seeded_default_rules = seeded
        _flush_permission_policies(app)
        logger.info("reason=default_network_rules_seeded ids=%s", ",".join(added))
    return added


def read_seeded(path: Path) -> dict[str, str] | None:
    """Return the file's ``seeded_default_rules`` record, ``{}`` when absent, ``None`` if unreadable."""

    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    record = data.get(SEEDED_KEY) if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in record.items()} if isinstance(record, dict) else {}
