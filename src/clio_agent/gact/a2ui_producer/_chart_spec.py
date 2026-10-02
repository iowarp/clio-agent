"""Server-side ``clio.chart.v1`` spec/preset validation (#1549 G3).

The catalog's JSON Schema already enforces most of the chart guard
recursively at ``apply_messages`` time (top-level key allowlist, no
``url``/``usermeta`` at any depth, no ``bind.element`` at any depth, ``data``
only as ``{"name": "source"}``) -- a failure there comes back as the generic
``a2ui_validation_failed``. Three rules JSON Schema cannot express at all
(the spec's compact-JSON byte size, its view count, and its nesting depth)
were previously enforced ONLY by the gact-tui renderer, so a spec breaking
one of them was accepted here as ``rendered: true`` and only failed client
side -- invisible to the agent that produced it (issue #1549 G3). This module
closes that gap by running the SAME guard the renderer runs,
``clio_schemas.a2ui.chart_spec.check_chart_spec``, before
``create_a2ui_surface``/``update_a2ui_components`` accept the surface, and
returns a specific, actionable typed refusal instead.

A ``preset``-based chart carries no ``spec`` at all -- the preset is expanded
into one at RENDER time, client-side (``chart-presets.ts``). This module
mirrors that expansion server-side too, with
``clio_schemas.a2ui.chart_spec.render_preset``, so a bad preset parameter
(an unknown preset, a missing required field, a field the preset does not
use, or a malformed ``xType``/``selectionParam``) is also a typed refusal
here rather than a silent client-side render failure. What stays CLIENT-ONLY:
whether a dataUri'd dataset's REAL values actually suit the preset visually
(e.g. too many categories for a legend) -- that is a rendering-quality
concern, not a guard rule, and is out of scope here, same as for an inline
``spec``. Field-NAME-vs-dataset-schema checks (does ``xField`` name a real
column) are a separate, already-shipped check
(:mod:`clio_agent.gact.a2ui_producer._data_reference`, #1533 S4); this module
only validates the spec/preset SHAPE.

Dependency note: clio-agent pins an EXACT ``clio-schemas`` version
(``pyproject.toml``). The chart guard's rule set is versioned
(``CHART_SPEC_RULES_VERSION``) and this module always enforces whatever rules
the PINNED version ships -- it never branches on the installed version. In
particular, until the coordinated release bumps this pin to 0.5.2, a spec
using 0.5.2's data-free layout keys (``columns``, ``spacing``, ``padding``,
``align``, ``bounds``, ``center``, ``projection`` -- issue #1549 G4) is
refused here with ``top_level_key_not_allowed`` even though a 0.5.2 gact-tui
client guard already accepts it; this resolves itself the moment the pin
bump lands, same as every other guard-rule change.
"""

from __future__ import annotations

from typing import Any

from clio_schemas.a2ui.chart_spec import (
    PRESET_SLOTS,
    ChartSpecError,
    check_chart_spec,
    render_preset,
)

from clio_agent.gact.a2ui_producer._refusal import refusal

_CHART_COMPONENT = "clio.chart.v1"


def validate_chart_components(components: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Validate every ``clio.chart.v1`` component's inline ``spec`` or ``preset``.

    Args:
        components: The producer call's components, in wire shape (after the
            export boundary, same as :func:`clio_agent.gact.a2ui_producer.
            _data_reference.validate_component_data_references`).

    Returns:
        ``None`` when every chart component's spec/preset is acceptable;
        otherwise a typed refusal dict (see :func:`clio_agent.gact.
        a2ui_producer._refusal.refusal`), never raised.
    """

    for component in components:
        if not isinstance(component, dict) or component.get("component") != _CHART_COMPONENT:
            continue
        outcome = _validate_one(component)
        if outcome is not None:
            return outcome
    return None


def _validate_one(component: dict[str, Any]) -> dict[str, Any] | None:
    component_id = str(component.get("id") or "")
    spec = component.get("spec")
    if isinstance(spec, dict):
        return _validate_spec(component_id, spec)
    preset = component.get("preset")
    if isinstance(preset, str) and preset:
        return _validate_preset(component_id, preset, component)
    # Neither spec nor preset is a dict/non-empty string: the catalog's own
    # "exactly one of spec or preset" schema rule reports that, never
    # duplicated here.
    return None


def _validate_spec(component_id: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    violations = check_chart_spec(spec)
    if not violations:
        return None
    codes = sorted({violation.code for violation in violations})
    violation_text = "; ".join(str(violation) for violation in violations)
    return refusal(
        "a2ui_chart_spec_rejected",
        detail=(
            f"component {component_id!r} spec failed the chart guard "
            f"({', '.join(codes)}): {violation_text}"
        ),
    )


def _validate_preset(
    component_id: str, preset: str, component: dict[str, Any]
) -> dict[str, Any] | None:
    fields = {
        slot: component[slot] for slot in PRESET_SLOTS if isinstance(component.get(slot), str)
    }
    try:
        render_preset(preset, fields)
    except ValueError as exc:
        return refusal(
            "a2ui_chart_preset_invalid",
            detail=f"component {component_id!r} preset {preset!r} is invalid: {exc}",
        )
    except ChartSpecError as exc:
        # A template bug (a shipped preset itself fails its own guard) --
        # never the model's fault, but still a typed refusal rather than a
        # 500: nothing the model changes about this call would fix it, so
        # the hint says so explicitly instead of the generic preset hint.
        return refusal(
            "a2ui_chart_spec_rejected",
            detail=(
                f"component {component_id!r} preset {preset!r} rendered a spec that "
                f"fails the chart guard (this is a preset template defect, not a "
                f"caller mistake): {exc}"
            ),
            hint="try a different preset or an inline spec; report this preset as broken",
        )
    return None


__all__ = ["validate_chart_components"]
