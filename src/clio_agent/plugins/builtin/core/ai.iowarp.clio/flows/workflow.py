"""The ``workflow`` flow: run a workflow's steps as tasks, as soon as each step's inputs finish.

Input (from ``run_workflow`` or ``ctx.call("workflow", {...})``)::

    {"workflow": {"steps": [...]}, "task": "...", "editors": ["critic"],
     "max_added_steps": 4, "allowed_targets": [...], "approve_edits": true}

A step whose target is a named editor runs with ``workflow_add_step`` and
``workflow_cancel_step``; its proposals are read from its own settled result, so a
replay applies exactly the same edits. Growth is open unless the caller sets
``max_added_steps`` or ``max_edit_rounds``, or the person's settings cap them (the
tighter wins; the bounds in force are recorded when the run starts); ``allowed_targets``
limits what may be added. With ``approve_edits`` each proposal waits on a Question; a
proposed step is recorded as pending until then. A failed step is first offered to the
editors as a typed stall (they may add a repair step within the same bounds); if nobody
edits, the stall returns to the caller.

A running step may ``fork`` itself (``gact/agent_fork.py``): the fork runs beside it as
``<step>~<n>``, counts as an added step, and the step's wait covers it, so its
dependents wait on both and ``$<step>`` reads every answer of the family, labelled. A
failed fork is offered to the editors like a failed step.

With ``isolate`` the run gets ``<workspace>/runs/<run-id>/`` and each step its own
folder in it, which is the only place that step's session may write
(``gact/node_folders.py``); a fork starts with a copy of its step's folder. A run
whose OS fence cannot narrow writes stalls at start (``isolation_unconfined``).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from clio_agent.flows import limits
from clio_agent.flows.outcomes import (
    CallCancelled,
    CallFailed,
    FlowCheckpointLimit,
    FlowOutcome,
    FlowStall,
    FlowTargetUnknown,
)
from clio_agent.flows.values import CallResult
from clio_agent.sdk import Flow, Step, Workflow, WorkflowError

_EDIT_TOOLS = ("workflow_add_step", "workflow_cancel_step")


#: ``$task`` or ``$<step id>``; step ids are lowercase letters, digits, ``-`` and ``_``,
#: so a full stop ends the reference ("Check: $draft." refers to ``draft``).
_REFERENCE = re.compile(r"\$([A-Za-z0-9_][A-Za-z0-9_-]*)")


def _substitute(value: Any, task: Any, results: dict[str, Any]) -> Any:
    """Replace ``$task`` and ``$<step>`` references: a whole-string reference keeps the
    value itself; a reference inside text becomes the text of that value."""

    if isinstance(value, str):

        def resolve(name: str) -> Any:
            if name == "task":
                return task
            result = results[name]
            forks = _fork_labels(name, results)
            if not forks:
                return getattr(result, "answer", result)
            # A forked step's reference reads the whole family, each answer labelled.
            return "\n\n".join(
                f"[{label}]\n{_text(getattr(results[label], 'answer', results[label]))}"
                for label in (name, *forks)
            )

        def known(name: str) -> bool:
            return name == "task" or name in results

        def named(raw: str) -> str:
            # A trailing "-" is text after the reference unless a step is named with it.
            name = raw
            while name.endswith("-") and not known(name):
                name = name[:-1]
            return name

        def replace(match: re.Match[str]) -> str:
            name = named(match.group(1))
            if not known(name):
                return match.group(0)
            return _text(resolve(name)) + match.group(1)[len(name) :]

        whole = _REFERENCE.fullmatch(value)
        if whole and known(whole.group(1)):
            return resolve(whole.group(1))
        return _REFERENCE.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute(v, task, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, task, results) for v in value]
    return value


def _fork_labels(step_id: str, labelled: Any) -> list[str]:
    """``<step>~1``, ``<step>~2`` ... among ``labelled``'s keys, in fork order."""

    prefix = step_id + "~"
    found = [k for k in labelled if k.startswith(prefix) and k[len(prefix) :].isdigit()]
    return sorted(found, key=lambda k: int(k[len(prefix) :]))


def _isolate(workspace_root: str, run_id: str) -> dict[str, Any]:
    from clio_agent.gact.node_folders import isolation_refusal, run_folder  # noqa: PLC0415

    if not workspace_root:
        return {"accepted": False, "reason": "no_workspace", "error": "no workspace folder"}
    return isolation_refusal() or {"folder": str(run_folder(workspace_root, run_id))}


