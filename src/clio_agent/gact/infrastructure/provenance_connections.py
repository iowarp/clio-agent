"""Connection-only provenance setup, verified in an isolated process before activation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from clio_agent import conf
from clio_agent.gact.infrastructure.models import ExternalServiceConnection, utc_now
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)


class ProvenanceConnectionInput(BaseModel):
    """Non-secret connection metadata; settings and capture paths belong to this CLIO host."""

    model_config = ConfigDict(extra="forbid")
    service_id: Literal["flowcept", "cmf"]
    label: str = Field(min_length=1, max_length=160)
    url: HttpUrl
    settings_path: str = ""
    attention_files_dir: str = ""
    capture_attention: bool = False

    @model_validator(mode="after")
    def validate_connection(self) -> ProvenanceConnectionInput:
        """Reject credentials in URLs and settings the selected backend cannot consume."""
        if self.url.username or self.url.password or self.url.query or self.url.fragment:
            raise ValueError("Use a service URL without credentials, query or fragment")
        if self.service_id == "cmf" and (
            self.settings_path or self.capture_attention or self.attention_files_dir
        ):
            raise ValueError("CMF direct-server mode does not use Flowcept or attention settings")
        if self.service_id == "flowcept":
            if not Path(self.settings_path).is_absolute():
                raise ValueError("Select an absolute Flowcept settings path on the connected CLIO")
            if self.capture_attention and not Path(self.attention_files_dir).is_absolute():
                raise ValueError(
                    "Select an absolute local attention directory on the connected CLIO"
                )
        return self

    def record(self) -> ExternalServiceConnection:
        """Create an externally owned record without copying file contents or credentials."""
        return ExternalServiceConnection(
            service_id=self.service_id,
            label=self.label,
            url=str(self.url).rstrip("/"),
            configuration={
                "settings_path": self.settings_path,
                "attention_files_dir": self.attention_files_dir,
                "capture_attention": "true" if self.capture_attention else "false",
            },
        )


def connection_revision(row: ExternalServiceConnection) -> str:
    """Invalidate verification when connection settings or the private settings file change."""
    payload: dict[str, Any] = {
        "service_id": row.service_id,
        "url": row.url,
        "configuration": row.configuration,
    }
    if row.service_id == "flowcept":
        path = Path(row.configuration.get("settings_path", ""))
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > 1024**2:
            raise ValueError("Flowcept settings must be a readable local file under 1 MiB")
        payload["settings_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if row.configuration.get("capture_attention") == "true":
            captures = Path(row.configuration.get("attention_files_dir", ""))
            if (
                not captures.is_absolute()
                or not captures.is_dir()
                or not os.access(captures, os.R_OK)
            ):
                raise ValueError(
                    "Select an existing readable attention folder on the connected CLIO"
                )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def verify_connection(row: ExternalServiceConnection) -> ExternalServiceConnection:
    """Require fresh write/readback; never reconfigure Flowcept's process-global SDK in place."""
    revision = connection_revision(row)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "clio_agent.gact.infrastructure.provenance_probe"],
            input=row.model_dump_json(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=65,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Provenance verification timed out; no activation was saved") from exc
    # Provider tracebacks may contain private settings. Only the probe's allowlisted
    # result can leave the subprocess; neither stdout noise nor stderr is published.
    try:
        evidence = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "Provenance verification failed; check service health and local settings"
        ) from exc
    if (
        result.returncode
        or not isinstance(evidence, dict)
        or evidence.get("write_readback") is not True
    ):
        raise ValueError(
            "Provenance write/readback failed; check service health and local settings"
        )
    if connection_revision(row) != revision:
        raise ValueError("Settings changed during verification; verify again")
    allowed = {
        key: evidence[key]
        for key in ("probe_id", "write_readback", "input_output_lineage")
        if key in evidence
    }
    return row.model_copy(
        update={
            "reachable": True,
            "checked_at": utc_now(),
            "verification": {**allowed, "revision": revision, "verified_at": utc_now()},
        }
    )


