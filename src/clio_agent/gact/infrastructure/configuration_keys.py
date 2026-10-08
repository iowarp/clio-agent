"""Reject service-action configuration keys CLIO would otherwise ignore.

A managed service reads only the keys it declares: its setup fields
(``ManagedServiceDefinition.configuration_fields``), its server parameters as
``param.<id>``, the ``storage.*`` locations, and the lifecycle control answers
below. Anything else used to be accepted and dropped, so a request carrying
``tool_call_parser`` instead of ``param.tool_call_parser`` launched a server
without the setting and without a warning (F006). Such a key is now a typed
validation error that names it and the accepted form.

Keys already in the installed record's configuration are accepted as-is: the UI
sends the installed configuration back with each action, and it carries values
CLIO derived itself (``native_owner``, ``compatibility_profile``, ...).
"""

from __future__ import annotations

from collections.abc import Mapping

from clio_agent.gact.infrastructure.drivers import service_definitions
from clio_agent.gact.infrastructure.models import ManagedServiceDefinition, TargetFacts
from clio_agent.gact.infrastructure.server_access import SHAREABLE_FIELD
from clio_agent.gact.infrastructure.server_parameters import PARAMETER_PREFIX

#: Location overrides every managed deployment reads (``storage.service_directory``, ...).
STORAGE_PREFIX = "storage."

#: Lifecycle answers and launch inputs that are not form fields of any one service.
CONTROL_KEYS = frozenset(
    {
        # The remote-CLIO version-conflict answer (#1528) and the process it names.
        "on_conflict",
        "conflict_pid",
        "conflict_root",
        # The Desktop instance that owns a remote CLIO launch.
        "desktop_id",
        "keep_running",
        # A model server deliberately run without its API key.
        SHAREABLE_FIELD,
        # The immutable revision of a CLIO-downloaded model being served.
        "model_revision",
        # The provenance workflow a native vLLM reports under.
        "workflow_id",
    }
)

#: Keys a service reads that its catalog form does not render.
SERVICE_KEYS: dict[str, frozenset[str]] = {"clio_agent": frozenset({"port"})}


class UnknownConfigurationKeyError(ValueError):
    """A configuration key the requested service would silently ignore."""

    code = "unknown_configuration_key"

    def __init__(self, service_id: str, key: str, accepted: list[str], hint: str) -> None:
        self.service_id = service_id
        self.key = key
        self.accepted = accepted
        super().__init__(f"{key!r} is not a configuration key of {service_id}; {hint}")


def _definition(service_id: str) -> ManagedServiceDefinition | None:
    # Field and parameter ids do not depend on host facts (only their
    # compatibility and options do), so a neutral host describes them.
    facts = TargetFacts(target_id="", label="", os="linux", arch="x86_64")
    return next((row for row in service_definitions(facts) if row.id == service_id), None)


def validate_configuration_keys(
    service_id: str,
    configuration: Mapping[str, str],
    installed: Mapping[str, str] | None = None,
) -> None:
    """Raise :class:`UnknownConfigurationKeyError` for the first key the service ignores.

    Args:
        service_id: The managed service the action targets.
        configuration: The request's configuration.
        installed: The installed record's configuration, echoed back by clients.

    An unknown ``service_id`` is left to the operation, which reports it.
    """

    definition = _definition(service_id)
    if definition is None:
        return
    fields = {row.id for row in definition.configuration_fields}
    fields |= SERVICE_KEYS.get(service_id, frozenset())
    parameters = {row.id for row in definition.parameters}
    accepted = sorted(fields) + [f"{STORAGE_PREFIX}<location>"]
    if parameters:
        accepted.append(f"{PARAMETER_PREFIX}<parameter>")
    for key in configuration:
        if key in fields or key in CONTROL_KEYS or key.startswith(STORAGE_PREFIX):
            continue
        if installed and key in installed:
            continue
        if key.startswith(PARAMETER_PREFIX):
            pid = key[len(PARAMETER_PREFIX) :]
            if pid in parameters:
                continue
            hint = (
                f"its server parameters are {', '.join(sorted(parameters))}"
                if parameters
                else "it has no server parameters"
            )
            raise UnknownConfigurationKeyError(service_id, key, accepted, hint)
        candidate = key.replace("-", "_").removeprefix("__")
        if candidate in parameters:
            hint = f"server parameters take the form '{PARAMETER_PREFIX}{candidate}'"
        else:
            hint = "accepted keys are " + ", ".join(accepted)
        raise UnknownConfigurationKeyError(service_id, key, accepted, hint)
