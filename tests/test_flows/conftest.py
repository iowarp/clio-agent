"""Flows on the real task layer, with a scripted program standing in for the model.

``flow_app`` builds a GACT app whose default marketplace is the hermetic fixture
plus a ``flowtest`` plugin of test flows and agents that select them. The turn's
agent module is the real flow runner (``run_agent_flow``) over the pinned
resolution; only the ReAct program (the model) is scripted: ``program(question)``
returns a prediction, so a flow's children run as real child assignments.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._harness import _commit_test_model_input
from tests._plugin_fixtures import custom_marketplace

FLOWS = """
from clio_agent.sdk import Flow


class Fanout(Flow):
    id = "fanout"

    async def forward(self, task, ctx):
        calls = [ctx.call("self", f"part {i} of {task}") for i in range(3)]
        results = await ctx.gather(calls, limit=2)
        return {"answer": " | ".join(r.answer for r in results), "handles": [r.handle for r in results]}


class Tolerant(Flow):
    id = "tolerant"

    async def forward(self, task, ctx):
        calls = [ctx.call("self", "ok one"), ctx.call("self", "FAIL two"), ctx.call("self", "ok three")]
        results = await ctx.gather(calls, return_exceptions=True)
        return {"answer": ",".join(type(r).__name__ for r in results)}


class Yielder(Flow):
    id = "yielder"

    async def forward(self, task, ctx):
        from clio_agent.flows.outcomes import CallAwaitingInput

        try:
            await ctx.call("self", "YIELD for input")
        except CallAwaitingInput as waiting:
            return f"{type(waiting).__name__}: {waiting.answer}"
        return "the child completed"


class Stopper(Flow):
    id = "stopper"

    async def forward(self, task, ctx):
        from clio_agent.flows.outcomes import CallStopped

        try:
            await ctx.call("self", "STALL until stopped")
        except CallStopped as stopped:
            return f"{type(stopped).__name__}: {stopped.reason}"
        return "the child completed"


class Strict(Flow):
    id = "strict"

    async def forward(self, task, ctx):
        await ctx.gather([ctx.call("self", "ok one"), ctx.call("self", "FAIL two")])
        return "unreachable"


class Asker(Flow):
    id = "asker"

    async def forward(self, task, ctx):
        if await ctx.ask("Proceed with the run?", schema=bool):
            return (await ctx.call("self", "approved work")).answer
        return "declined"


class Breaker(Flow):
    id = "breaker"

    async def forward(self, task, ctx):
        await ctx.step(Breaker.parse, "first")
        return "unreachable"

    @staticmethod
    def parse(value):
        raise ValueError(f"could not parse the reply to {value}")


class Stepper(Flow):
    id = "stepper"
    calls = []

    async def forward(self, task, ctx):
        first = await ctx.step(Stepper.record, "a")
        second = await ctx.step(Stepper.record, "b")
        return f"{first}{second}"

    @staticmethod
    def record(value):
        Stepper.calls.append(value)
        return value.upper()


WORKFLOW = {
    "steps": [
        {"id": "survey", "target": "self", "input": "$task"},
        {"id": "left", "target": "self", "after": ["survey"], "description": "left branch"},
        {"id": "right", "target": "self", "after": ["survey"], "description": "right branch"},
        {"id": "merge", "target": "self", "after": ["left", "right"]},
    ]
}


class WorkflowRunner(Flow):
    id = "workflowrunner"

    async def forward(self, task, ctx):
        result = await ctx.call("workflow", {"workflow": WORKFLOW, "task": task})
        return {"answer": result.answer, "workflow": result.outputs.get("workflow")}


class EditedWorkflow(Flow):
    id = "editedworkflow"

    async def forward(self, task, ctx):
        spec = {
            "workflow": {"steps": [{"id": "draft", "target": "self"}, {"id": "critique", "target": "critic", "after": ["draft"]}]},
            "task": task,
            "editors": ["critic"],
            "max_added_steps": 1,
            "approve_edits": "approve" in task,
        }
        result = await ctx.call("workflow", spec)
        return {"answer": result.answer, "workflow": result.outputs.get("workflow")}