def desired_configuration(row: ExternalServiceConnection) -> dict[str, Any]:
    """Return only the declared backend's configuration, retaining the other backend."""
    if row.service_id == "cmf":
        return {
            "provenance.artifacts.provider": "cmf",
            "provenance.artifacts.cmf.server_url": row.url,
            "provenance.artifacts.cmf.python": "",
            "provenance.artifacts.cmf.worker_url": "",
        }
    if row.service_id != "flowcept":
        raise ValueError("Only Flowcept and CMF support provenance activation")
    capture = row.configuration.get("capture_attention") == "true"
    resolver = conf.ConfigStore()
    return {
        "provenance.agentic.providers": list(
            dict.fromkeys(
                [
                    *conf.as_csv(
                        resolver.resolve(
                            "provenance.agentic.providers",
                            env="CLIO_PROVENANCE_PROVIDERS",
                            default=["jsonl"],
                        )
                    ),
                    "jsonl",
                    "flowcept",
                ]
            )
        ),
        "provenance.agentic.flowcept.settings_path": row.configuration["settings_path"],
        "provenance.agentic.flowcept.persistence_owner": "collector",
        "provenance.agentic.flowcept.privacy": "full" if capture else "metadata",
        "provenance.attention.enabled": capture,
        "provenance.attention.files_dir": row.configuration.get("attention_files_dir", ""),
    }


def _set(document: dict[str, Any], dotted: str, value: Any) -> None:
    node = document
    parts = dotted.split(".")
    for part in parts[:-1]:
        if part in node and not isinstance(node[part], dict):
            if part == "attention" and isinstance(node[part], bool):
                node[part] = {"enabled": node[part]}
            else:
                raise ValueError(f"Configuration section {part} must be a mapping")
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _save_configuration(changes: dict[str, Any]) -> None:
    with USER_CONFIG_LOCK:
        path = user_config_path()
        original = read_document(path)
        document = json.loads(json.dumps(original))
        for key, value in changes.items():
            _set(document, key, value)
        write_document(path, document)
        # Workspace overrides must not make a successful save silently ineffective.
        resolver = conf.ConfigStore()
        if any(resolver.file_value(key) != value for key, value in changes.items()):
            write_document(path, original)
            raise ValueError(
                "A workspace configuration overrides this connection; resolve it before activation"
            )


def activate_connection(row: ExternalServiceConnection) -> dict[str, Any]:
    """Save a verified choice for the next CLIO start without replacing live providers."""
    revision = connection_revision(row)
    if row.verification.get("revision") != revision or not row.verification.get("write_readback"):
        raise ValueError("Verify this connection and its current settings before using it")
    with USER_CONFIG_LOCK:
        _save_configuration(desired_configuration(row))
    return {"connection_id": row.id, "configuration_revision": revision, "restart_required": True}


def connection_selected(row: ExternalServiceConnection) -> bool:
    """Whether the next-start configuration selects this exact connection."""
    resolver = conf.ConfigStore()
    desired = desired_configuration(row)
    return all(
        (
            "flowcept" in conf.as_csv(resolver.file_value(key))
            if key == "provenance.agentic.providers"
            else resolver.file_value(key) == value
        )
        for key, value in desired.items()
    )


def disconnect_connection(row: ExternalServiceConnection) -> dict[str, Any]:
    """Stop using this backend on restart; retain the connection, service and evidence."""
    if not connection_selected(row):
        raise ValueError("This connection is not selected for provenance")
    resolver = conf.ConfigStore()
    if row.service_id == "cmf":
        changes: dict[str, Any] = {"provenance.artifacts.provider": "native"}
    else:
        providers = conf.as_csv(
            resolver.resolve(
                "provenance.agentic.providers", env="CLIO_PROVENANCE_PROVIDERS", default=["jsonl"]
            )
        )
        changes = {
            "provenance.agentic.providers": list(
                dict.fromkeys([name for name in providers if name != "flowcept"] + ["jsonl"])
            ),
            "provenance.attention.enabled": False,
        }
    _save_configuration(changes)
    return {"connection_id": row.id, "restart_required": True}
