"""Declared deterministic workflows (#948 S5, work item 4).

A tier-1 orchestrator blueprint may declare a ``workflow:`` block — a list of
``steps`` describing an a->b->c child pathway gated on typed ``workflow_state``
predicates. This REVIVES the retired continuation-contract schema shape wildfire
already authored (``when_state.<field>.exists`` / ``when_child_completed`` ->
next child), now as a first-class declaration executed by a real runner.

Two surfaces live here, in this owner module (never accreted onto ``builders.py``
or ``spawn_runtime.py``):

* **Declaration + typed validation** (:func:`parse_workflow`,
  :func:`workflow_validation_errors`). The loader validates the block against the
  expert's declared children — unknown child, dependency cycle, and malformed
  predicate become typed row errors that compose with the S4
  react-children-only hierarchy rules.

* **The runner** (:func:`run_declared_workflow`). Executes the declared steps
  DETERMINISTICALLY — declared infra determinism: the model is NOT in the loop
  for the declared steps (the DECLARATION is the decision; the pack author
  decided, clio executes what was declared and never infers). Each step is a real
  ``spawn_child_turn`` + wait with its own :class:`AgentTask` record (the S1-S3
  substrate), evaluating its gate over the ACCUMULATED typed ``workflow_state``.
  A gate that cannot be satisfied (missing field, a prior child that never
  completed) OR a spawned child that FAILS is a **typed stall**: the runner stops
  and returns the stall reason (step, predicate, observed state) — never a guess,
  never silent continuation.

The runner respects the per-depth concurrency caps of the spawn substrate; steps
run sequentially in declaration order (only one child in flight at a time), so a
declared workflow never contends for slots with itself. The MODEL decides what to
do with a stall — the runner surfaces it, it never re-routes or fabricates (⚑ #1
applies at the tool boundary where ``run_workflow`` returns to the react main).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional

from clio_agent import conf
from clio_agent.gact.workflow_step_watch import resolve_step_inactivity_s, watch_step

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef

logger = logging.getLogger(__name__)

# Typed stall reasons (no free-form strings on the wire).
STALL_PREDICATE_UNSATISFIED = "workflow_predicate_unsatisfied"
STALL_CHILD_FAILED = "workflow_child_failed"
STALL_SPAWN_REFUSED = "workflow_spawn_refused"
# A step whose child went INACTIVE (no observable progress) for the inactivity window
# while still RUNNING (non-terminal) — the progress-based liveness stall (#992). Distinct
# from a child that FAILED, so the wire record never claims "failed" for a child whose
# status is "running", and distinct from STALL_STEP_TIMEOUT (a declared absolute budget).
STALL_STEP_STALLED = "workflow_step_stalled"
# A step whose child exceeded a pack-DECLARED absolute per-step budget (``step.timeout_s``)
# while still RUNNING. Retained ONLY for the opt-in declared budget — the default liveness
# watch is progress-based (STALL_STEP_STALLED), never a wall-clock bound on legitimate work
# (the owner's liveness principle, #992). A child that FAILED is STALL_CHILD_FAILED.
STALL_STEP_TIMEOUT = "workflow_step_timeout"

# Progress-based step liveness lives in the owner module ``workflow_step_watch`` (#992);
# ``run_declared_workflow`` calls ``watch_step`` and maps its verdict to the typed stalls
# above. ``resolve_step_inactivity_s`` supplies the config-first default window.


# ===========================================================================
# Declaration model
# ===========================================================================


@dataclass(frozen=True)
class StatePredicate:
    """One typed gate over the accumulated ``workflow_state``.

    ``field_path`` is a dotted path descended through nested mappings
    (``acquisition.status``). Exactly one of the two forms is set:

    * ``kind == "exists"`` — the path must (``exists=True``) or must not
      (``exists=False``) be present.
    * ``kind == "equals"`` — the path must be present AND equal ``equals``.
    """

    field_path: str
    kind: Literal["exists", "equals"]
    exists: Optional[bool] = None
    equals: Any = None

    def as_declared(self) -> dict[str, Any]:
        """The predicate in its authored (wire-facing) shape, for stall rows."""

        inner: dict[str, Any] = (
            {"exists": self.exists} if self.kind == "exists" else {"equals": self.equals}
        )
        return {"when_state": {self.field_path: inner}}


@dataclass(frozen=True)
class WorkflowStep:
    """One declared step: spawn ``child`` when its gate holds."""

    id: str
    child: str
    task: str
    when_state: tuple[StatePredicate, ...]
    when_child_completed: str  # "" when no completion gate
    # Optional pack-DECLARED absolute per-step budget in seconds (#992): a hard wall this
    # step's child may not exceed even while actively progressing. ``0.0`` (the default,
    # and the absence of ``timeout_s`` in the declaration) means progress-based liveness
    # ONLY — no absolute bound. A pack author sets this only when a step genuinely must be
    # bounded regardless of activity; the default watch never wall-clock-bounds legit work.
    timeout_s: float = 0.0


@dataclass(frozen=True)
class DeclaredWorkflow:
    """A parsed, structurally-valid workflow declaration."""

    steps: tuple[WorkflowStep, ...]


# ===========================================================================
# Parsing + typed validation
# ===========================================================================


def _workflow_meta(agent_def: "AgentDef") -> Any:
    """The raw ``workflow`` declaration off an AgentDef's metadata (or ``None``)."""

    metadata = getattr(agent_def, "metadata", None)
    return metadata.get("workflow") if isinstance(metadata, Mapping) else None


