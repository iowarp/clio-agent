"""The ``plan`` flow: run a plan's nodes as tasks, as soon as each node's inputs finish.

Input (from ``run_plan`` or ``ctx.call("plan", {...})``)::

    {"plan": {"nodes": [...]}, "task": "...", "editors": ["critic"],
     "max_added": 6, "max_rounds": 3, "allowed_targets": [...], "approve_edits": true}

A node whose target is a named editor runs with ``plan_add_node`` and
``plan_cancel_node``; its proposals are read from its own settled result, so a
replay applies exactly the same edits. Growth is bounded by ``max_added`` nodes,
``max_rounds`` editing rounds and ``allowed_targets``. With ``approve_edits`` each
proposal waits on a Question; a proposed node is recorded as pending until then.
A failed node is first offered to the editors as a typed stall (they may add a
repair node within the same bounds); if nobody edits, the stall returns to the
caller.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from clio_agent.flows.outcomes import (
    CallCancelled,
    FlowCheckpointLimit,
    FlowOutcome,
    FlowStall,
    FlowTargetUnknown,
)
from clio_agent.sdk import Flow, Node, Plan, PlanError

_EDIT_TOOLS = ("plan_add_node", "plan_cancel_node")


_REFERENCE = re.compile(r"\$([A-Za-z0-9_][A-Za-z0-9_.-]*)")


def _substitute(value: Any, task: Any, results: dict[str, Any]) -> Any:
    """Replace ``$task`` and ``$<node>`` references: a whole-string reference keeps the
    value itself; a reference inside text becomes the text of that value."""

    if isinstance(value, str):

        def resolve(name: str) -> Any:
            if name == "task":
                return task
            result = results[name]
            return getattr(result, "answer", result)

        def known(name: str) -> bool:
            return name == "task" or name in results

        whole = _REFERENCE.fullmatch(value)
        if whole and known(whole.group(1)):
            return resolve(whole.group(1))
        return _REFERENCE.sub(
            lambda m: _text(resolve(m.group(1))) if known(m.group(1)) else m.group(0), value
        )
    if isinstance(value, dict):
        return {k: _substitute(v, task, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, task, results) for v in value]
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


class PlanFlow(Flow):
    """Run a plan; editors may grow it within bounds, with optional approval."""

    id = "plan"
    max_concurrency = 4

    async def forward(self, task: Any, ctx: Any) -> Any:
        spec = task if isinstance(task, dict) else {"plan": task}
        try:
            plan = Plan.from_dict(spec.get("plan"))
        except PlanError as exc:
            raise FlowStall("plan_invalid", str(exc), observed=spec.get("plan")) from exc
        run = _PlanRun(ctx, plan, spec)
        return await run.execute()


class _PlanRun:
    def __init__(self, ctx: Any, plan: Plan, spec: dict[str, Any]) -> None:
        self.ctx = ctx
        self.task = spec.get("task", "")
        self.nodes: dict[str, Node] = {node.id: node for node in plan.nodes}
        self.editors = {str(e) for e in spec.get("editors") or ()}
        self.max_added = int(spec.get("max_added", 6))
        self.max_rounds = int(spec.get("max_rounds", 3))
        allowed = spec.get("allowed_targets")
        self.allowed = {str(t) for t in allowed} if allowed is not None else None
        self.approve = bool(spec.get("approve_edits", False))
        self.status: dict[str, str] = dict.fromkeys(self.nodes, "pending")
        self.results: dict[str, Any] = {}
        self.errors: dict[str, dict[str, Any]] = {}
        self.cancelled: list[CallCancelled] = []
        self.added = 0
        self.rounds = 0
        self.handles: dict[str, str] = {}
        self.invocations: dict[str, str] = {}
        self.awaiting: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ helpers
    def _ready(self) -> list[Node]:
        return [
            node
            for node_id, node in self.nodes.items()
            if self.status[node_id] == "pending"
            and all(self.status.get(dep) == "completed" for dep in node.after)
        ]

    def _blocked(self) -> list[str]:
        """Pending nodes that can never run: a dependency failed or was cancelled."""

        return [
            node_id
            for node_id, node in self.nodes.items()
            if self.status[node_id] == "pending"
            and any(self.status.get(dep) in {"failed", "cancelled"} for dep in node.after)
        ]

    def _input(self, node: Node) -> Any:
        """A node's input: its own, or the task plus its instructions and dependencies' results."""

        if node.input is not None:
            return _substitute(node.input, self.task, self.results)
        if not node.after and not node.description:
            return self.task
        payload: dict[str, Any] = {"task": self.task}
        if node.description:
            payload["instructions"] = node.description
        if node.after:
            payload["inputs"] = {dep: self._dependency(dep) for dep in node.after}
        return payload

    def _dependency(self, node_id: str) -> Any:
        result = self.results.get(node_id)
        answer = getattr(result, "answer", "")
        state = getattr(result, "workflow_state", {}) if result is not None else {}
        return {"answer": answer, "workflow_state": state} if state else answer

    def summary(self) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    **node.to_dict(),
                    "status": self.status[node.id],
                    "handle": self.handles.get(node.id, ""),
                }
                for node in self.nodes.values()
            ],
            "added": self.added,
            "rounds": self.rounds,
        }

    async def _publish(self) -> None:
        """Publish the live plan: nodes with status, and proposals awaiting approval."""

        summary = self.summary()
        for row in summary["nodes"]:
            row["invocation"] = self.invocations.get(row["id"], "")
        await self.ctx.publish(
            {
                "kind": "plan",
                **summary,
                "awaiting_approval": list(self.awaiting.values()),
                "editors": sorted(self.editors),
                "max_added": self.max_added,
                "max_rounds": self.max_rounds,
                "approve_edits": self.approve,
            }
        )

    # -------------------------------------------------------------- run
    async def execute(self) -> dict[str, Any]:
        running: dict[asyncio.Future[Any], str] = {}
        await self._publish()
        while True:
            while blocked := self._blocked():
                # A skipped node blocks its own dependents in turn.
                for node_id in blocked:
                    self.status[node_id] = "cancelled"
                    await self.ctx.record_event({"event": "node_skipped", "node": node_id})
            for node in self._ready():
                self.status[node.id] = "running"
                running[asyncio.ensure_future(self._run_node(node))] = node.id
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for future in sorted(done, key=lambda f: list(self.nodes).index(running[f])):
                node_id = running.pop(future)
                await self._settle(node_id, future)
        failed = [n for n, s in self.status.items() if s == "failed"]
        await self._publish()
        if self.cancelled and not failed:
            raise self.cancelled[0]
        if failed:
            node_id = failed[0]
            raise FlowStall(
                "node_failed",
                f"plan node {node_id!r} failed and no editor repaired it",
                step=node_id,
                observed={"error": self.errors.get(node_id), "plan": self.summary()},
                handle=self.handles.get(node_id, ""),
            )
        sinks = [
            node_id
            for node_id in self.nodes
            if self.status[node_id] == "completed"
            and not any(node_id in other.after for other in self.nodes.values())
        ]

        # The caller reads the answer: the final steps first, then every earlier step's
        # answer, each under its node id, so nothing the plan found is left out.
        def section(node_id: str) -> str:
            return f"[{node_id}]\n{str(getattr(self.results[node_id], 'answer', '')).strip()}"

        earlier = [n for n in self.nodes if n in self.results and n not in sinks]
        answer = "\n\n".join(section(n) for n in sinks)
        if earlier:
            answer += "\n\nEarlier steps:\n\n" + "\n\n".join(section(n) for n in earlier)
        return {
            "answer": answer,
            "status": "completed",
            "results": {
                node_id: getattr(result, "answer", result)
                for node_id, result in self.results.items()
            },
            "plan": self.summary(),
            **(
                {"workflow_state": dict(self.ctx.workflow_state)} if self.ctx.workflow_state else {}
            ),
        }

    async def _run_node(self, node: Node) -> Any:
        payload = self._input(node)
        if node.target in self.editors:
            payload = {
                "assignment": payload,
                "plan": self.summary(),
                "editing": (
                    "You are an editor of this running plan. Only a plan_add_node or "
                    "plan_cancel_node call changes it; a step you describe in your answer "
                    f"is not added. At most {self.max_added - self.added} more nodes may "
                    "be added."
                ),
            }
        # A flow node's options select its configuration (an arena's contestants, ...).
        flows = {row["id"] for row in getattr(self.ctx.resolution, "flows", None) or ()}
        if node.options and node.target not in flows:
            raise FlowTargetUnknown(
                f"node {node.id!r}: options apply to a flow target, not {node.target!r}",
                target=node.target,
            )
        target = Flow(node.target, **node.options) if node.options else node.target
        call = self.ctx.call(target, payload, key=f"node:{node.id}")
        if node.target in self.editors:
            call.editor = {"run": self.ctx.run_id, "node": node.id}
        self.invocations[node.id] = call.invocation
        await self._publish()
        return await call

    async def _settle(self, node_id: str, future: asyncio.Future[Any]) -> None:
        node = self.nodes[node_id]
        try:
            result = future.result()
        except FlowCheckpointLimit:
            raise  # The run cannot record more work: it ends, it does not fail a node.
        except CallCancelled as outcome:
            # A cancelled task is not a failure an editor could repair: the node is
            # cancelled, its dependents are skipped, and the run ends cancelled.
            self.status[node_id] = "cancelled"
            self.handles[node_id] = outcome.handle
            self.cancelled.append(outcome)
            await self.ctx.record_event(
                {"event": "node_cancelled", "node": node_id, "handle": outcome.handle}
            )
            await self._publish()
            return
        except FlowOutcome as outcome:
            self.status[node_id] = "failed"
            self.handles[node_id] = outcome.handle
            self.errors[node_id] = outcome.to_dict()
            await self.ctx.record_event(
                {"event": "node_failed", "node": node_id, **outcome.to_dict()}
            )
            await self._publish()
            await self._offer_stall(node_id, outcome)
            return
        self.status[node_id] = "completed"
        self.results[node_id] = result
        self.handles[node_id] = getattr(result, "handle", "")
        await self.ctx.merge_results([result])
        await self.ctx.record_event(
            {"event": "node_completed", "node": node_id, "handle": self.handles[node_id]}
        )
        if node.target in self.editors:
            await self._apply_edits(node_id, result)
        await self._publish()

    async def _offer_stall(self, node_id: str, outcome: FlowOutcome) -> None:
        """Offer a failed node to the editors, who may add a repair node."""

        if not self.editors or self.rounds >= self.max_rounds:
            return
        for editor in sorted(self.editors):
            if self.rounds >= self.max_rounds:
                return
            stall = {
                "stall": "node_failed",
                "step": node_id,
                "target": self.nodes[node_id].target,
                "error": outcome.to_dict(),
            }
            payload = {
                "stall": stall,
                "plan": self.summary(),
                "editing": "Propose a repair node with plan_add_node, or none if it cannot be repaired.",
            }
            call = self.ctx.call(editor, payload, key=f"repair:{node_id}:{editor}")
            call.editor = {"run": self.ctx.run_id, "node": f"repair:{node_id}"}
            try:
                result = await call
            except FlowOutcome as failure:
                await self.ctx.record_event(
                    {
                        "event": "repair_failed",
                        "node": node_id,
                        "editor": editor,
                        **failure.to_dict(),
                    }
                )
                continue
            if await self._apply_edits(f"repair:{node_id}", result, repairing=node_id):
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
            if name == "plan_cancel_node":
                applied |= await self._cancel(str(args.get("node_id", "")), origin)
                continue
            applied |= await self._add(args, origin, index, repairing=repairing)
        return applied

    async def _add(self, args: dict[str, Any], origin: str, index: int, *, repairing: str) -> bool:
        node_id = str(args.get("node_id") or args.get("id") or "")
        target = str(args.get("target") or "")
        after = [str(a) for a in args.get("after") or ()]
        base = origin.removeprefix("repair:")
        # A node an editor step adds exists only once that step has finished, so it follows
        # that step (and reads its answer). A repair stands in for the failed node instead.
        if not repairing and base in self.nodes and base not in after:
            after.append(base)
        elif not after and base in self.nodes:
            after = [base]
        if repairing and repairing in after:
            after = [a for a in after if a != repairing]
        options = args.get("options") or None
        reason = ""
        if self.added >= self.max_added:
            reason = "max_added_reached"
        elif self.allowed is not None and target not in self.allowed:
            reason = "target_not_allowed"
        elif node_id in self.nodes:
            reason = "node_exists"
        else:
            try:
                proposed = Node(node_id, target, args.get("input"), after, options=options)
                Plan(nodes=[*self.nodes.values(), proposed])
            except PlanError as exc:
                reason = f"invalid: {exc}"
        event = {
            "event": "node_proposed",
            "node": node_id,
            "target": target,
            "after": after,
            "by": origin,
        }
        if reason:
            await self.ctx.record_event({**event, "event": "node_rejected", "reason": reason})
            return False
        await self.ctx.record_event(event)
        if self.approve:
            self.awaiting[node_id] = {
                "id": node_id,
                "target": target,
                "after": after,
                "by": origin,
                "reason": str(args.get("reason") or ""),
            }
            await self._publish()
            approved = await self.ctx.ask(
                f"Add plan node {node_id!r} ({target}) after {', '.join(after) or 'the start'}, "
                f"proposed by {origin}?",
                schema=bool,
                reason=str(args.get("reason") or ""),
                key=f"approve:{origin}:{index}:{node_id}",
            )
            self.awaiting.pop(node_id, None)
            if not approved:
                await self.ctx.record_event(
                    {**event, "event": "node_rejected", "reason": "not_approved"}
                )
                return False
        self.nodes[node_id] = Node(
            node_id, target, args.get("input"), after, str(args.get("reason") or ""), options
        )
        self.status[node_id] = "pending"
        self.added += 1
        if repairing:
            # The repair stands in for the failed node: its dependents wait on the repair.
            for other_id, other in list(self.nodes.items()):
                if repairing in other.after and self.status[other_id] == "pending":
                    self.nodes[other_id] = Node(
                        other.id,
                        other.target,
                        other.input,
                        [node_id if a == repairing else a for a in other.after],
                        other.description,
                        other.options,
                    )
            self.status[repairing] = "repaired"
        await self.ctx.record_event({**event, "event": "node_added"})
        return True

    async def _cancel(self, node_id: str, origin: str) -> bool:
        if self.status.get(node_id) != "pending":
            await self.ctx.record_event(
                {"event": "cancel_rejected", "node": node_id, "by": origin, "reason": "not_pending"}
            )
            return False
        self.status[node_id] = "cancelled"
        await self.ctx.record_event({"event": "node_cancelled", "node": node_id, "by": origin})
        await self._publish()
        return True
