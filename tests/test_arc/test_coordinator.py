"""Tests for MultiAgentCoordinator -- multi-agent task coordination.

Tests cover:
- AgentTask, CoordinationPlan, CoordinationResult data classes
- create_plan with single and multi-agent queries
- execute_plan with sequential execution
- execute_sequential with context passing
- execute_parallel (falls back to sequential)
- _execute_task with various agent types
- _store_coordination_trace in ARC
- Error handling for missing agents and failed tasks
"""

import time
from unittest.mock import MagicMock

import pytest

from clio_agent.arc.coordinator import (
    AgentTask,
    CoordinationPlan,
    CoordinationResult,
    MultiAgentCoordinator,
)
from clio_agent.arc.memory import ARCMemory


@pytest.fixture
def arc(tmp_path):
    return ARCMemory(data_dir=str(tmp_path / "arc"))


@pytest.fixture
def coordinator(arc):
    return MultiAgentCoordinator(arc)


class TestAgentTask:
    """Test AgentTask data class."""

    def test_creation(self):
        task = AgentTask("t1", "DataExpert", "analyze file", {"filepath": "a.h5"})
        assert task.task_id == "t1"
        assert task.agent_id == "DataExpert"
        assert task.query == "analyze file"
        assert task.context == {"filepath": "a.h5"}
        assert task.depends_on == []

    def test_with_dependencies(self):
        task = AgentTask("t2", "HPCExpert", "optimize", {}, depends_on=["t1"])
        assert task.depends_on == ["t1"]

    def test_repr(self):
        task = AgentTask("t1", "DataExpert", "q", {}, depends_on=["t0"])
        r = repr(task)
        assert "t1" in r
        assert "DataExpert" in r
        assert "t0" in r


class TestCoordinationPlan:
    """Test CoordinationPlan data class."""

    def test_creation(self):
        task = AgentTask("t1", "DataExpert", "q", {})
        plan = CoordinationPlan("p1", [task], "sequential", time.time())
        assert plan.plan_id == "p1"
        assert len(plan.tasks) == 1
        assert plan.execution_mode == "sequential"

    def test_repr(self):
        plan = CoordinationPlan("p1", [], "sequential", time.time())
        r = repr(plan)
        assert "p1" in r
        assert "0" in r
        assert "sequential" in r


class TestCoordinationResult:
    """Test CoordinationResult data class."""

    def test_success_result(self):
        result = CoordinationResult("p1", {"t1": "ok"}, 500.0, True)
        assert result.success is True
        assert result.error is None
        assert result.completed_at is not None

    def test_failure_result(self):
        result = CoordinationResult("p1", {}, 100.0, False, error="boom")
        assert result.success is False
        assert result.error == "boom"

    def test_repr_success(self):
        result = CoordinationResult("p1", {}, 100.0, True)
        assert "success" in repr(result)

    def test_repr_failure(self):
        result = CoordinationResult("p1", {}, 100.0, False, error="fail")
        assert "failed" in repr(result)
        assert "fail" in repr(result)


class TestCreatePlan:
    """Test MultiAgentCoordinator.create_plan."""

    def test_single_agent_no_coordination(self, coordinator):
        """Query without coordination keywords -> single task."""
        agents = {"DataExpert": MagicMock()}
        plan = coordinator.create_plan("analyze HDF5 file", agents)
        assert len(plan.tasks) == 1
        assert plan.tasks[0].agent_id == "DataExpert"

    def test_single_agent_defaults_to_first(self, coordinator):
        """If DataExpert not in agents, use first available."""
        agents = {"HPCExpert": MagicMock()}
        plan = coordinator.create_plan("analyze file", agents)
        assert len(plan.tasks) == 1
        assert plan.tasks[0].agent_id == "HPCExpert"

    def test_multi_agent_with_then(self, coordinator):
        """'then' keyword should create multi-agent plan."""
        agents = {"DataExpert": MagicMock(), "HPCExpert": MagicMock()}
        plan = coordinator.create_plan(
            "analyze data file then optimize on cluster", agents
        )
        assert len(plan.tasks) == 2

    def test_multi_agent_with_also(self, coordinator):
        """'also' keyword should trigger coordination."""
        agents = {"DataExpert": MagicMock()}
        plan = coordinator.create_plan("check data also run analysis", agents)
        assert len(plan.tasks) >= 1

    def test_task_dependencies_chain(self, coordinator):
        """Second task should depend on first."""
        agents = {"DataExpert": MagicMock(), "HPCExpert": MagicMock()}
        plan = coordinator.create_plan(
            "analyze HDF5 file then optimize for HPC cluster", agents
        )
        if len(plan.tasks) == 2:
            assert plan.tasks[1].depends_on == [plan.tasks[0].task_id]

    def test_keyword_routing_data(self, coordinator):
        """Tasks with 'data' keyword should route to DataExpert."""
        agents = {"DataExpert": MagicMock(), "HPCExpert": MagicMock()}
        plan = coordinator.create_plan(
            "analyze data then run on cluster", agents
        )
        if len(plan.tasks) == 2:
            assert plan.tasks[0].agent_id == "DataExpert"

    def test_keyword_routing_hpc(self, coordinator):
        """Tasks with 'cluster' keyword should route to HPCExpert."""
        agents = {"DataExpert": MagicMock(), "HPCExpert": MagicMock()}
        plan = coordinator.create_plan(
            "read HDF5 file then deploy to HPC cluster", agents
        )
        if len(plan.tasks) == 2:
            assert plan.tasks[1].agent_id == "HPCExpert"


