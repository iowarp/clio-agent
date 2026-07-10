"""Pack-declared workflow_state vocabulary (#646/#648, Phase C).

Core is a generic engine; the DOMAIN vocabulary (section names, status
precedence, readiness rules, artifact fields, scrub aliases, failure rules)
is declared once per Agent Blueprint in AGENT.md frontmatter under
``workflow_state:`` and compiled into this frozen, typed schema. Copies the
enrichment.py empty-hook pattern: GENERIC (below) is the domain-free default --
core reproduces EarthScope precedence for NO pack until the pack declares it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from clio_agent.gact.workflow_state.merge import _normalize_workflow_state_scalar


class WorkflowSectionReadiness(BaseModel):
    """The 'analysis-ready' acquisition rule block (merge.py:74-97/139-171 generalized)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str  # "analysis_ready"
    ready_status: str  # "staged"
    ready_rank: int  # 5
    requires_ondisk: bool = True  # the staged+ready on-disk demotion
    not_on_disk_rank: int = 1
    path_fields: tuple[str, ...]  # ("local_path", "path") -- `or`-chain order
    metadata_path_field: str = ""  # "metadata_path" ("" disables reuse check)
    demote_keep_statuses: tuple[str, ...]  # ("blocked", "missing", "metadata_only")
    demote_status_reused_metadata: str  # "metadata_only"
    demote_status_default: str  # "candidate_found"
    blocker_field: str = "blocker"
    blocker_reused_metadata: str
    blocker_default: str


class WorkflowSectionRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status_ranks: dict[str, int]  # matched status -> rank (higher wins)
    sticky_true_fields: tuple[str, ...] = ()  # current True beats incoming False
    readiness: WorkflowSectionReadiness | None = None


class WorkflowFailureRule(BaseModel):
    """One entry of the failed-child typed-state stamp (delegation.py:769-785 generalized)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    section: str
    when: Literal["readiness_not_true", "always"]  # both fire only if section present as dict
    set_status: str
    set_readiness_false: bool = False  # writes readiness.field = False
    set_fields: dict[str, str] = Field(default_factory=dict)  # field -> .format template
    # templates may use {child!r}, {error}, {message}


class WorkflowScrubAliases(BaseModel):
    """Accepted-but-INERT since #881.

    These aliases drove the public-prompt and visible-transcript prose scrubbers
    in :mod:`clio_agent.gact.delegation`, both DELETED in #881: the client renders
    model prose VERBATIM and the server fixes leaks at the root, so clio no longer
    edits a model's visible text by matching a declared vocabulary. The fields are
    RETAINED (not removed) purely for pack compatibility — external marketplace
    ``AGENT.md`` blueprints already declare ``workflow_state.aliases`` and
    :class:`WorkflowStateSchema` is ``extra="forbid"``, so dropping the field
    would reject those blueprints at load. Nothing in core reads these values.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sections: tuple[str, ...] = ()  # (inert since #881) former `state_path` members
    orphan_sections: tuple[str, ...] = ()  # (inert since #881) trailing-orphan subset
    fields: tuple[str, ...] = ()  # (inert since #881) former domain field names
    fence_labels: tuple[str, ...] = ()  # (inert since #881) former fence-intro labels


class WorkflowStateSchema(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sections: dict[str, WorkflowSectionRule] = Field(default_factory=dict)
    artifact_paths: tuple[tuple[str, str], ...] = ()  # (section, key)
    artifact_extensions: tuple[str, ...] = ()  # ("csv", "png") -- lowercase, no dot
    aliases: WorkflowScrubAliases = Field(default_factory=WorkflowScrubAliases)
    resume_example_fields: tuple[str, ...] = ()  # "acquisition.metadata_path", ...
    failure_rules: tuple[WorkflowFailureRule, ...] = ()

    # ---- engine ----------------------------------------------------------
    def rank(self, section: str, value: Mapping[str, Any]) -> int:
        """Byte-identical port of merge.py:74-136 driven by the declaration."""
        rule = self.sections.get(section)
        if rule is None:
            return 0  # undeclared section: presence-only (rank 0)
        status = str(value.get("status") or "").strip().lower()
        r = rule.readiness
        if r is not None and status == r.ready_status and value.get(r.field) is True:
            raw: Any = ""
            for name in r.path_fields:  # reproduce `value.get("local_path") or value.get("path")`
                candidate = value.get(name)
                if candidate:
                    raw = candidate
                    break
            local_path = str(raw or "").strip()
            if (
                r.requires_ondisk
                and local_path.startswith(("/", "~"))
                and not Path(local_path).expanduser().is_file()
            ):
                return r.not_on_disk_rank
            return r.ready_rank
        if status in rule.status_ranks:
            return rule.status_ranks[status]
        if status:
            return 1
        return 0

    def normalize_section(self, section: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """Byte-identical port of merge.py:139-171 driven by the declaration."""
        normalized = {str(k): _normalize_workflow_state_scalar(str(k), v) for k, v in value.items()}
        rule = self.sections.get(section)
        r = rule.readiness if rule is not None else None
        if r is None:
            return normalized
        status = str(normalized.get("status") or "").strip().lower()
        raw: Any = ""
        for name in r.path_fields:
            candidate = normalized.get(name)
            if candidate:
                raw = candidate
                break
        local_path = str(raw or "").strip()
        metadata_path = (
            str(normalized.get(r.metadata_path_field) or "").strip()
            if r.metadata_path_field
            else ""
        )
        reused_metadata_as_data = bool(local_path) and local_path == metadata_path
        if normalized.get(r.field) is True and (
            status != r.ready_status or not local_path or reused_metadata_as_data
        ):
            normalized[r.field] = False
            if status in set(r.demote_keep_statuses):
                normalized["status"] = status
            elif reused_metadata_as_data:
                normalized["status"] = r.demote_status_reused_metadata
            else:
                normalized["status"] = r.demote_status_default
            normalized.setdefault(
                r.blocker_field,
                r.blocker_reused_metadata if reused_metadata_as_data else r.blocker_default,
            )
        elif normalized.get(r.field) is True and status == r.ready_status and local_path:
            normalized.pop(r.blocker_field, None)
        return normalized

    def sticky_true_fields_for(self, section: str) -> tuple[str, ...]:
        rule = self.sections.get(section)
        return rule.sticky_true_fields if rule is not None else ()

    def apply_failure_rules(
        self, state: dict[str, Any], *, child_agent_id: str, error: str, message: str
    ) -> None:
        """Byte-identical port of delegation.py:769-785 driven by the declaration."""
        for rule in self.failure_rules:
            section = state.get(rule.section)
            if not isinstance(section, dict):
                continue
            if rule.when == "readiness_not_true":
                section_rule = self.sections.get(rule.section)
                readiness = section_rule.readiness if section_rule is not None else None
                if readiness is None or section.get(readiness.field) is True:
                    continue
                if rule.set_readiness_false:
                    section[readiness.field] = False
            section["status"] = rule.set_status
            for field_name, template in rule.set_fields.items():
                section[field_name] = template.format(
                    child=child_agent_id, error=error, message=message
                )


GENERIC_WORKFLOW_STATE_SCHEMA = WorkflowStateSchema()  # domain-free: everything ranks 0