class Nester(Flow):
    id = "nester"
    max_depth = 1

    async def forward(self, task, ctx):
        try:
            result = await ctx.call("nester", "deeper")
        except Exception as exc:
            return f"{type(exc).__name__}:{getattr(exc, 'reason', '')}:{ctx.depth}"
        return "child:" + result.answer


class Greedy(Flow):
    id = "greedy"
    max_model_calls = 2

    async def forward(self, task, ctx):
        for i in range(3):
            await ctx.call("self", f"call {i}")
        return "unreachable"


class Budgeted(Flow):
    id = "budgeted"
    budget_tokens = 10
    max_concurrency = 1

    async def forward(self, task, ctx):
        target = "budget-worker" if task == "nested" else "self"
        calls = [ctx.call(target, f"budgeted part {i}") for i in range(3)]
        results = await ctx.gather(calls, return_exceptions=True)
        return ",".join(type(result).__name__ for result in results)


class BudgetWorker(Flow):
    id = "budget-worker"

    async def forward(self, task, ctx):
        return (await ctx.call("self", task)).answer
"""

SUBAGENTS = """
from clio_agent.sdk import Subagent

critic = Subagent(
    "critic",
    prompt="Review the draft and propose any missing step.",
    plugins=["flowtest"],
    description="Reviews a draft workflow step",
)
"""

AGENTS = """
from clio_agent.sdk import Agent, Flow


def _agent(agent_id, flow):
    return type(
        agent_id.title(),
        (Agent,),
        {
            "id": agent_id,
            "version": "1.0.0",
            "role": "a flow test agent",
            "flow": flow,
            "plugins": Agent.plugins + ["workspace", "delegation", "flowtest"],
            "__module__": __name__,
        },
    )


Fanouter = _agent("fanouter", "fanout")
Tolerater = _agent("tolerater", "tolerant")
Yielderer = _agent("yielderer", "yielder")
Stopperer = _agent("stopperer", "stopper")
Stricter = _agent("stricter", "strict")
Askerer = _agent("askerer", "asker")
Stepperer = _agent("stepperer", "stepper")
Breakerer = _agent("breakerer", "breaker")
Greedier = _agent("greedier", Flow("greedy", max_model_calls=2))
Budgeter = _agent("budgeter", "budgeted")
Planner = _agent("planner", "workflowrunner")
Nesterer = _agent("nesterer", "nester")


class Orchestrator(Agent):
    id = "orchestrator"
    version = "1.0.0"
    role = "a flow test agent that starts flows"
    plugins = Agent.plugins + ["workspace", "delegation", "orchestration", "flowtest"]