def _step_raw_key(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _parse_predicates(when_state: Any, *, step_id: str) -> tuple[list[StatePredicate], list[str]]:
    """Parse a step's ``when_state`` mapping into typed predicates + structural errors.

    A predicate mapping must declare exactly one of ``exists`` (a bool) or
    ``equals`` (any value). Anything else is a malformed-predicate authoring error.
    """

    if when_state in (None, "", {}):
        return [], []
    if not isinstance(when_state, Mapping):
        return [], [
            f"workflow step {step_id!r} when_state must be a mapping of "
            f"field-path -> {{exists|equals}}, got {type(when_state).__name__}"
        ]
    predicates: list[StatePredicate] = []
    errors: list[str] = []
    for raw_field, raw_pred in when_state.items():
        field_path = str(raw_field).strip()
        if not field_path:
            errors.append(f"workflow step {step_id!r} has an empty when_state field path")
            continue
        if not isinstance(raw_pred, Mapping):
            errors.append(
                f"workflow step {step_id!r} when_state field {field_path!r} must map to "
                f"{{exists|equals}}, got {type(raw_pred).__name__}"
            )
            continue
        has_exists = "exists" in raw_pred
        has_equals = "equals" in raw_pred
        if has_exists == has_equals:
            errors.append(
                f"workflow step {step_id!r} when_state field {field_path!r} must declare "
                "exactly one of 'exists' or 'equals'"
            )
            continue
        if has_exists:
            exists_val = raw_pred["exists"]
            if not isinstance(exists_val, bool):
                errors.append(
                    f"workflow step {step_id!r} when_state field {field_path!r} 'exists' "
                    f"must be a bool, got {type(exists_val).__name__}"
                )
                continue
            predicates.append(
                StatePredicate(field_path=field_path, kind="exists", exists=exists_val)
            )
        else:
            predicates.append(
                StatePredicate(field_path=field_path, kind="equals", equals=raw_pred["equals"])
            )
    return predicates, errors


def _parse_step(raw: Any, *, index: int) -> tuple[Optional[WorkflowStep], list[str]]:
    if not isinstance(raw, Mapping):
        return None, [f"workflow step #{index} must be a mapping, got {type(raw).__name__}"]
    step_id = str(_step_raw_key(raw, "id") or "").strip()
    child = str(_step_raw_key(raw, "child") or "").strip()
    display_id = step_id or child or f"#{index}"
    errors: list[str] = []
    if not child:
        errors.append(f"workflow step {display_id!r} missing required 'child'")
    task = str(_step_raw_key(raw, "task") or "").strip()
    when_child = str(_step_raw_key(raw, "when_child_completed") or "").strip()
    predicates, pred_errors = _parse_predicates(
        _step_raw_key(raw, "when_state"), step_id=display_id
    )
    errors.extend(pred_errors)
    timeout_s, timeout_error = _parse_step_timeout(raw, step_id=display_id)
    if timeout_error:
        errors.append(timeout_error)
    if errors:
        return None, errors
    return (
        WorkflowStep(
            id=step_id or child,
            child=child,
            task=task,
            when_state=tuple(predicates),
            when_child_completed=when_child,
            timeout_s=timeout_s,
        ),
        [],
    )


def _parse_step_timeout(raw: Mapping[str, Any], *, step_id: str) -> tuple[float, str]:
    """Parse a step's optional declared absolute budget ``timeout_s`` (#992).

    Absent → ``(0.0, "")`` (progress-based liveness only). Present → it must be a
    positive number; anything else is a typed authoring error (like a malformed
    predicate) that disables the expert rather than silently degrading to no budget.
    """

    raw_value = _step_raw_key(raw, "timeout_s")
    if raw_value in (None, ""):
        return 0.0, ""
    try:
        value = conf.as_float(raw_value)
    except (ValueError, TypeError):
        return 0.0, (
            f"workflow step {step_id!r} 'timeout_s' must be a positive number, "
            f"got {type(raw_value).__name__}"
        )
    if value <= 0:
        return 0.0, f"workflow step {step_id!r} 'timeout_s' must be > 0, got {value}"
    return value, ""


def _build_workflow(raw: Any) -> tuple[Optional[DeclaredWorkflow], list[str]]:
    """Structural parse of a raw ``workflow`` mapping.

    Returns ``(workflow, errors)``. ``workflow`` is ``None`` when the block is
    absent/empty OR any step is structurally malformed (the errors describe why).
    """

    if not isinstance(raw, Mapping) or not raw:
        return None, []
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, (list, tuple)) or not steps_raw:
        return None, ["workflow.steps must be a non-empty list"]
    steps: list[WorkflowStep] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, step_raw in enumerate(steps_raw):
        step, step_errors = _parse_step(step_raw, index=index)
        errors.extend(step_errors)
        if step is None:
            continue
        if step.id in seen_ids:
            errors.append(f"workflow has a duplicate step id: {step.id!r}")
            continue
        seen_ids.add(step.id)
        steps.append(step)
    if errors:
        return None, errors
    return DeclaredWorkflow(steps=tuple(steps)), []


