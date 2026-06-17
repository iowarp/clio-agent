"""Multi-agent coordination for complex queries

This module provides the MultiAgentCoordinator class which manages coordination
between multiple agents for complex queries. It handles:
    - Query analysis to determine multi-agent needs
    - Coordination plan creation
    - Sequential and parallel execution
    - Inter-agent communication tracking
    - Coordination trace storage in ARC

Performance Targets:
    - Sequential execution with context passing
    - Parallel execution support (future)
    - Full coordination trace in ARC

See docs/CLIO_AGENT_ARCHITECTURE.md for architecture details.
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Invocation


class AgentTask:
    """Task for an agent in coordination plan.

    Attributes:
        task_id: Unique task identifier
        agent_id: Agent identifier to execute task
        query: Query/task for the agent
        context: Additional context for execution
        depends_on: List of task IDs this task depends on

    Example:
        >>> task = AgentTask(
        ...     task_id="task-1",
        ...     agent_id="DataExpert",
        ...     query="Analyze HDF5 file structure",
        ...     context={"filepath": "/data/file.h5"},
        ...     depends_on=[]
        ... )
    """

    def __init__(
        self,
        task_id: str,
        agent_id: str,
        query: str,
        context: Dict[str, Any],
        depends_on: Optional[List[str]] = None,
    ):
        """Initialize agent task.

        Args:
            task_id: Unique task identifier
            agent_id: Agent identifier
            query: Task query
            context: Task context
            depends_on: Task dependencies (default: empty list)
        """
        self.task_id = task_id
        self.agent_id = agent_id
        self.query = query
        self.context = context
        self.depends_on = depends_on or []

    def __repr__(self) -> str:
        return f"AgentTask(id={self.task_id}, agent={self.agent_id}, deps={self.depends_on})"


class CoordinationPlan:
    """Plan for multi-agent coordination.

    Attributes:
        plan_id: Unique plan identifier
        tasks: List of agent tasks
        execution_mode: Execution mode ("sequential" or "parallel")
        created_at: Plan creation timestamp (Unix timestamp float)

    Example:
        >>> plan = CoordinationPlan(
        ...     plan_id="plan-123",
        ...     tasks=[task1, task2],
        ...     execution_mode="sequential",
        ...     created_at=1704067200.0
        ... )
    """

    def __init__(
        self,
        plan_id: str,
        tasks: List[AgentTask],
        execution_mode: str,
        created_at: float,
    ):
        """Initialize coordination plan.

        Args:
            plan_id: Unique plan identifier
            tasks: List of agent tasks
            execution_mode: "sequential" or "parallel"
            created_at: Creation timestamp (Unix timestamp float)
        """
        self.plan_id = plan_id
        self.tasks = tasks
        self.execution_mode = execution_mode
        self.created_at = created_at

    def __repr__(self) -> str:
        return f"CoordinationPlan(id={self.plan_id}, tasks={len(self.tasks)}, mode={self.execution_mode})"


class CoordinationResult:
    """Result of multi-agent coordination.

    Attributes:
        plan_id: Plan identifier
        task_results: Dictionary mapping task_id to result
        total_duration_ms: Total execution duration in milliseconds
        success: Whether coordination succeeded
        error: Error message if failed
        completed_at: Completion timestamp (Unix timestamp float)

    Example:
        >>> result = CoordinationResult(
        ...     plan_id="plan-123",
        ...     task_results={"task-1": {"answer": "Success"}},
        ...     total_duration_ms=1500.0,
        ...     success=True,
        ...     error=None,
        ...     completed_at=1704067200.0
        ... )
    """

    def __init__(
        self,
        plan_id: str,
        task_results: Dict[str, Any],
        total_duration_ms: float,
        success: bool,
        error: Optional[str] = None,
        completed_at: Optional[float] = None,
    ):
        """Initialize coordination result.

        Args:
            plan_id: Plan identifier
            task_results: Task results dictionary
            total_duration_ms: Total duration in ms
            success: Success flag
            error: Error message if failed
            completed_at: Completion timestamp (Unix timestamp float)
        """
        self.plan_id = plan_id
        self.task_results = task_results
        self.total_duration_ms = total_duration_ms
        self.success = success
        self.error = error
        self.completed_at = completed_at or time.time()

    def __repr__(self) -> str:
        status = "success" if self.success else f"failed: {self.error}"
        return f"CoordinationResult(plan={self.plan_id}, {status}, {self.total_duration_ms:.1f}ms)"


class MultiAgentCoordinator:
    """Coordinates multiple agents for complex queries.

    The coordinator manages multi-agent execution with:
    - Query analysis to detect coordination needs
    - Plan creation with task dependencies
    - Sequential execution with context passing
    - Parallel execution (placeholder for v0.4.0+)
    - Full trace storage in ARC Memory

    Args:
        memory: ARC Memory instance for trace storage

    Examples:
        >>> from clio_agent.arc.memory import ARCMemory
        >>> arc = ARCMemory()
        >>> coordinator = MultiAgentCoordinator(arc)
        >>> agents = {"DataExpert": data_expert, "HPCExpert": hpc_expert}
        >>> plan = coordinator.create_plan("Optimize HDF5 on cluster", agents)
        >>> result = coordinator.execute_plan(plan, agents, "session-123")
        >>> print(f"Success: {result.success}, Duration: {result.total_duration_ms}ms")
    """

    def __init__(self, memory: ARCMemory):
        """Initialize multi-agent coordinator.

        Args:
            memory: ARC Memory instance for persistence
        """
        self.memory = memory

    def _select_agent_for_task(self, task_text: str, available_agents: Dict[str, Any]) -> str:
        """Select an available expert for a task segment using explicit keywords."""
        if not available_agents:
            raise ValueError("No agents available for coordination planning")

        task_lower = task_text.lower()
        keyword_routes = (
            ("DataExpert", ("hdf5", "file", "data", "parquet", "csv")),
            ("AnalysisExpert", ("analysis", "statistics", "stats", "compute")),
            ("HPCExpert", ("cluster", "hpc", "slurm")),
            ("VisualizationExpert", ("plot", "chart", "visualization", "visualize")),
        )

        for agent_id, keywords in keyword_routes:
            if any(keyword in task_lower for keyword in keywords):
                if agent_id not in available_agents:
                    raise ValueError(
                        f"Task segment matched {agent_id}, but that agent is not available"
                    )
                return agent_id

        raise ValueError(
            "No available agent matched task segment. "
            "Use more specific data, analysis, visualization, or HPC wording."
        )

    def create_plan(self, query: str, available_agents: Dict[str, Any]) -> CoordinationPlan:
        """Create coordination plan for query.

        Analyzes query to determine if multi-agent coordination is needed
        and creates an execution plan with agent tasks.

        Algorithm (v0.2.0 - simple keyword-based):
        1. Check for coordination keywords ("and", "then", "also")
        2. Extract sub-tasks from query
        3. Map tasks to agents based on keywords
        4. Create sequential plan (parallel in v0.4.0+)

        Args:
            query: User query to analyze
            available_agents: Dictionary of agent_id -> agent instance

        Returns:
            CoordinationPlan with tasks and execution mode

        Examples:
            >>> agents = {"DataExpert": expert1, "HPCExpert": expert2}
            >>> plan = coordinator.create_plan(
            ...     "Analyze HDF5 file and then optimize for cluster",
            ...     agents
            ... )
            >>> print(plan.tasks)
            [AgentTask(agent=DataExpert, ...), AgentTask(agent=HPCExpert, ...)]
        """
        plan_id = f"plan-{uuid.uuid4()}"
        tasks: List[AgentTask] = []

        # Simple keyword-based analysis (v0.2.0)
        query_lower = query.lower()

        # Check if multi-agent coordination is needed
        coordination_keywords = ["and then", "then", "also", "additionally", "next"]
        needs_coordination = any(kw in query_lower for kw in coordination_keywords)

        if not needs_coordination:
            agent_id = self._select_agent_for_task(query, available_agents)
            task = AgentTask(
                task_id=f"task-{uuid.uuid4()}",
                agent_id=agent_id,
                query=query,
                context={},
                depends_on=[],
            )
            tasks.append(task)
        else:
            # Multi-agent - split query and create sequential tasks
            # Simple heuristic: split on coordination keywords
            parts = []
            for keyword in coordination_keywords:
                if keyword in query_lower:
                    keyword_idx = query_lower.find(keyword)
                    parts = [
                        query[:keyword_idx].strip(),
                        query[keyword_idx + len(keyword) :].strip(),
                    ]
                    break

            if len(parts) >= 2:
                # Create tasks for each part
                prev_task_id = None

                for part in parts:
                    if not part:
                        raise ValueError("Coordination query produced an empty task segment")
                    agent_id = self._select_agent_for_task(part, available_agents)

                    task_id = f"task-{uuid.uuid4()}"
                    task = AgentTask(
                        task_id=task_id,
                        agent_id=agent_id,
                        query=part,
                        context={},
                        depends_on=[prev_task_id] if prev_task_id else [],
                    )
                    tasks.append(task)
                    prev_task_id = task_id
            else:
                raise ValueError("Could not split coordination query into task segments")

        execution_mode = "sequential" if needs_coordination else "sequential"

        plan = CoordinationPlan(
            plan_id=plan_id,
            tasks=tasks,
            execution_mode=execution_mode,
            created_at=time.time(),  # Unix timestamp float
        )

        return plan

    def execute_plan(
        self, plan: CoordinationPlan, agents: Dict[str, Any], session_id: str
    ) -> CoordinationResult:
        """Execute coordination plan.

        Executes the coordination plan using the specified execution mode
        (sequential or parallel) and stores full trace in ARC.

        Args:
            plan: Coordination plan to execute
            agents: Available agents dictionary
            session_id: Session ID for tracking

        Returns:
            CoordinationResult with task results and metadata

        Examples:
            >>> plan = coordinator.create_plan(query, agents)
            >>> result = coordinator.execute_plan(plan, agents, "session-123")
            >>> if result.success:
            ...     print("All tasks completed successfully")
        """
        start_time = time.time()
        task_results: Dict[str, Any] = {}
        success = True
        error = None

        try:
            if plan.execution_mode == "sequential":
                task_results = self.execute_sequential(plan.tasks, agents, session_id)
            elif plan.execution_mode == "parallel":
                task_results = self.execute_parallel(plan.tasks, agents, session_id)
            else:
                raise ValueError(f"Unknown execution mode: {plan.execution_mode}")

        except Exception as e:
            success = False
            error = str(e)

        total_duration_ms = (time.time() - start_time) * 1000

        result = CoordinationResult(
            plan_id=plan.plan_id,
            task_results=task_results,
            total_duration_ms=total_duration_ms,
            success=success,
            error=error,
            completed_at=time.time(),  # Unix timestamp float
        )

        # Store coordination trace in ARC
        self._store_coordination_trace(session_id, plan, result)

        return result

    def execute_sequential(
        self, tasks: List[AgentTask], agents: Dict[str, Any], session_id: str
    ) -> Dict[str, Any]:
        """Execute tasks sequentially with context passing.

        Executes tasks in order, passing results from previous tasks
        to subsequent tasks as context.

        Args:
            tasks: List of agent tasks to execute
            agents: Available agents dictionary
            session_id: Session ID for tracking

        Returns:
            Dictionary mapping task_id to result

        Examples:
            >>> tasks = [task1, task2]
            >>> results = coordinator.execute_sequential(tasks, agents, "session-1")
            >>> print(results["task-1"]["answer"])
        """
        task_results: Dict[str, Any] = {}

        for task in tasks:
            # Get agent
            agent = agents.get(task.agent_id)
            if not agent:
                task_results[task.task_id] = {
                    "error": f"Agent {task.agent_id} not found",
                    "success": False,
                }
                continue

            # Build context from dependencies
            prior_results = {}
            for dep_id in task.depends_on:
                if dep_id in task_results:
                    prior_results[dep_id] = task_results[dep_id]

            # Execute task
            try:
                result = self._execute_task(task, agent, session_id, prior_results)
                task_results[task.task_id] = {
                    "result": result,
                    "success": True,
                    "agent_id": task.agent_id,
                }
            except Exception as e:
                task_results[task.task_id] = {
                    "error": str(e),
                    "success": False,
                    "agent_id": task.agent_id,
                }

        return task_results

    def execute_parallel(
        self, tasks: List[AgentTask], agents: Dict[str, Any], session_id: str
    ) -> Dict[str, Any]:
        """Execute tasks in parallel.

        Placeholder for parallel execution (v0.4.0+).
        Currently falls back to sequential execution.

        Args:
            tasks: List of agent tasks to execute
            agents: Available agents dictionary
            session_id: Session ID for tracking

        Returns:
            Dictionary mapping task_id to result

        Examples:
            >>> # Will be implemented in v0.4.0+
            >>> results = coordinator.execute_parallel(tasks, agents, "session-1")
        """
        # TODO: Implement parallel execution using threading/asyncio in v0.4.0+
        # For now, fall back to sequential
        return self.execute_sequential(tasks, agents, session_id)

    def _execute_task(
        self,
        task: AgentTask,
        agent: Any,
        session_id: str,
        prior_results: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Execute single agent task.

        Args:
            task: Agent task to execute
            agent: Agent instance
            session_id: Session ID for tracking
            prior_results: Results from prior tasks

        Returns:
            Task execution result

        Raises:
            Exception: If task execution fails
        """
        start_time = time.time()
        trace_id = f"trace-{uuid.uuid4()}"

        # Build input with prior results as context
        task_input = {
            "query": task.query,
            "context": task.context,
            "prior_results": prior_results or {},
        }

        # Execute agent (assuming agent has forward() method)
        try:
            if hasattr(agent, "forward"):
                result = agent.forward(task.query)
            elif callable(agent):
                result = agent(task.query)
            else:
                raise TypeError(f"Agent {task.agent_id} is not callable")

            output = {"answer": str(result), "success": True}
            status = "success"

        except Exception as e:
            output = {"error": str(e), "success": False}
            status = "failure"
            raise

        finally:
            duration_ms = (time.time() - start_time) * 1000

            # Store invocation in ARC
            invocation = Invocation(
                trace_id=trace_id,
                session_id=session_id,
                parent_trace_id=None,
                agent_id=task.agent_id,
                tier=2,  # Expert tier
                source="coordination",
                started_at=start_time,  # Unix timestamp (float)
                completed_at=time.time(),  # Unix timestamp (float)
                duration_ms=duration_ms,
                status=status,
                input=task_input,
                output=output,
                tools_called=[],
                nanoagents_spawned=[],
                performance={"task_id": task.task_id},
            )
            self.memory.store_invocation(invocation)

        return result

    def _store_coordination_trace(
        self, session_id: str, plan: CoordinationPlan, result: CoordinationResult
    ) -> None:
        """Store coordination trace in ARC memory.

        Creates an invocation record for the coordination event
        that links all task invocations.

        Args:
            session_id: Session ID
            plan: Coordination plan executed
            result: Coordination result
        """
        trace_id = f"coordination-{plan.plan_id}"

        # Build coordination summary
        task_summary = [
            {
                "task_id": task.task_id,
                "agent_id": task.agent_id,
                "query": task.query,
                "depends_on": task.depends_on,
            }
            for task in plan.tasks
        ]

        invocation = Invocation(
            trace_id=trace_id,
            session_id=session_id,
            parent_trace_id=None,
            agent_id="MultiAgentCoordinator",
            tier=1,  # Main tier
            source="coordination",
            started_at=plan.created_at,
            completed_at=result.completed_at,
            duration_ms=result.total_duration_ms,
            status="success" if result.success else "failure",
            input={
                "plan_id": plan.plan_id,
                "execution_mode": plan.execution_mode,
                "task_count": len(plan.tasks),
                "tasks": task_summary,
            },
            output={
                "success": result.success,
                "error": result.error,
                "task_results": result.task_results,
            },
            tools_called=[],
            nanoagents_spawned=[],
            performance={
                "coordination": True,
                "total_tasks": len(plan.tasks),
            },
        )

        self.memory.store_invocation(invocation)