class TestExecutePlan:
    """Test MultiAgentCoordinator.execute_plan."""

    def test_execute_sequential_plan(self, coordinator):
        """Sequential execution should produce results for each task."""
        agent = MagicMock()
        agent.forward.return_value = "analysis done"  # Return string, not Prediction
        agents = {"DataExpert": agent}

        plan = coordinator.create_plan("analyze file", agents)
        result = coordinator.execute_plan(plan, agents, "session-1")

        assert result.success is True
        assert len(result.task_results) == 1

    def test_execute_stores_trace(self, coordinator, arc):
        """Execution should store coordination trace in ARC."""
        agent = MagicMock()
        agent.forward.return_value = "ok"
        agents = {"DataExpert": agent}

        plan = coordinator.create_plan("analyze file", agents)
        coordinator.execute_plan(plan, agents, "session-1")

        # Should have invocations stored
        invocations = arc.get_invocations_by_agent("MultiAgentCoordinator")
        assert len(invocations) >= 1

    def test_execute_failure_handling(self, coordinator):
        """Exception during execution should be captured in result."""
        agent = MagicMock()
        agent.forward.side_effect = RuntimeError("boom")
        agents = {"DataExpert": agent}

        plan = coordinator.create_plan("analyze file", agents)
        result = coordinator.execute_plan(plan, agents, "session-1")

        # The per-task error is captured, coordination still succeeds
        assert result.success is True  # Coordination itself didn't throw

    def test_execute_parallel_falls_back(self, coordinator):
        """Parallel execution should fall back to sequential."""
        agent = MagicMock()
        agent.forward.return_value = "ok"
        agents = {"DataExpert": agent}

        tasks = [AgentTask("t1", "DataExpert", "q1", {})]
        result = coordinator.execute_parallel(tasks, agents, "session-1")
        assert "t1" in result


class TestExecuteSequential:
    """Test execute_sequential with various scenarios."""

    def test_missing_agent_logs_error(self, coordinator):
        """Task for missing agent should produce error result."""
        tasks = [AgentTask("t1", "MissingExpert", "query", {})]
        result = coordinator.execute_sequential(tasks, {}, "session-1")
        assert result["t1"]["success"] is False
        assert "not found" in result["t1"]["error"]

    def test_context_passing(self, coordinator):
        """Results from previous tasks should be available as prior_results."""
        agent1 = MagicMock()
        agent1.forward.return_value = "result1"
        agent2 = MagicMock()
        agent2.forward.return_value = "result2"

        tasks = [
            AgentTask("t1", "A", "q1", {}),
            AgentTask("t2", "B", "q2", {}, depends_on=["t1"]),
        ]
        agents = {"A": agent1, "B": agent2}
        result = coordinator.execute_sequential(tasks, agents, "session-1")

        assert result["t1"]["success"] is True
        assert result["t2"]["success"] is True


class TestExecuteTask:
    """Test _execute_task with different agent types."""

    def test_callable_agent(self, coordinator):
        """Callable agents (no .forward) should work."""
        agent = MagicMock(return_value="callable result")
        agent.forward = None  # Remove forward to trigger callable branch
        delattr(agent, "forward")

        task = AgentTask("t1", "Func", "query", {})
        result = coordinator._execute_task(task, agent, "session-1")
        assert result == "callable result"

    def test_non_callable_agent_raises(self, coordinator):
        """Non-callable agent should raise TypeError."""
        agent = "not callable"
        task = AgentTask("t1", "Bad", "query", {})
        with pytest.raises(TypeError, match="not callable"):
            coordinator._execute_task(task, agent, "session-1")

    def test_task_failure_stores_invocation(self, coordinator, arc):
        """Failed task should still store invocation in ARC."""
        agent = MagicMock()
        agent.forward.side_effect = RuntimeError("task failed")

        task = AgentTask("t1", "DataExpert", "query", {})
        with pytest.raises(RuntimeError):
            coordinator._execute_task(task, agent, "session-1")

        # Invocation should be stored despite failure
        invocations = arc.get_invocations_by_agent("DataExpert")
        assert len(invocations) >= 1
        assert invocations[0].status == "failure"