def parse_workflow(agent_def: "AgentDef") -> Optional[DeclaredWorkflow]:
    """The parsed workflow of a blueprint AgentDef (``None`` when unset/malformed).

    Used by the runner and by the ``run_workflow`` tool gate. A structurally
    malformed block returns ``None`` (the loader has already disabled the expert
    with the typed errors, so the tool is unreachable), never a partial workflow.
    The declaration lives on ``metadata['workflow']`` (mirrored there by the loader,
    like ``fanout`` — no new AgentDef field / god-file growth).
    """

    workflow, _errors = _build_workflow(_workflow_meta(agent_def))
    return workflow


def _dependency_cycle(steps: tuple[WorkflowStep, ...]) -> list[str]:
    """Detect a cycle in the ``when_child_completed`` dependency graph.

    An edge runs from a step to the step that PRODUCES the child it waits on. A
    cycle means the steps can never linearize (deterministic execution is
    impossible). Returns the step ids on the first cycle found, or ``[]``.
    """

    producer: dict[str, str] = {}
    for step in steps:
        producer.setdefault(step.child, step.id)
    by_id = {step.id: step for step in steps}
    edges: dict[str, str] = {}
    for step in steps:
        if step.when_child_completed and step.when_child_completed in producer:
            dep_step = producer[step.when_child_completed]
            if dep_step != step.id:
                edges[step.id] = dep_step

    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(by_id, WHITE)

    def _visit(node: str, stack: list[str]) -> list[str]:
        color[node] = GREY
        stack.append(node)
        nxt = edges.get(node)
        if nxt is not None:
            if color.get(nxt) == GREY:
                return stack[stack.index(nxt) :] + [nxt]
            if color.get(nxt) == WHITE:
                found = _visit(nxt, stack)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return []

    for sid in by_id:
        if color[sid] == WHITE:
            cycle = _visit(sid, [])
            if cycle:
                return cycle
    return []