Editor = _agent("editor", "editedworkflow")
"""


class FakeHost:
    """The base agent ``build_app`` needs; turns never call it (the module is scripted)."""

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover
        raise AssertionError("the scripted flow module runs instead of the host")


class ScriptedFlowModule:
    """The agent module: the real flow runner over the resolution, a scripted program."""

    def __init__(self, agent_def: Any, resolution: Any, program: Callable[..., Any]) -> None:
        self.agent_def = agent_def
        self.resolution = resolution
        self.tools: list[Any] = []
        self.program = program

    def __call__(self, question: str, session_id: str, **kwargs: Any) -> Any:
        from clio_agent.flows.runner import accept_flow_input, flow_selection, run_agent_flow
        from clio_agent.gact import context

        # As in production: a non-react flow confirms its input in the flow runner;
        # under react, the scripted program stands in for ReAct's own confirmation.
        if flow_selection(context.active_app(), session_id, self.agent_def).get("id") == "react":
            _commit_test_model_input(self.agent_def, question, session_id)
        else:
            # The production module's forward sets the react scope before its flow runs.
            tokens = [
                context.set_react_scope(self.agent_def.id),
                context.set_react_session(session_id),
            ]
            try:
                accept_flow_input(self, question, session_id)
            finally:
                for token in reversed(tokens):
                    context.reset(token)
        prediction = run_agent_flow(self, {"question": question, "session_id": session_id})
        return dspy.Prediction(
            answer=str(getattr(prediction, "answer", "")),
            workflow_state=getattr(prediction, "workflow_state", ""),
            tools_called=list(getattr(prediction, "tools_called", None) or []),
            selected_expert=self.agent_def.id,
            assignment_disposition=getattr(prediction, "assignment_disposition", None),
        )


def default_program(question: str = "", **_: Any) -> Any:
    if "FAIL" in question:
        raise RuntimeError("scripted failure")
    if "STALL" in question:
        # Works until its turn is asked to stop, as a model call does when Stop or the
        # no-progress bound ends it.
        from clio_agent.agent import cancellation_requested
        from clio_agent.gact.agents.clio_react import _turn_cancelled

        deadline = time.monotonic() + 60
        while not cancellation_requested():
            if time.monotonic() > deadline:
                raise RuntimeError("the test never stopped the child")
            time.sleep(0.05)
        raise _turn_cancelled()
    if "YIELD" in question:
        # The child keeps its assignment open for a follow-up nobody will send.
        return dspy.Prediction(answer="need approval to continue", assignment_disposition="yield")
    if "workflow_add_step" in question:
        # The critic edits the running workflow. Like a real ReAct run, the prediction
        # carries no tool trace: the call reaches the turn through the tool observer.
        from clio_agent.gact import context

        app = context.active_app()
        app.state.tool_call_ledger.setdefault(context.active_session_id(), []).append(
            {
                "name": "workflow_add_step",
                # Like the live reviewer, it names the step it builds on, not itself.
                "args": {
                    "step_id": "polish",
                    "target": "self",
                    "input": "polish $draft",
                    "after": ["draft"],
                },
                "ok": True,
            }
        )
        return dspy.Prediction(answer="critique: add a polish step")
    return dspy.Prediction(answer=f"done: {question.splitlines()[0][:60]}")


@pytest.fixture
def flow_app(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # The default workspace is the working directory: flows that record files there
    # (the arena's votes) write into the test's folder, not the checkout.
    monkeypatch.chdir(tmp_path)
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "flowtest": {
                "files": {
                    "ai.iowarp.clio/flows/test_flows.py": FLOWS,
                    "ai.iowarp.clio/subagents/critic.py": SUBAGENTS,
                },
                "ext": {"requires": ["delegation"]},
            }
        },
        agents={"flow_agents.py": AGENTS},
    )
    from clio_agent.gact.agents import builders

    program: dict[str, Callable[..., Any]] = {"run": default_program}

    def scripted(**kwargs: Any) -> Any:
        # A child sees its assignment as delegated context, not as the turn question;
        # the scripted "model" answers the assignment it was given.
        from clio_agent.gact import context

        app = context.active_app()
        assignment = app.state.task_history.latest_assignment(context.active_session_id())
        if assignment is not None and assignment.owner.parent_handle is not None:
            kwargs["question"] = assignment.description
        return program["run"](**kwargs)

    modules: dict[str, ScriptedFlowModule] = {}

    def build(base_agent: Any, agent_def: Any, resolution: Any = None) -> ScriptedFlowModule:
        from clio_agent.gact import context

        module = ScriptedFlowModule(agent_def, resolution, scripted)
        modules[context.active_session_id()] = module
        return module

    monkeypatch.setattr(builders, "build_agent_module", build)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeHost())
    app.state.flow_program = program
    app.state.flow_modules = modules
    with TestClient(app) as client:
        yield client


def new_session(client: TestClient, agent_ref: str) -> str:
    response = client.post(
        "/v1/sessions", json={"title": agent_ref, "metadata": {"agent_ref": agent_ref}}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def post(client: TestClient, sid: str, text: str) -> None:
    ack = client.post(
        f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": text}]}
    )
    assert ack.status_code == 202, ack.text


def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 60.0, interval: float = 0.05
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError("condition not reached")


def assistant_messages(client: TestClient, sid: str) -> list[dict[str, Any]]:
    rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    return [m for m in rows if m["role"] == "assistant" and not m.get("metadata", {}).get("live")]


def text_of(message: dict[str, Any]) -> str:
    return "".join(p.get("text", "") for p in message.get("parts", []) if p.get("type") == "text")