def _bounds(asked_added: int | None, asked_rounds: int | None) -> dict[str, int | None]:
    """The growth bounds in force: the caller's, capped by the person's settings."""

    return {
        "max_added_steps": limits.effective(asked_added, limits.max_added_steps()),
        "max_edit_rounds": limits.effective(asked_rounds, limits.max_edit_rounds()),
    }


def _bound(value: Any) -> int | None:
    return None if value is None else int(value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


class WorkflowFlow(Flow):
    """Run a workflow; editors may grow it within bounds, with optional approval."""

    id = "workflow"

    async def forward(self, task: Any, ctx: Any) -> Any:
        spec = task if isinstance(task, dict) else {"workflow": task}
        try:
            workflow = Workflow.from_dict(spec.get("workflow"))
        except WorkflowError as exc:
            raise FlowStall("workflow_invalid", str(exc), observed=spec.get("workflow")) from exc
        run = _WorkflowRun(ctx, workflow, spec)
        return await run.execute()


class _WorkflowRun:
    def __init__(self, ctx: Any, workflow: Workflow, spec: dict[str, Any]) -> None:
        self.ctx = ctx
        self.task = spec.get("task", "")
        self.steps: dict[str, Step] = {step.id: step for step in workflow.steps}
        self.editors = {str(e) for e in spec.get("editors") or ()}
        self.asked = (_bound(spec.get("max_added_steps")), _bound(spec.get("max_edit_rounds")))
        self.max_added_steps: int | None = None
        self.max_edit_rounds: int | None = None
        allowed = spec.get("allowed_targets")
        self.allowed = {str(t) for t in allowed} if allowed is not None else None
        self.approve = bool(spec.get("approve_edits", False))
        self.isolate = bool(spec.get("isolate", False))
        #: The run's folder when isolated: ``<workspace>/runs/<run-id>/``.
        self.folder = ""
        self.status: dict[str, str] = dict.fromkeys(self.steps, "pending")
        self.results: dict[str, Any] = {}
        self.errors: dict[str, dict[str, Any]] = {}
        self.cancelled: list[CallCancelled] = []
        self.added = 0
        self.rounds = 0
        self.handles: dict[str, str] = {}
        self.invocations: dict[str, str] = {}
        self.awaiting: dict[str, dict[str, Any]] = {}
        #: A step's forks by label (``<step>~<n>``): the step it forked, handle, status.
        self.forks: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ helpers
    def _ready(self) -> list[Step]:
        return [
            step
            for step_id, step in self.steps.items()
            if self.status[step_id] == "pending"
            and all(self.status.get(dep) == "completed" for dep in step.after)
        ]

    def _blocked(self) -> list[str]:
        """Pending steps that can never run: a dependency failed or was cancelled."""

        return [
            step_id
            for step_id, step in self.steps.items()
            if self.status[step_id] == "pending"
            and any(self.status.get(dep) in {"failed", "cancelled"} for dep in step.after)
        ]

    def _input(self, step: Step) -> Any:
        """A step's input: its own, or the task plus its instructions and dependencies' results."""

        if step.input is not None:
            return _substitute(step.input, self.task, self.results)
        if not step.after and not step.description:
            return self.task
        payload: dict[str, Any] = {"task": self.task}
        if step.description:
            payload["instructions"] = step.description
        if step.after:
            payload["inputs"] = {
                label: self._dependency(label)
                for dep in step.after
                for label in (dep, *_fork_labels(dep, self.results))
            }
        return payload

    def _dependency(self, step_id: str) -> Any:
        result = self.results.get(step_id)
        answer = getattr(result, "answer", "")
        response = getattr(result, "response_text", answer)
        state = getattr(result, "workflow_state", {}) if result is not None else {}
        if state or response != answer:
            return {
                "answer": answer,
                **({"response_text": response} if response != answer else {}),
                **({"workflow_state": state} if state else {}),
            }
        return answer

    def summary(self) -> dict[str, Any]:
        return {
            "steps": [
                {
                    **step.to_dict(),
                    "status": self.status[step.id],
                    "handle": self.handles.get(step.id, ""),
                }
                for step in self.steps.values()
            ],
            "forks": [{"id": label, **row} for label, row in self.forks.items()],
            "added": self.added,
            "rounds": self.rounds,
        }

    def _rounds_spent(self) -> bool:
        return self.max_edit_rounds is not None and self.rounds >= self.max_edit_rounds

    async def _publish(self) -> None:
        """Publish the live workflow: steps with status, and proposals awaiting approval."""

        summary = self.summary()
        for row in summary["steps"]:
            row["invocation"] = self.invocations.get(row["id"], "")
        await self.ctx.publish(
            {
                "kind": "workflow",
                **summary,
                "awaiting_approval": list(self.awaiting.values()),
                "editors": sorted(self.editors),
                "max_added_steps": self.max_added_steps,
                "max_edit_rounds": self.max_edit_rounds,
                "approve_edits": self.approve,
                **({"folder": self.folder} if self.folder else {}),
            }
        )

    # -------------------------------------------------------------- run
    async def execute(self) -> dict[str, Any]:
        running: dict[asyncio.Future[Any], str] = {}
        bounds = await self.ctx.step(_bounds, *self.asked)
        self.max_added_steps, self.max_edit_rounds = (
            bounds["max_added_steps"],
            bounds["max_edit_rounds"],
        )
        if self.isolate:
            root = self.ctx.workspace.root
            isolated = await self.ctx.step(_isolate, str(root or ""), self.ctx.run_id)
            if "folder" not in isolated:
                raise FlowStall(isolated["reason"], isolated["error"], observed=isolated)
            self.folder = isolated["folder"]
        await self._publish()
        while True:
            while blocked := self._blocked():
                # A skipped step blocks its own dependents in turn.
                for step_id in blocked:
                    self.status[step_id] = "cancelled"
                    await self.ctx.record_event({"event": "step_skipped", "step": step_id})
            for step in self._ready():
                self.status[step.id] = "running"
                running[asyncio.ensure_future(self._run_step(step))] = step.id
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for future in sorted(done, key=lambda f: list(self.steps).index(running[f])):
                step_id = running.pop(future)
                await self._settle(step_id, future)
        failed = [n for n, s in self.status.items() if s == "failed"]
        await self._publish()
        if self.cancelled and not failed:
            raise self.cancelled[0]
        if failed:
            step_id = failed[0]
            raise FlowStall(
                "step_failed",
                f"workflow step {step_id!r} failed and no editor repaired it",
                step=step_id,
                observed={"error": self.errors.get(step_id), "workflow": self.summary()},
                handle=self.handles.get(step_id, ""),
            )
        sinks = [
            step_id
            for step_id in self.steps
            if self.status[step_id] == "completed"
            and not any(step_id in other.after for other in self.steps.values())
        ]

        # The caller reads the answer: the final steps first, then every earlier step's
        # answer, each under its step id, so nothing the workflow found is left out.
        def section(step_id: str) -> str:
            result = self.results[step_id]
            model = str(getattr(result, "model", "") or "")
            label = f"{step_id}, on {model}" if model else step_id
            return f"[{label}]\n{str(getattr(result, 'response_text', '')).strip()}"

        earlier = [n for n in self.steps if n in self.results and n not in sinks]
        earlier += [label for label in self.forks if label in self.results]
        answer = "\n\n".join(section(n) for n in sinks)
        if earlier:
            answer += "\n\nEarlier steps:\n\n" + "\n\n".join(section(n) for n in earlier)
        return {
            "answer": answer,
            "status": "completed",
            "results": {
                step_id: getattr(result, "answer", result)
                for step_id, result in self.results.items()
            },
            "workflow": self.summary(),
            **(
                {"workflow_state": dict(self.ctx.workflow_state)} if self.ctx.workflow_state else {}
            ),
        }

    async def _run_step(self, step: Step) -> Any:
        payload = self._input(step)
        if step.target in self.editors:
            payload = {
                "assignment": payload,
                "workflow": self.summary(),
                "editing": (
                    "You are an editor of this running workflow. Only a workflow_add_step or "
                    "workflow_cancel_step call changes it; a step you describe in your answer "
                    "is not added."
                    + (
                        f" At most {self.max_added_steps - self.added} more steps may be added."
                        if self.max_added_steps is not None
                        else ""
                    )
                ),
            }
        # A flow step's options select its configuration (an arena's contestants, ...).
        flows = {row["id"] for row in getattr(self.ctx.resolution, "flows", None) or ()}
        if step.options and step.target not in flows:
            raise FlowTargetUnknown(
                f"step {step.id!r}: options apply to a flow target, not {step.target!r}",
                target=step.target,
            )
        target = Flow(step.target, **step.options) if step.options else step.target
        call = self.ctx.call(target, payload, key=f"step:{step.id}", model=step.model)
        call.label = step.id
        call.workflow_step = {"run": self.ctx.run_id, "step": step.id}
        if self.folder:
            from clio_agent.gact.node_folders import step_folder  # noqa: PLC0415

            call.workflow_step["folder"] = step_folder(self.folder, step.id)
        if step.target in self.editors:
            call.editor = {"run": self.ctx.run_id, "step": step.id}
        self.invocations[step.id] = call.invocation
        await self._publish()
        return await call

    async def _settle(self, step_id: str, future: asyncio.Future[Any]) -> None:
        step = self.steps[step_id]
        try:
            result = future.result()
        except FlowCheckpointLimit:
            raise  # The run cannot record more work: it ends, it does not fail a step.
        except CallCancelled as outcome:
            # A cancelled task is not a failure an editor could repair: the step is
            # cancelled, its dependents are skipped, and the run ends cancelled.
            self.status[step_id] = "cancelled"
            self.handles[step_id] = outcome.handle
            self.cancelled.append(outcome)
            await self.ctx.record_event(
                {"event": "step_cancelled", "step": step_id, "handle": outcome.handle}
            )
            await self._publish()
            return
        except FlowOutcome as outcome:
            self.status[step_id] = "failed"
            self.handles[step_id] = outcome.handle
            self.errors[step_id] = outcome.to_dict()
            await self.ctx.record_event(
                {"event": "step_failed", "step": step_id, **outcome.to_dict()}
            )
            await self._publish()
            await self._offer_stall(step_id, outcome)
            return
        self.status[step_id] = "completed"
        self.results[step_id] = result
        self.handles[step_id] = getattr(result, "handle", "")
        await self.ctx.merge_results([result])
        await self.ctx.record_event(
            {"event": "step_completed", "step": step_id, "handle": self.handles[step_id]}
        )
        await self._settle_forks(step_id, result)
        if step.target in self.editors:
            await self._apply_edits(step_id, result)
        await self._publish()

    async def _settle_forks(self, step_id: str, result: Any) -> None:
        """Record the step's forks, which its wait covered; offer a failed one to the editors."""

        for number, row in enumerate(getattr(result, "forks", None) or [], 1):
            label = f"{step_id}~{number}"
            status = str(row.get("status") or ("failed" if row.get("error") else ""))
            self.forks[label] = {
                "fork_of": step_id,
                "handle": row.get("handle", ""),
                "status": status,
            }
            if status == "completed":
                self.results[label] = CallResult(
                    row.get("result"), handle=row.get("handle", ""), target=label, kind="agent"
                )
                await self.ctx.record_event(
                    {"event": "fork_completed", "step": label, "fork_of": step_id}
                )
                continue
            failure = CallFailed(
                f"fork {label!r} ended {status or 'without a result'}",
                handle=row.get("handle", ""),
                status=status,
                error=str(row.get("error") or ""),
            )
            self.errors[label] = failure.to_dict()
            await self.ctx.record_event(
                {"event": "fork_failed", "step": label, "fork_of": step_id, **failure.to_dict()}
            )
            await self._offer_stall(label, failure, target=self.steps[step_id].target)

    async def _offer_stall(self, step_id: str, outcome: FlowOutcome, *, target: str = "") -> None:
        """Offer a failed step (or fork) to the editors, who may add a repair step."""

        if not self.editors or self._rounds_spent():
            return
        for editor in sorted(self.editors):
            if self._rounds_spent():
                return
            stall = {
                "stall": "step_failed",
                "step": step_id,
                "target": target or self.steps[step_id].target,
                "error": outcome.to_dict(),
            }
            payload = {
                "stall": stall,
                "workflow": self.summary(),
                "editing": "Propose a repair step with workflow_add_step, or none if it cannot be repaired.",
            }
            call = self.ctx.call(editor, payload, key=f"repair:{step_id}:{editor}")
            call.editor = {"run": self.ctx.run_id, "step": f"repair:{step_id}"}
            try:
                result = await call
            except FlowOutcome as failure:
                await self.ctx.record_event(
                    {
                        "event": "repair_failed",
                        "step": step_id,
                        "editor": editor,
                        **failure.to_dict(),
                    }
                )
                continue
            if await self._apply_edits(f"repair:{step_id}", result, repairing=step_id):
                return

    async def _apply_edits(self, origin: str, result: Any, *, repairing: str = "") -> bool:
        """Apply an editor's proposals within bounds; returns whether any applied."""

        proposals = [
            row
            for row in (result.outputs.get("tools_called") or [])
            if isinstance(row, dict) and str(row.get("tool") or row.get("name")) in _EDIT_TOOLS
        ]
        if not proposals:
            return False
        self.rounds += 1
        applied = False
        for index, row in enumerate(proposals):
            name = str(row.get("tool") or row.get("name"))
            args = row.get("args") or row.get("arguments") or {}
            if not isinstance(args, dict):
                continue
            if name == "workflow_cancel_step":
                applied |= await self._cancel(str(args.get("step_id", "")), origin)
                continue
            applied |= await self._add(args, origin, index, repairing=repairing)
        return applied

    async def _add(self, args: dict[str, Any], origin: str, index: int, *, repairing: str) -> bool:
        step_id = str(args.get("step_id") or args.get("id") or "")
        target = str(args.get("target") or "")
        after = [str(a) for a in args.get("after") or ()]
        base = origin.removeprefix("repair:")
        # A step an editor adds exists only once that step has finished, so it follows
        # that step (and reads its answer). A repair stands in for the failed step instead.
        if not repairing and base in self.steps and base not in after:
            after.append(base)
        elif not after and base in self.steps:
            after = [base]
        if repairing and repairing in after:
            after = [a for a in after if a != repairing]
        options = args.get("options") or None
        reason = ""
        # A fork counts as an added step against the same bound.
        if (
            self.max_added_steps is not None
            and self.added + len(self.forks) >= self.max_added_steps
        ):
            reason = "max_added_reached"
        elif self.allowed is not None and target not in self.allowed:
            reason = "target_not_allowed"
        elif step_id in self.steps:
            reason = "step_exists"
        else:
            try:
                proposed = Step(step_id, target, args.get("input"), after, options=options)
                Workflow(steps=[*self.steps.values(), proposed])
            except WorkflowError as exc:
                reason = f"invalid: {exc}"
        event = {
            "event": "step_proposed",
            "step": step_id,
            "target": target,
            "after": after,
            "by": origin,
        }
        if reason:
            await self.ctx.record_event({**event, "event": "step_rejected", "reason": reason})
            return False
        await self.ctx.record_event(event)
        if self.approve:
            self.awaiting[step_id] = {
                "id": step_id,
                "target": target,
                "after": after,
                "by": origin,
                "reason": str(args.get("reason") or ""),
            }
            await self._publish()
            approved = await self.ctx.ask(
                f"Add workflow step {step_id!r} ({target}) after {', '.join(after) or 'the start'}, "
                f"proposed by {origin}?",
                schema=bool,
                reason=str(args.get("reason") or ""),
                key=f"approve:{origin}:{index}:{step_id}",
            )
            self.awaiting.pop(step_id, None)
            if not approved:
                await self.ctx.record_event(
                    {**event, "event": "step_rejected", "reason": "not_approved"}
                )
                return False
        self.steps[step_id] = Step(
            step_id, target, args.get("input"), after, str(args.get("reason") or ""), options
        )
        self.status[step_id] = "pending"
        self.added += 1
        if repairing:
            # The repair stands in for the failed step: its dependents wait on the repair.
            for other_id, other in list(self.steps.items()):
                if repairing in other.after and self.status[other_id] == "pending":
                    self.steps[other_id] = Step(
                        other.id,
                        other.target,
                        other.input,
                        [step_id if a == repairing else a for a in other.after],
                        other.description,
                        other.options,
                    )
            self.status[repairing] = "repaired"
        # `approved` records that the person was asked and said yes, so a client can show it.
        await self.ctx.record_event({**event, "event": "step_added", "approved": self.approve})
        return True

    async def _cancel(self, step_id: str, origin: str) -> bool:
        if self.status.get(step_id) != "pending":
            await self.ctx.record_event(
                {"event": "cancel_rejected", "step": step_id, "by": origin, "reason": "not_pending"}
            )
            return False
        self.status[step_id] = "cancelled"
        await self.ctx.record_event({"event": "step_cancelled", "step": step_id, "by": origin})
        await self._publish()
        return True