def workflow_validation_errors(agent_def: "AgentDef", declared_children: set[str]) -> list[str]:
    """Typed validation of a blueprint's ``workflow`` against its declared children.

    Composes structural errors (malformed predicate, missing child, non-list
    steps) with semantic errors — an unknown child, a ``when_child_completed``
    that no step produces, and a dependency cycle. Returns ``[]`` when there is no
    workflow OR it is fully valid; each string is appended to the expert's
    ``validation_errors`` (which disables the expert), exactly like the S5
    module-variant and S4 react-children checks.
    """

    raw = _workflow_meta(agent_def)
    if not isinstance(raw, Mapping) or not raw:
        return []
    workflow, errors = _build_workflow(raw)
    if workflow is None:
        return errors
    produced: set[str] = {step.child for step in workflow.steps}
    # First step index that produces each child (the runner executes in declaration order).
    producer_index: dict[str, int] = {}
    for i, step in enumerate(workflow.steps):
        producer_index.setdefault(step.child, i)
    for consumer_index, step in enumerate(workflow.steps):
        if step.child not in declared_children:
            errors.append(
                f"workflow step {step.id!r} references undeclared child {step.child!r} "
                f"(declared children: {sorted(declared_children)})"
            )
        if step.when_child_completed:
            if step.when_child_completed not in declared_children:
                errors.append(
                    f"workflow step {step.id!r} when_child_completed references undeclared "
                    f"child {step.when_child_completed!r}"
                )
            elif step.when_child_completed not in produced:
                errors.append(
                    f"workflow step {step.id!r} when_child_completed {step.when_child_completed!r} "
                    "is never produced by any step"
                )
            elif producer_index[step.when_child_completed] > consumer_index:
                # #953 [6]: acyclic but MISORDERED — the producer step is declared AFTER
                # this consumer, so the runner (which never topo-sorts) stalls on this gate
                # forever. A typed load error, like the sibling unproduced check above.
                errors.append(
                    f"workflow step {step.id!r} when_child_completed "
                    f"{step.when_child_completed!r} is produced by a LATER step "
                    "(declaration order must satisfy the dependency order)"
                )
    cycle = _dependency_cycle(workflow.steps)
    if cycle:
        errors.append("workflow has a dependency cycle: " + " -> ".join(cycle))
    return errors


def workflow_row_errors(row: "AgentDef", all_rows: list["AgentDef"]) -> list[str]:
    """Loader-facing one-liner (#948 S5): validate ``row``'s workflow against its
    declared children (rows whose ``parent_id`` is ``row.id``). Returns ``[]`` when the
    row declares no workflow — so the expert loader composes it unconditionally with
    the react-children hierarchy rules without growing the god file."""

    if not _workflow_meta(row):
        return []
    return workflow_validation_errors(row, {r.id for r in all_rows if r.parent_id == row.id})


# ===========================================================================
# Predicate evaluation (pure)
# ===========================================================================


@dataclass(frozen=True)
class GateResult:
    """Whether a step's gate holds, plus the unmet predicate + observed state."""

    satisfied: bool
    predicate: Optional[dict[str, Any]] = None
    observed: Optional[dict[str, Any]] = None


