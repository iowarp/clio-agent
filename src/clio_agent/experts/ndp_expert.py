"""Nested National Data Platform catalog expert."""

from __future__ import annotations

from typing import Any

import dspy

from clio_agent.experts.native_tools import NativeToolRunner
from clio_agent.harness import ExpertRequest, ExpertResult, format_tool_error, validate_tool_result
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.gateway import gateway

NDP_ORGANIZATION_FIELDS: dict[str, type | tuple[type, ...]] = {
    "organizations": list,
    "count": int,
    "server": str,
}

NDP_DATASET_FIELDS: dict[str, type | tuple[type, ...]] = {
    "datasets": list,
    "count": int,
    "server": str,
}

_NDP_INTENT_TERMS = (
    "catalog",
    "ckan",
    "dataset discovery",
    "discover datasets",
    "find datasets",
    "list organizations",
    "national data platform",
    "ndp",
    "search datasets",
)

_NDP_SEARCH_TERMS = (
    "carbon",
    "climate",
    "earth observation",
    "fire",
    "forest",
    "hurricane",
    "netcdf",
    "ocean",
    "precipitation",
    "seismic",
    "seismological",
    "temperature",
    "weather",
    "wildfire",
)


class NDPExpert(dspy.Module):
    """Nested data-access expert for NDP discovery and bounded staging."""

    def __init__(self, tool_executor: ToolExecutor | None = None) -> None:
        """Initialize the nested NDP expert with NDP-scoped tools."""
        super().__init__()
        self._owns_executor = tool_executor is None
        self._tool_executor = tool_executor or create_sync_tool_executor(gateway)
        self._tools = [
            tool for tool in self._tool_executor.to_dspy_tools() if tool.name.startswith("ndp_")
        ]

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Run NDP discovery and return a DSPy-compatible prediction."""
        result = self.run(ExpertRequest(question=question, file_context=file_context))
        return self._to_prediction(result)

    def run(self, request: ExpertRequest) -> ExpertResult:
        """Discover external datasets through clio-kit-backed NDP tools."""
        runner = NativeToolRunner(self._tool_executor)
        q_lower = request.question.lower()
        org_filter = self._organization_filter(request.question)
        search_terms = self._search_terms(request.question)
        resource_format = self._resource_format(request.question)

        organizations: dict[str, Any] | None = None
        if org_filter or "organization" in q_lower or "noaa" in q_lower:
            organizations = runner.call(
                "ndp_list_organizations",
                {"name_filter": org_filter, "server": "global"},
            )
            organizations_valid = validate_tool_result(
                "ndp_list_organizations",
                organizations,
                NDP_ORGANIZATION_FIELDS,
            )
            if not organizations_valid.ok:
                assert organizations_valid.error is not None
                runner.mark_validation_error("ndp_list_organizations", organizations_valid.error)
                return self._failure_result("list organizations", organizations_valid.error, runner)
            organizations = organizations_valid.data or {}

        should_search = any(
            term in q_lower for term in ("dataset", "discover", "find", "search", "data product")
        )
        datasets: dict[str, Any] | None = None
        if should_search:
            datasets_result = self._search_datasets(
                runner,
                search_terms=search_terms,
                resource_format=resource_format,
            )
            if isinstance(datasets_result.get("error"), dict):
                return self._failure_result(
                    "search datasets",
                    datasets_result["error"],
                    runner,
                )
            datasets = datasets_result

        if organizations is None and datasets is None:
            organizations = runner.call(
                "ndp_list_organizations",
                {"name_filter": org_filter, "server": "global"},
            )
            organizations_valid = validate_tool_result(
                "ndp_list_organizations",
                organizations,
                NDP_ORGANIZATION_FIELDS,
            )
            if not organizations_valid.ok:
                assert organizations_valid.error is not None
                runner.mark_validation_error("ndp_list_organizations", organizations_valid.error)
                return self._failure_result("list organizations", organizations_valid.error, runner)
            organizations = organizations_valid.data or {}

        analysis_lines = ["Queried the National Data Platform catalog through clio-kit MCP."]
        if organizations is not None:
            org_rows = [str(row) for row in organizations.get("organizations", [])[:8]]
            analysis_lines.append(
                f"Organizations matched: {organizations.get('count', 0)}"
                + (("\n- " + "\n- ".join(org_rows)) if org_rows else "")
            )
        staging_metadata: dict[str, Any] | None = None
        if datasets is not None:
            dataset_rows = [
                row for row in datasets.get("datasets", [])[:5] if isinstance(row, dict)
            ]
            dataset_lines = [self._dataset_summary_line(row) for row in dataset_rows]
            analysis_lines.append(
                f"Datasets matched: {datasets.get('count', 0)}"
                + (("\n- " + "\n- ".join(dataset_lines)) if dataset_lines else "")
            )
            contextual = self._contextual_analysis(request.question, dataset_rows)
            if contextual:
                analysis_lines.append(contextual)
            staging_note, staging_metadata = self._staging_attempt(
                request.question,
                dataset_rows,
                runner,
            )
            if staging_note:
                analysis_lines.append(staging_note)
        recommendations = self._recommendations(
            request.question,
            datasets.get("datasets", []) if datasets else [],
        )
        metadata: dict[str, Any] = {
            "expert": "ndp_catalog",
            "parent_expert": "data",
            "format": "ndp",
            "source": "clio-kit",
        }
        if staging_metadata:
            metadata["staging"] = staging_metadata
        return ExpertResult(
            analysis="\n\n".join(analysis_lines),
            recommendations=recommendations,
            source="deterministic",
            tools=runner.observations,
            metadata=metadata,
        )

    def _search_datasets(
        self,
        runner: NativeToolRunner,
        *,
        search_terms: list[str],
        resource_format: str | None,
    ) -> dict[str, Any]:
        """Search NDP with independent terms, then merge/dedupe dataset rows."""
        query_sets = [[term] for term in search_terms] or [[]]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        first_error: dict[str, Any] | None = None
        valid_calls = 0

        for terms in query_sets:
            params: dict[str, Any] = {"server": "global", "limit": 5}
            if terms:
                params["search_terms"] = terms
            if resource_format:
                params["resource_format"] = resource_format

            result = runner.call("ndp_search_datasets", params)
            datasets_valid = validate_tool_result(
                "ndp_search_datasets",
                result,
                NDP_DATASET_FIELDS,
            )
            if not datasets_valid.ok:
                assert datasets_valid.error is not None
                runner.mark_validation_error("ndp_search_datasets", datasets_valid.error)
                if first_error is None:
                    first_error = datasets_valid.error
                continue

            valid_calls += 1
            data = datasets_valid.data or {}
            for row in data.get("datasets", []):
                if not isinstance(row, dict):
                    continue
                key = str(row.get("id") or row.get("name") or row.get("title") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                rows.append(row)

        if valid_calls == 0 and first_error is not None:
            return {"error": first_error}

        rows = sorted(rows, key=NDPExpert._dataset_priority, reverse=True)
        return {"datasets": rows[:5], "count": len(rows), "server": "global"}

    @staticmethod
    def _failure_result(
        action: str,
        error: dict[str, Any],
        runner: NativeToolRunner,
    ) -> ExpertResult:
        return ExpertResult(
            analysis=f"Could not {action} in NDP: {format_tool_error(error)}",
            recommendations="Verify clio-kit is installed and the NDP endpoint is reachable.",
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "ndp_catalog",
                "parent_expert": "data",
                "format": "ndp",
                "source": "clio-kit",
            },
        )

    @staticmethod
    def wants_request(question: str) -> bool:
        """Return whether the request asks for NDP/catalog discovery."""
        q_lower = question.lower()
        return any(term in q_lower for term in _NDP_INTENT_TERMS)

    @staticmethod
    def _organization_filter(question: str) -> str | None:
        """Extract an obvious organization filter from natural catalog requests."""
        q_lower = question.lower()
        if "noaa" in q_lower:
            return "noaa"
        if "nasa" in q_lower:
            return "nasa"
        if "doe" in q_lower:
            return "doe"
        if "seismic" in q_lower or "seismological" in q_lower:
            return "seism"
        return None

    @staticmethod
    def _search_terms(question: str) -> list[str]:
        """Extract conservative search terms for NDP dataset discovery."""
        q_lower = question.lower()
        terms = [term for term in _NDP_SEARCH_TERMS if term in q_lower]
        if any(term in q_lower for term in ("seismic", "seismological", "three axes")):
            terms.append("waveform")
        return list(dict.fromkeys(terms))

    @staticmethod
    def _resource_format(question: str) -> str | None:
        """Extract common resource-format filters from a catalog request."""
        q_lower = question.lower()
        for fmt in ("csv", "json", "netcdf", "zarr", "hdf5", "parquet"):
            if fmt in q_lower:
                return fmt.upper()
        return None

    @staticmethod
    def _dataset_summary_line(row: dict[str, Any]) -> str:
        """Format one NDP dataset row for a compact expert answer."""
        title = str(row.get("title") or row.get("name") or row.get("id") or "<untitled>")
        owner = str(row.get("owner_org") or "unknown owner")
        resources = row.get("resources") or []
        formats = sorted(
            {
                str(resource.get("format")).upper()
                for resource in resources
                if isinstance(resource, dict) and resource.get("format")
            }
        )
        formats.extend(str(fmt).upper() for fmt in row.get("resource_formats", []) if fmt)
        formats = sorted(set(formats))
        resource_names = [str(name) for name in row.get("resource_names", []) if name]
        format_text = ", ".join(formats[:5]) if formats else "formats not listed"
        resource_text = f"; resources: {', '.join(resource_names[:2])}" if resource_names else ""
        return f"{title} ({owner}; {format_text}{resource_text})"

    @staticmethod
    def _contextual_analysis(question: str, rows: list[dict[str, Any]]) -> str:
        """Return domain-specific discovery notes grounded in catalog rows."""
        q_lower = question.lower()
        if not any(term in q_lower for term in ("seismic", "seismological", "three axes")):
            return ""

        candidate = NDPExpert._select_seismic_dataset(rows)
        if candidate is None:
            return (
                "Seismic workflow note: no catalog row clearly exposes waveform data. "
                "Do not route to analysis or visualization until a downloadable waveform "
                "resource has been staged."
            )

        title = str(candidate.get("title") or candidate.get("name") or candidate.get("id"))
        notes = str(candidate.get("notes") or "")
        resource_names = ", ".join(str(name) for name in candidate.get("resource_names", [])[:3])
        text = (notes + resource_names).lower()
        if "miniseed" in text:
            format_hint = "MiniSEED waveform data"
        elif "sac" in text:
            format_hint = "SAC waveform data"
        else:
            format_hint = "seismic waveform data"
        return (
            "Seismic workflow note: the best discovery-stage candidate is "
            f"{title!r}. Its catalog text/resource names indicate {format_hint}"
            + (f" ({resource_names})." if resource_names else ".")
            + " CLIO has not downloaded or opened that resource yet, so analysis and "
            "three-axis plotting remain blocked on staging the waveform file."
        )

    @staticmethod
    def _staging_attempt(
        question: str,
        rows: list[dict[str, Any]],
        runner: NativeToolRunner,
    ) -> tuple[str, dict[str, Any] | None]:
        """Attempt data-stage resource staging when the prompt asks beyond discovery."""
        q_lower = question.lower()
        if not any(
            term in q_lower
            for term in (
                "analyze",
                "download",
                "inspect the data",
                "open the data",
                "plot",
                "stage",
                "three-axis",
                "three axes",
            )
        ):
            return "", None

        candidates = NDPExpert._staging_candidates(
            rows,
            waveform_only=NDPExpert._requires_waveform_resource(question),
        )
        if not candidates:
            return (
                "Staging note: no dataset candidate was available, so CLIO did not "
                "attempt resource staging."
            ), {
                "status": "blocked",
                "reason": "no_candidate",
                "attempts": [],
                "recommended_parent_actions": [
                    "broaden_catalog_search",
                    "ask_user_for_dataset_hint",
                    "try_another_provider",
                ],
            }

        failed_attempts: list[str] = []
        structured_attempts: list[dict[str, Any]] = []
        for candidate in candidates:
            identifier = str(candidate.get("id") or candidate.get("name") or "").strip()
            title = str(candidate.get("title") or candidate.get("name") or identifier)
            if not identifier:
                failed_attempts.append("candidate without dataset id/name")
                structured_attempts.append(
                    {
                        "status": "failed",
                        "reason": "missing_dataset_identifier",
                        "title": title,
                    }
                )
                continue

            identifier_type = "id" if candidate.get("id") else "name"
            details = runner.call(
                "ndp_get_dataset_details",
                {
                    "dataset_identifier": identifier,
                    "identifier_type": identifier_type,
                    "server": "global",
                },
            )
            if isinstance(details, dict) and details.get("error"):
                failed_attempts.append(
                    f"{title}: detail lookup failed: {format_tool_error(details['error'])}"
                )
                structured_attempts.append(
                    {
                        "status": "failed",
                        "stage": "details",
                        "dataset_identifier": identifier,
                        "identifier_type": identifier_type,
                        "title": title,
                        "error": details["error"],
                    }
                )
                runner.mark_error_handled(
                    "ndp_get_dataset_details",
                    reason="NDP staging recovery tried the next candidate.",
                )
                continue

            for resource_index in range(NDPExpert._resource_attempt_count(candidate, details)):
                staged = runner.call(
                    "ndp_stage_resource",
                    {
                        "dataset_identifier": identifier,
                        "identifier_type": identifier_type,
                        "resource_index": resource_index,
                        "server": "global",
                    },
                )
                if isinstance(staged, dict) and staged.get("staged"):
                    staged_path = str(staged.get("path") or "")
                    structured_attempts.append(
                        {
                            "status": "staged",
                            "stage": "stage_resource",
                            "dataset_identifier": identifier,
                            "identifier_type": identifier_type,
                            "resource_index": resource_index,
                            "title": title,
                            "path": staged_path,
                            "url": staged.get("url"),
                            "size_bytes": staged.get("size_bytes"),
                        }
                    )
                    inspection_note = ""
                    if staged_path.lower().endswith((".sac", ".tar", ".tgz", ".gz")):
                        inspected = runner.call(
                            "sac_inspect_archive",
                            {"filepath": staged_path, "max_members": 8},
                        )
                        if isinstance(inspected, dict) and not inspected.get("error"):
                            inspection_note = (
                                f" Data-stage inspection found {inspected.get('sac_trace_count')} "
                                "SAC traces in the staged file."
                            )
                        elif isinstance(inspected, dict) and inspected.get("error"):
                            inspection_note = (
                                " Data-stage seismic inspection failed visibly: "
                                f"{format_tool_error(inspected['error'])}"
                            )
                            runner.mark_error_handled(
                                "sac_inspect_archive",
                                reason=(
                                    "NDP staged the resource and surfaced the inspection "
                                    "failure in the staging note."
                                ),
                            )
                    attempted = (
                        f" after {len(failed_attempts)} failed attempt(s)"
                        if failed_attempts
                        else ""
                    )
                    return (
                        "Staging note: CLIO staged the selected NDP resource at "
                        f"{staged_path}{attempted}. Analysis and visualization can now use that "
                        f"local file if the format is supported.{inspection_note}"
                    ), {
                        "status": "staged",
                        "path": staged_path,
                        "dataset_identifier": identifier,
                        "identifier_type": identifier_type,
                        "resource_index": resource_index,
                        "attempts": structured_attempts,
                    }
                if isinstance(staged, dict) and staged.get("error"):
                    code = (
                        staged["error"].get("code")
                        if isinstance(staged["error"], dict)
                        else "tool_error"
                    )
                    failed_attempts.append(f"{title} resource {resource_index}: {code}")
                    structured_attempts.append(
                        {
                            "status": "failed",
                            "stage": "stage_resource",
                            "dataset_identifier": identifier,
                            "identifier_type": identifier_type,
                            "resource_index": resource_index,
                            "title": title,
                            "error": staged["error"],
                        }
                    )
                    runner.mark_error_handled(
                        "ndp_stage_resource",
                        reason=(
                            "NDP catalog child captured the failure for the parent "
                            "to decide recovery."
                        ),
                    )
                    continue
                failed_attempts.append(f"{title} resource {resource_index}: unexpected result")
                structured_attempts.append(
                    {
                        "status": "failed",
                        "stage": "stage_resource",
                        "dataset_identifier": identifier,
                        "identifier_type": identifier_type,
                        "resource_index": resource_index,
                        "title": title,
                        "reason": "unexpected_result_shape",
                        "result": staged,
                    }
                )

        if failed_attempts:
            return (
                "Staging note: CLIO attempted bounded NDP staging across candidate "
                "resources, but none could be staged by the NDP Catalog Expert. "
                "Attempts: "
                + "; ".join(failed_attempts[:6])
                + " Parent recovery should decide whether to broaden the search, use "
                "another provider or utility download path, ask the user, or stop "
                "without inventing downstream analysis."
            ), {
                "status": "blocked",
                "reason": "staging_failed",
                "attempts": structured_attempts,
                "recommended_parent_actions": [
                    "broaden_catalog_search",
                    "try_another_provider",
                    "delegate_to_utility_download",
                    "ask_user_for_dataset_hint",
                    "stop_without_downstream_analysis",
                ],
            }
        return (
            "Staging note: CLIO attempted resource staging but received an unexpected "
            "result shape, so downstream analysis remains blocked."
        ), {
            "status": "blocked",
            "reason": "unexpected_result_shape",
            "attempts": structured_attempts,
            "recommended_parent_actions": ["inspect_tool_result", "retry_or_stop"],
        }

    @staticmethod
    def _staging_candidates(
        rows: list[dict[str, Any]],
        *,
        waveform_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return bounded NDP dataset candidates in recovery order."""

        unique: dict[str, dict[str, Any]] = {}
        for row in sorted(rows, key=NDPExpert._dataset_priority, reverse=True):
            if waveform_only and not NDPExpert._row_is_waveform_candidate(row):
                continue
            key = str(row.get("id") or row.get("name") or len(unique))
            unique.setdefault(key, row)
        return list(unique.values())[:5]

    @staticmethod
    def _requires_waveform_resource(question: str) -> bool:
        """Return whether staging must stay scoped to waveform-compatible rows."""

        q_lower = question.lower()
        return any(
            term in q_lower
            for term in (
                "sac",
                "miniseed",
                "waveform",
                "trace statistics",
                "representative trace",
                "three-axis",
                "three axes",
            )
        )

    @staticmethod
    def _row_is_waveform_candidate(row: dict[str, Any]) -> bool:
        """Return whether an NDP row advertises waveform-compatible content."""

        haystack = " ".join(
            str(value)
            for value in (
                row.get("title"),
                row.get("name"),
                row.get("notes"),
                " ".join(str(name) for name in row.get("resource_names", [])),
                " ".join(str(fmt) for fmt in row.get("resource_formats", [])),
            )
            if value
        ).lower()
        if any(term in haystack for term in ("waveform", "miniseed", "mseed", ".sac", " sac ")):
            return True
        return any(str(url).lower().endswith((".sac", ".mseed", ".miniseed")) for url in row.get("resource_urls", []))

    @staticmethod
    def _resource_attempt_count(candidate: dict[str, Any], details: Any) -> int:
        """Return a bounded number of resource indexes worth attempting."""

        counts: list[int] = []
        resources = details.get("resources") if isinstance(details, dict) else None
        if isinstance(resources, list):
            counts.append(len(resources))
        if isinstance(details, dict):
            urls = details.get("resource_urls")
            if isinstance(urls, dict):
                try:
                    counts.append(int(urls.get("count") or 0))
                except (TypeError, ValueError):
                    pass
            elif isinstance(urls, list):
                counts.append(len(urls))
        try:
            counts.append(int(candidate.get("resource_count") or 0))
        except (TypeError, ValueError):
            pass
        urls = candidate.get("resource_urls")
        if isinstance(urls, list):
            counts.append(len(urls))
        count = max([value for value in counts if value > 0], default=1)
        return max(1, min(count, 3))

    @staticmethod
    def _recommendations(question: str, rows: list[Any]) -> str:
        """Return next actions for the NDP discovery result."""
        del rows
        q_lower = question.lower()
        if any(term in q_lower for term in ("seismic", "seismological", "three axes")):
            return (
                "Treat this as the NDP data-discovery and staging stage. If staging "
                "succeeded, pass the staged SAC/MiniSEED/waveform file to the relevant "
                "format, analysis, and visualization experts. If staging is blocked, "
                "surface the transport or size error and select a smaller concrete "
                "resource rather than inventing waveform analysis."
            )
        return (
            "Treat these as discovery results owned by the NDP catalog expert. Use "
            "ndp_get_dataset_details with a dataset id or name before downloading, then "
            "stage a concrete resource before routing quantitative work to analysis."
        )

    @staticmethod
    def _select_seismic_dataset(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Choose the most analysis-ready seismic row from compact NDP results."""
        for row in rows:
            haystack = " ".join(
                str(value)
                for value in (
                    row.get("title"),
                    row.get("name"),
                    row.get("notes"),
                    " ".join(str(name) for name in row.get("resource_names", [])),
                )
                if value
            ).lower()
            if "miniseed" in haystack or ("seismic" in haystack and "waveform" in haystack):
                return row
        return None

    @staticmethod
    def _select_stageable_seismic_dataset(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Prefer seismic rows with direct HTTP resources for staged analysis."""
        for row in rows:
            haystack = " ".join(
                str(value)
                for value in (
                    row.get("title"),
                    row.get("name"),
                    row.get("notes"),
                    " ".join(str(name) for name in row.get("resource_names", [])),
                )
                if value
            ).lower()
            if not any(term in haystack for term in ("waveform", "sac", "miniseed")):
                continue
            urls = [str(url).lower() for url in row.get("resource_urls", []) if url]
            if any(url.startswith(("http://", "https://")) for url in urls):
                return row
        return None

    @staticmethod
    def _dataset_priority(row: dict[str, Any]) -> int:
        """Score NDP rows so analysis-ready waveform data survives truncation."""
        haystack = " ".join(
            str(value)
            for value in (
                row.get("title"),
                row.get("name"),
                row.get("notes"),
                " ".join(str(name) for name in row.get("resource_names", [])),
                " ".join(str(fmt) for fmt in row.get("resource_formats", [])),
            )
            if value
        ).lower()
        urls = [str(url).lower() for url in row.get("resource_urls", []) if url]
        score = 0
        if any(term in haystack for term in ("waveform", "sac", "miniseed")):
            score += 20
        if any(url.startswith(("http://", "https://")) for url in urls):
            score += 8
        if any(url.startswith("osdf://") for url in urls):
            score += 4
        if "seismic" in haystack or "seismological" in haystack:
            score += 2
        if "lidar" in haystack or "point cloud" in haystack:
            score -= 8
        return score

    @staticmethod
    def _to_prediction(result: ExpertResult) -> dspy.Prediction:
        return dspy.Prediction(
            analysis=result.analysis,
            recommendations=result.recommendations,
            synthesis_source=result.source,
            tool_provenance=list(result.tools),
            metadata=dict(result.metadata),
        )

    def close(self) -> None:
        """Release tool execution resources if this expert owns them."""
        if self._owns_executor:
            self._tool_executor.close()

    @staticmethod
    def get_capabilities() -> dict[str, Any]:
        """Return NDP nested expert capability metadata."""
        return {
            "name": "NDP Catalog Expert",
            "description": (
                "Nested expert for National Data Platform discovery, catalog metadata, "
                "resource ranking, and bounded staging."
            ),
            "keywords": [
                "ndp",
                "national data platform",
                "earthscope",
                "dataset discovery",
                "catalog",
                "resource",
                "staging",
            ],
            "priority": 2,
        }


__all__ = ["NDPExpert"]