def _field_lookup(state: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """Descend a dotted path through nested mappings. Returns ``(found, value)``."""

    cur: Any = state
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def evaluate_step_gate(
    step: WorkflowStep,
    state: Mapping[str, Any],
    completed: set[str],
) -> GateResult:
    """Evaluate a step's gate over the accumulated state + completed children.

    The gate holds when the ``when_child_completed`` child (if any) has completed
    AND every ``when_state`` predicate holds (match=all). The FIRST unmet predicate
    is surfaced with the observed reality — never a guess.
    """

    if step.when_child_completed and step.when_child_completed not in completed:
        return GateResult(
            satisfied=False,
            predicate={"when_child_completed": step.when_child_completed},
            observed={"completed_children": sorted(completed)},
        )
    for pred in step.when_state:
        found, value = _field_lookup(state, pred.field_path)
        if pred.kind == "exists":
            if found != pred.exists:
                return GateResult(
                    satisfied=False,
                    predicate=pred.as_declared(),
                    observed={"field": pred.field_path, "exists": found},
                )
        else:  # equals
            if not found or value != pred.equals:
                return GateResult(
                    satisfied=False,
                    predicate=pred.as_declared(),
                    observed={"field": pred.field_path, "found": found, "value": value},
                )
    return GateResult(satisfied=True)


def _merge_step_state(accumulated: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """Overlay a completed step's typed ``workflow_state`` onto the accumulator.

    Sequential (declaration-order) top-level overlay: a later step wins a colliding
    top-level key. Deterministic because steps run one at a time in declaration
    order (unlike the concurrent ensemble merge, which must key on run_index)."""

    for key, value in incoming.items():
        accumulated[str(key)] = value


# ===========================================================================
# The runner
# ===========================================================================


def _render_task(step: WorkflowStep, request: str) -> str:
    """The child's task text: the declared step template, grounded with the request."""

    task = step.task or f"Perform the {step.child} step of the declared workflow."
    if request.strip():
        return f"{task}\n\n[request]\n{request.strip()}"
    return task


def run_declared_workflow(
    app: "FastAPI",
    agent_def: "AgentDef",
    parent_session_id: str,
    *,
    requesting_expert_id: str = "",
    request: str = "",
    inactivity_window_s: Optional[float] = None,
) -> dict[str, Any]:
    """Execute ``agent_def``'s declared workflow deterministically over the substrate.

    Steps run sequentially in declaration order. Before each step the runner
    evaluates its gate over the ACCUMULATED typed ``workflow_state``; an unmet gate
    is a typed :data:`STALL_PREDICATE_UNSATISFIED`. A satisfied step is spawned as a
    real child turn (its own :class:`AgentTask` record) and WATCHED for PROGRESS: a step
    is stalled (:data:`STALL_STEP_STALLED`) only when its child shows no observable
    activity for ``inactivity_window_s`` (the owner's liveness principle — a
    legitimately-heavy step that keeps progressing is never wall-clock-bounded, #992). A
    step that DECLARES an absolute budget (``step.timeout_s``) is additionally hard-bounded
    (:data:`STALL_STEP_TIMEOUT`) even while active. A child that terminates non-completed is
    :data:`STALL_CHILD_FAILED`. A completed child's typed ``workflow_state`` is merged into
    the accumulator (later step wins a collision) and its id joins the completed set. On any
    non-completing outcome the orphaned child is cancelled (the S7 cancel path). Returns::

        {"status": "completed" | "stalled",
         "steps": [{step_id, child, task_id, run_index, child_status, workflow_state}...],
         "workflow_state": <accumulated>,
         "stall": {reason, step, predicate, observed} | None}

    ``inactivity_window_s`` defaults to the configured window
    (:func:`_resolve_step_inactivity_s`, file → env → 120s). The MODEL decides what to do
    with a stall — the runner never guesses.
    """

    window = inactivity_window_s if inactivity_window_s is not None else resolve_step_inactivity_s()

    from clio_agent.gact.agent_tasks import STATUS_COMPLETED  # noqa: PLC0415
    from clio_agent.gact.agents.invoker import SpawnError, TaskSpec  # noqa: PLC0415
    from clio_agent.gact.agents.spawn_runtime import (  # noqa: PLC0415
        _current_session_depth,  # noqa: PLC0415
        emit_workflow_step_return,
        emit_workflow_step_start,
    )
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _active_semantic_turn_id,
    )
    from clio_agent.gact.spawn_context import bind_task_spec_to_parent  # noqa: PLC0415

    # #953 [2]/[8]: stamp each step's spawn with the active parent turn id so run_index
    # resets per turn (else it accumulates across the whole session).
    active_turn_id = _active_semantic_turn_id()

    workflow = parse_workflow(agent_def)
    requesting = requesting_expert_id or agent_def.id
    step_records: list[dict[str, Any]] = []
    accumulated: dict[str, Any] = {}
    completed: set[str] = set()

    if workflow is None:
        # Gate ensures this is unreachable from a valid expert; surface typed, never
        # a silent empty success.
        logger.warning("run_workflow invoked with no declared workflow agent=%s", agent_def.id)
        return {
            "status": "stalled",
            "steps": step_records,
            "workflow_state": accumulated,
            "stall": {
                "reason": "no_workflow_declared",
                "step": "",
                "predicate": None,
                "observed": {"agent_id": agent_def.id},
            },
        }

    depth = _current_session_depth(app, parent_session_id) + 1

    for step in workflow.steps:
        gate = evaluate_step_gate(step, accumulated, completed)
        if not gate.satisfied:
            logger.warning(
                "workflow stall reason=%s step=%s predicate=%s agent=%s",
                STALL_PREDICATE_UNSATISFIED,
                step.id,
                gate.predicate,
                agent_def.id,
            )
            return {
                "status": "stalled",
                "steps": step_records,
                "workflow_state": accumulated,
                "stall": {
                    "reason": STALL_PREDICATE_UNSATISFIED,
                    "step": step.id,
                    "predicate": gate.predicate,
                    "observed": gate.observed,
                },
            }

        task_text = _render_task(step, request)
        try:
            spawned = app.state.expert_invoker.invoke(
                bind_task_spec_to_parent(
                    app,
                    TaskSpec(
                        child_expert_id=step.child,
                        task_text=task_text,
                        parent_session_id=parent_session_id,
                        requesting_expert_id=requesting,
                        parent_turn_id=active_turn_id,
                        depth=depth,
                        mode="sync",
                        workflow_state=dict(accumulated) or None,
                    ),
                ),
            )
        except SpawnError as exc:
            logger.warning(
                "workflow stall reason=%s step=%s spawn_reason=%s agent=%s",
                STALL_SPAWN_REFUSED,
                step.id,
                exc.reason,
                agent_def.id,
            )
            return {
                "status": "stalled",
                "steps": step_records,
                "workflow_state": accumulated,
                "stall": {
                    "reason": STALL_SPAWN_REFUSED,
                    "step": step.id,
                    "predicate": {"child": step.child},
                    "observed": {"spawn_reason": exc.reason},
                },
            }

        emit_workflow_step_start(app, parent_session_id, agent_def, step.child, task_text, spawned)
        task, outcome, observed_inactivity_s = watch_step(
            app,
            spawned.task_id,
            spawned.child_session_id,
            inactivity_window_s=window,
            absolute_budget_s=step.timeout_s,
        )
        child_status = task.status if task is not None else "unknown"
        child_state = (task.result or {}).get("workflow_state", {}) if task is not None else {}
        if not isinstance(child_state, dict):
            child_state = {}
        step_records.append(
            {
                "step_id": step.id,
                "child": step.child,
                "task_id": spawned.task_id,
                "run_index": spawned.run_index,
                "child_status": child_status,
                "workflow_state": child_state,
            }
        )
        if task is not None:
            emit_workflow_step_return(app, parent_session_id, agent_def, task)

        if outcome != "terminal" or task is None or task.status != STATUS_COMPLETED:
            # A non-completing step. Three distinct typed reasons, never conflated (#992):
            #   * "stalled": the child went INACTIVE for the inactivity window while still
            #     RUNNING (progress-based liveness) — NOT a failure, and NOT a wall-clock
            #     bound on legitimate work.
            #   * "timeout": the child exceeded a pack-DECLARED absolute budget while still
            #     RUNNING (#953 [7], opt-in only).
            #   * STALL_CHILD_FAILED: the child terminated non-completed (failed/cancelled)
            #     or was never on the registry.
            # A still-RUNNING orphan (stalled/timeout) is cancelled transitively (the S7
            # cancel path) so it stops holding a per-depth pool slot AND stops producing.
            non_terminal = outcome in ("stalled", "timeout")
            if outcome == "stalled":
                reason = STALL_STEP_STALLED
            elif outcome == "timeout":
                reason = STALL_STEP_TIMEOUT
            else:
                reason = STALL_CHILD_FAILED
            if non_terminal:
                app.state.expert_invoker.cancel(spawned)
            logger.warning(
                "workflow stall reason=%s step=%s child=%s child_status=%s agent=%s",
                reason,
                step.id,
                step.child,
                child_status,
                agent_def.id,
            )
            observed: dict[str, Any] = {
                "task_id": spawned.task_id,
                "child_status": child_status,
                "error_reason": getattr(task, "error_reason", "") if task else "",
            }
            if outcome == "stalled":
                observed["inactivity_s"] = window
                observed["observed_inactivity_s"] = round(observed_inactivity_s, 3)
            elif outcome == "timeout":
                observed["timeout_s"] = step.timeout_s
            return {
                "status": "stalled",
                "steps": step_records,
                "workflow_state": accumulated,
                "stall": {
                    "reason": reason,
                    "step": step.id,
                    "predicate": {"child": step.child},
                    "observed": observed,
                },
            }

        _merge_step_state(accumulated, child_state)
        completed.add(step.child)

    return {
        "status": "completed",
        "steps": step_records,
        "workflow_state": accumulated,
        "stall": None,
    }
