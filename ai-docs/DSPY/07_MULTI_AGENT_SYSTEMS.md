# DSPy 3.x Multi-Agent & Multi-Step Systems
> Version: dspy-ai 3.1.3 | Updated: February 2026

Guide to building multi-agent architectures with DSPy 3.x. For individual module APIs (ReAct, CodeAct, Parallel, Tool), see [03_MODULES_GUIDE.md](03_MODULES_GUIDE.md). For tool integration, streaming, and async patterns, see [06_ADVANCED_PATTERNS.md](06_ADVANCED_PATTERNS.md).

---

## Table of Contents

1. [Multi-Agent Composition Patterns](#1-multi-agent-composition-patterns)
2. [Hierarchical Agent Systems](#2-hierarchical-agent-systems)
3. [Pipeline & Sequential Agents](#3-pipeline--sequential-agents)
4. [Router-Based Dispatch](#4-router-based-dispatch)
5. [Trajectory Tracking & State](#5-trajectory-tracking--state)
6. [Agent Optimization Strategies](#6-agent-optimization-strategies)
7. [Error Handling in Multi-Agent Systems](#7-error-handling-in-multi-agent-systems)
8. [Production Multi-Agent Deployment](#8-production-multi-agent-deployment)
9. [CLIO Agent Architecture Patterns](#9-clio-agent-architecture-patterns)

---

## 1. Multi-Agent Composition Patterns

DSPy treats agents as composable modules — standard Python classes inheriting from `dspy.Module`. Multi-agent systems compose these modules using standard patterns: hierarchy, pipeline, routing, and parallel fan-out.

### 1.1 Core Principle: Modules as Agents

Every DSPy agent is a `dspy.Module`. Composition is plain Python:

```python
import dspy

class ResearchAgent(dspy.Module):
    """Agent that researches a topic using search tools."""
    def __init__(self):
        self.agent = dspy.ReAct(
            "topic -> findings: str",
            tools=[search_web, search_papers],
            max_iters=5
        )

    def forward(self, topic: str) -> dspy.Prediction:
        return self.agent(topic=topic)


class WriterAgent(dspy.Module):
    """Agent that writes content from research findings."""
    def __init__(self):
        self.writer = dspy.ChainOfThought("findings, style -> article: str")

    def forward(self, findings: str, style: str = "technical") -> dspy.Prediction:
        return self.writer(findings=findings, style=style)


class ResearchAndWrite(dspy.Module):
    """Composed multi-agent pipeline."""
    def __init__(self):
        self.researcher = ResearchAgent()
        self.writer = WriterAgent()

    def forward(self, topic: str) -> dspy.Prediction:
        research = self.researcher(topic=topic)
        article = self.writer(findings=research.findings)
        return article
```

### 1.2 Composition vs. Orchestration Frameworks

DSPy's approach differs from LangGraph, CrewAI, and AutoGen:

| Aspect | DSPy | LangGraph/CrewAI |
|--------|------|------------------|
| Agent definition | Python class + Module | Config/YAML/decorator |
| Composition | Plain Python code | Graph/DAG/role definitions |
| Optimization | Automatic (SIMBA, MIPRO) | Manual prompt tuning |
| State | Python attributes + dspy.History | Explicit state graphs |
| Tool binding | `dspy.Tool` wrapping | Framework-specific adapters |

---

## 2. Hierarchical Agent Systems

### 2.1 Three-Tier Hierarchy (CLIO Pattern)

```python
class NanoAgent(dspy.Module):
    """Tier 3: Single-task specialist."""
    def __init__(self, task_signature: str, tools: list = None):
        self.agent = dspy.ReAct(task_signature, tools=tools or [], max_iters=3)

    def forward(self, **kwargs) -> dspy.Prediction:
        return self.agent(**kwargs)


class ExpertAgent(dspy.Module):
    """Tier 2: Domain expert that delegates to nanoagents."""
    def __init__(self):
        self.planner = dspy.ChainOfThought(
            "query, available_capabilities: list[str] -> plan: str, selected_agents: list[str]"
        )
        self.agents = {
            "file_inspector": NanoAgent("filepath -> file_info: str", tools=[inspect_file]),
            "schema_analyzer": NanoAgent("filepath -> schema: str", tools=[read_schema]),
            "stats_computer": NanoAgent("filepath, columns: list[str] -> statistics: str", tools=[compute_stats]),
        }

    def forward(self, query: str) -> dspy.Prediction:
        capabilities = list(self.agents.keys())
        plan = self.planner(query=query, available_capabilities=capabilities)

        results = []
        for agent_name in plan.selected_agents:
            if agent_name in self.agents:
                result = self.agents[agent_name](query=query)
                results.append(result)

        return dspy.Prediction(results=results, plan=plan.plan)


class OrchestratorAgent(dspy.Module):
    """Tier 1: Main orchestrator routing to expert domains."""
    def __init__(self):
        self.router = dspy.ChainOfThought(
            "user_request -> domain: Literal['data', 'io', 'config'], reasoning: str"
        )
        self.experts = {
            "data": ExpertAgent(),
            "io": IOExpert(),
            "config": ConfigExpert(),
        }
        self.synthesizer = dspy.ChainOfThought(
            "user_request, expert_results: str -> final_response: str"
        )

    def forward(self, user_request: str) -> dspy.Prediction:
        routing = self.router(user_request=user_request)
        expert = self.experts[routing.domain]
        expert_result = expert(query=user_request)
        return self.synthesizer(
            user_request=user_request,
            expert_results=str(expert_result)
        )
```

### 2.2 Dynamic Agent Spawning

```python
class DynamicOrchestrator(dspy.Module):
    """Spawns agents based on task decomposition."""
    def __init__(self, tool_registry: dict):
        self.decomposer = dspy.ChainOfThought(
            "complex_task -> subtasks: list[str], tool_sets: list[list[str]]"
        )
        self.tool_registry = tool_registry

    def forward(self, complex_task: str) -> dspy.Prediction:
        plan = self.decomposer(complex_task=complex_task)

        results = []
        for subtask, tool_names in zip(plan.subtasks, plan.tool_sets):
            tools = [self.tool_registry[t] for t in tool_names if t in self.tool_registry]
            agent = dspy.ReAct(
                "subtask -> result: str",
                tools=tools,
                max_iters=5
            )
            result = agent(subtask=subtask)
            results.append(result.result)

        synthesizer = dspy.ChainOfThought(
            "task, subtask_results: list[str] -> final_answer: str"
        )
        return synthesizer(task=complex_task, subtask_results=results)
```

---

## 3. Pipeline & Sequential Agents

### 3.1 Linear Pipeline

```python
class AnalysisPipeline(dspy.Module):
    """Sequential agent pipeline: extract -> analyze -> report."""
    def __init__(self):
        self.extractor = dspy.ReAct(
            "filepath -> extracted_data: str, metadata: str",
            tools=[read_file, parse_csv, parse_hdf5]
        )
        self.analyzer = dspy.ChainOfThought(
            "extracted_data, metadata, analysis_type -> findings: str, confidence: float"
        )
        self.reporter = dspy.ChainOfThought(
            "findings, confidence: float -> report: str, recommendations: list[str]"
        )

    def forward(self, filepath: str, analysis_type: str = "summary") -> dspy.Prediction:
        extracted = self.extractor(filepath=filepath)
        analysis = self.analyzer(
            extracted_data=extracted.extracted_data,
            metadata=extracted.metadata,
            analysis_type=analysis_type
        )
        return self.reporter(
            findings=analysis.findings,
            confidence=analysis.confidence
        )
```

### 3.2 Conditional Pipeline

```python
class ConditionalPipeline(dspy.Module):
    """Pipeline with branching based on intermediate results."""
    def __init__(self):
        self.classifier = dspy.Predict("data_description -> data_type: Literal['tabular', 'image', 'text', 'timeseries']")
        self.handlers = {
            "tabular": TabularAnalyzer(),
            "image": ImageAnalyzer(),
            "text": TextAnalyzer(),
            "timeseries": TimeseriesAnalyzer(),
        }
        self.fallback = dspy.ChainOfThought("data_description -> analysis: str")

    def forward(self, data_description: str) -> dspy.Prediction:
        classification = self.classifier(data_description=data_description)
        handler = self.handlers.get(classification.data_type)
        if handler:
            return handler(data_description=data_description)
        return self.fallback(data_description=data_description)
```

---

## 4. Router-Based Dispatch

### 4.1 Capability-Based Router

```python
class CapabilityRouter(dspy.Module):
    """Routes queries to agents based on capability matching."""
    def __init__(self, agents: dict[str, dspy.Module], descriptions: dict[str, str]):
        self.agents = agents
        self.descriptions = descriptions

        # Build routing signature dynamically
        agent_names = list(agents.keys())
        self.router = dspy.ChainOfThought(
            f"query, agent_descriptions: str -> selected_agent: Literal{agent_names}, confidence: float, reasoning: str"
        )

    def forward(self, query: str) -> dspy.Prediction:
        desc_str = "\n".join(f"- {k}: {v}" for k, v in self.descriptions.items())
        routing = self.router(query=query, agent_descriptions=desc_str)

        if routing.confidence < 0.5:
            # Low confidence — try multiple agents
            results = {}
            for name, agent in self.agents.items():
                try:
                    results[name] = agent(query=query)
                except Exception:
                    continue
            return dspy.Prediction(results=results, strategy="multi-agent")

        agent = self.agents[routing.selected_agent]
        return agent(query=query)


# Usage
router = CapabilityRouter(
    agents={
        "data_expert": DataExpert(),
        "io_expert": IOExpert(),
        "config_expert": ConfigExpert(),
    },
    descriptions={
        "data_expert": "Analyzes scientific data files (HDF5, Parquet, CSV)",
        "io_expert": "Handles I/O patterns and performance analysis",
        "config_expert": "Manages configuration and IOWarp settings",
    }
)
result = router(query="What's the schema of experiment_001.h5?")
```

### 4.2 Multi-Agent Fan-Out with dspy.Parallel

```python
class FanOutAnalysis(dspy.Module):
    """Run multiple analysis agents in parallel, synthesize results."""
    def __init__(self):
        self.agents = [
            dspy.ChainOfThought("data -> statistical_summary: str"),
            dspy.ChainOfThought("data -> anomaly_report: str"),
            dspy.ChainOfThought("data -> trend_analysis: str"),
        ]
        self.parallel = dspy.Parallel(num_threads=3, provide_traceback=True)
        self.synthesizer = dspy.ChainOfThought(
            "statistical_summary, anomaly_report, trend_analysis -> comprehensive_report: str"
        )

    def forward(self, data: str) -> dspy.Prediction:
        # Prepare parallel execution
        exec_pairs = [(agent, {"data": data}) for agent in self.agents]
        results = self.parallel(exec_pairs)

        return self.synthesizer(
            statistical_summary=results[0].statistical_summary,
            anomaly_report=results[1].anomaly_report,
            trend_analysis=results[2].trend_analysis,
        )
```

---

## 5. Trajectory Tracking & State

### 5.1 dspy.History

`dspy.History` captures the full trajectory of agent execution — every LM call, tool invocation, and intermediate result.

```python
import dspy

agent = dspy.ReAct("task -> result", tools=[search, calculate], max_iters=5)

# Enable history tracking
dspy.configure(track_usage=True)
result = agent(task="Find the population of France and calculate its density")

# Access trajectory
history = result.history  # dspy.History object

# Iterate over trajectory steps
for step in history:
    print(f"Step: {step.module_name}")
    print(f"  Input: {step.inputs}")
    print(f"  Output: {step.outputs}")
    if step.tool_calls:
        for tc in step.tool_calls:
            print(f"  Tool: {tc.name}({tc.args}) -> {tc.result}")
```

### 5.2 Custom State Management

```python
class StatefulAgent(dspy.Module):
    """Agent with explicit state tracking across turns."""
    def __init__(self):
        self.agent = dspy.ReAct(
            "query, conversation_history: str -> response: str, updated_state: str",
            tools=[search_data, analyze_file],
            max_iters=5
        )
        self.state = []

    def forward(self, query: str) -> dspy.Prediction:
        history_str = "\n".join(
            f"User: {s['query']}\nAgent: {s['response']}"
            for s in self.state[-5:]  # Last 5 turns
        )
        result = self.agent(query=query, conversation_history=history_str)
        self.state.append({"query": query, "response": result.response})
        return result

    def reset(self):
        self.state.clear()
```

### 5.3 Usage Tracking

```python
# Track LM usage across multi-agent calls
dspy.configure(track_usage=True)

orchestrator = OrchestratorAgent()
result = orchestrator(user_request="Analyze my HDF5 dataset")

# Get total usage across all agents
usage = dspy.get_lm_usage()
for model, stats in usage.items():
    print(f"{model}: {stats['prompt_tokens']} prompt, {stats['completion_tokens']} completion")
    print(f"  Cost: ${stats.get('cost', 0):.4f}")
    print(f"  Calls: {stats['num_calls']}")

# Reset counters
dspy.reset_lm_usage()
```

---

## 6. Agent Optimization Strategies

### 6.1 SIMBA for Agentic Workloads

SIMBA (SIMulated Bandit-based Agent optimization) is purpose-built for optimizing agents and multi-step systems. See [04_OPTIMIZATION_GUIDE.md](04_OPTIMIZATION_GUIDE.md#simba-agent-optimized-new-in-3x) for full API.

```python
# Optimize a multi-agent system end-to-end
trainset = [
    dspy.Example(user_request="Analyze experiment data", expected_output="...").with_inputs("user_request"),
    # ... 50-200 examples recommended
]

def agent_metric(example, pred, trace=None):
    """Evaluate agent quality including tool usage efficiency."""
    correctness = dspy.evaluate.answer_exact_match(example, pred)

    # Penalize excessive tool calls
    tool_calls = len(trace) if trace else 0
    efficiency = max(0, 1.0 - (tool_calls - 3) * 0.1) if tool_calls > 3 else 1.0

    return correctness * 0.7 + efficiency * 0.3

optimizer = dspy.SIMBA(
    metric=agent_metric,
    max_bootstrapped_demos=4,
    max_labeled_demos=8,
    num_candidate_programs=10,
)

optimized_system = optimizer.compile(
    OrchestratorAgent(),
    trainset=trainset,
)
```

### 6.2 Optimizing Individual Agents in a System

```python
class OptimizableSystem(dspy.Module):
    """System where each agent can be independently optimized."""
    def __init__(self):
        self.router = dspy.ChainOfThought("query -> domain, reasoning")
        self.data_expert = DataExpert()
        self.io_expert = IOExpert()

    def forward(self, query: str) -> dspy.Prediction:
        routing = self.router(query=query)
        if routing.domain == "data":
            return self.data_expert(query=query)
        return self.io_expert(query=query)


# Strategy: Optimize each component with its own optimizer and dataset
# 1. Optimize router independently
router_trainset = [dspy.Example(query="...", domain="data").with_inputs("query")]
router_optimizer = dspy.MIPROv2(metric=routing_accuracy, num_candidates=20)

# 2. Optimize experts with SIMBA (agent-aware)
expert_optimizer = dspy.SIMBA(metric=expert_metric, max_bootstrapped_demos=4)

# 3. Compose optimized components
system = OptimizableSystem()
system.router = router_optimizer.compile(system.router, trainset=router_trainset)
system.data_expert = expert_optimizer.compile(system.data_expert, trainset=expert_trainset)
```

### 6.3 BootstrapFinetune for Multi-Agent Systems

```python
# Collect trajectories from a strong teacher model, finetune a smaller model
teacher_lm = dspy.LM("openai/gpt-4o")
student_lm = dspy.LM("openai/gpt-4o-mini")

# Build system with teacher
dspy.configure(lm=teacher_lm)
teacher_system = OrchestratorAgent()

# Optimize with BootstrapFinetune
optimizer = dspy.BootstrapFinetune(
    metric=agent_metric,
    max_bootstrapped_demos=4,
    num_threads=4,
)

# This collects teacher trajectories, finetunes student
optimized = optimizer.compile(
    teacher_system,
    trainset=trainset,
    target=student_lm.model,
)

# Deploy with cheaper student model
dspy.configure(lm=student_lm)
result = optimized(user_request="Analyze my data")
```

---

## 7. Error Handling in Multi-Agent Systems

### 7.1 Agent-Level Error Isolation

```python
class ResilientOrchestrator(dspy.Module):
    """Orchestrator with per-agent error isolation."""
    def __init__(self):
        self.primary = DataExpert()
        self.fallback = dspy.ChainOfThought("query -> response: str")

    def forward(self, query: str) -> dspy.Prediction:
        try:
            result = self.primary(query=query)
            if not result or not hasattr(result, 'response'):
                raise ValueError("Empty result from primary agent")
            return result
        except Exception as e:
            # Log failure, use fallback
            dspy.logger.warning(f"Primary agent failed: {e}, using fallback")
            return self.fallback(query=query)
```

### 7.2 Retry with Backoff

```python
class RetryAgent(dspy.Module):
    """Agent with configurable retry strategy."""
    def __init__(self, inner: dspy.Module, max_retries: int = 3):
        self.inner = inner
        self.max_retries = max_retries
        self.refiner = dspy.Refine(
            module=inner,
            N=max_retries,
            reward_fn=lambda result: bool(result and hasattr(result, 'response')),
        )

    def forward(self, **kwargs) -> dspy.Prediction:
        return self.refiner(**kwargs)
```

### 7.3 Circuit Breaker Pattern

```python
import time

class CircuitBreaker:
    """Circuit breaker for agent calls."""
    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60.0):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.last_failure_time = 0.0
        self.state = "closed"  # closed, open, half-open

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure_time > self.reset_timeout:
                self.state = "half-open"
                return True
            return False
        return True  # half-open: allow one attempt

    def record_success(self):
        self.failure_count = 0
        self.state = "closed"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"


class ProtectedAgent(dspy.Module):
    """Agent protected by circuit breaker."""
    def __init__(self, agent: dspy.Module, fallback: dspy.Module):
        self.agent = agent
        self.fallback = fallback
        self.breaker = CircuitBreaker()

    def forward(self, **kwargs) -> dspy.Prediction:
        if not self.breaker.can_execute():
            return self.fallback(**kwargs)
        try:
            result = self.agent(**kwargs)
            self.breaker.record_success()
            return result
        except Exception:
            self.breaker.record_failure()
            return self.fallback(**kwargs)
```

---

## 8. Production Multi-Agent Deployment

### 8.1 FastAPI Deployment

```python
from fastapi import FastAPI
import dspy
import uvicorn

app = FastAPI()

# Load optimized system once at startup
dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))
system = OrchestratorAgent()
system.load("optimized_system.json")  # Load optimized prompts/demos

@app.post("/query")
async def handle_query(request: dict):
    result = system(user_request=request["query"])
    return {
        "response": result.response,
        "metadata": {
            "domain": getattr(result, "domain", None),
        }
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### 8.2 Async Multi-Agent Execution

```python
import asyncio
import dspy

class AsyncOrchestrator(dspy.Module):
    """Orchestrator using async for concurrent agent execution."""
    def __init__(self):
        self.agents = {
            "data": DataExpert(),
            "io": IOExpert(),
            "config": ConfigExpert(),
        }

    async def aforward(self, queries: dict[str, str]) -> dspy.Prediction:
        """Execute multiple agent queries concurrently."""
        tasks = []
        for domain, query in queries.items():
            if domain in self.agents:
                tasks.append(self._run_agent(domain, query))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        combined = {}
        for domain, result in results:
            if isinstance(result, Exception):
                combined[domain] = f"Error: {result}"
            else:
                combined[domain] = result.response

        return dspy.Prediction(results=combined)

    async def _run_agent(self, domain: str, query: str):
        result = await self.agents[domain].acall(query=query)
        return (domain, result)


# Execute
async def main():
    orchestrator = AsyncOrchestrator()
    result = await orchestrator.acall(
        queries={
            "data": "Schema of experiment.h5",
            "io": "Current I/O throughput",
        }
    )
    print(result.results)

asyncio.run(main())
```

### 8.3 Per-Request LM Override with dspy.context()

```python
import dspy

# Global default
dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))
system = OrchestratorAgent()

# Per-request override (thread-safe)
with dspy.context(lm=dspy.LM("openai/gpt-4o")):
    # This request uses gpt-4o
    result = system(user_request="Complex analysis task")

# Back to gpt-4o-mini outside the context
result = system(user_request="Simple query")
```

### 8.4 Streaming Multi-Agent Output

```python
import dspy

system = OrchestratorAgent()

# Stream the final output
stream = dspy.streamify(system)

async for chunk in stream(user_request="Analyze this dataset"):
    if isinstance(chunk, dspy.Prediction):
        print(f"\nFinal: {chunk.response}")
    else:
        print(chunk, end="", flush=True)
```

---

## 9. CLIO Agent Architecture Patterns

### 9.1 Three-Tier Mapping

CLIO Agent uses DSPy's module composition for its three-tier architecture:

| CLIO Tier | DSPy Pattern | Role |
|-----------|-------------|------|
| Tier 1: Main Agent | `OrchestratorAgent(dspy.Module)` | Routes user queries to experts |
| Tier 2: Experts | `DataExpert(dspy.Module)` using `dspy.ReAct` | Domain-specific reasoning + tools |
| Tier 3: Nanoagents | `NanoAgent(dspy.Module)` using `dspy.Predict` | Single-task execution |

### 9.2 Registry-Based Routing

```python
class CLIORouter(dspy.Module):
    """CLIO's capability-based routing using the agent registry."""
    def __init__(self, registry):
        self.registry = registry
        self.router = dspy.ChainOfThought(
            "query, capabilities: str -> selected_agent: str, reasoning: str"
        )

    def forward(self, query: str) -> dspy.Prediction:
        # Get capabilities from registry
        capabilities = self.registry.get_capability_descriptions()
        routing = self.router(query=query, capabilities=capabilities)

        # Get agent from registry
        agent = self.registry.get_agent(routing.selected_agent)
        return agent(query=query)
```

### 9.3 ARC-Integrated Agent Pattern

```python
class ARCAgent(dspy.Module):
    """Agent that checks ARC cache before execution."""
    def __init__(self, arc, inner_agent: dspy.Module):
        self.arc = arc
        self.inner = inner_agent

    def forward(self, query: str) -> dspy.Prediction:
        # Cache-first pattern
        cached = self.arc.get_cached_result(query)
        if cached:
            return dspy.Prediction(**cached)

        # Execute agent
        import time
        start = time.time()
        result = self.inner(query=query)
        duration_ms = (time.time() - start) * 1000

        # Store in ARC
        self.arc.cache_result(query, result.toDict())
        self.arc.store_invocation({
            "query": query,
            "duration_ms": duration_ms,
            "success": True,
            "agent": type(self.inner).__name__,
        })
        return result
```

### 9.4 MCP Gateway with Multi-Agent Backend

```python
from fastmcp import FastMCP
import dspy

mcp_server = FastMCP("CLIO Agent")

# Initialize multi-agent system
dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))
system = OrchestratorAgent()

@mcp_server.tool
def query(user_request: str) -> str:
    """Process a user request through CLIO's multi-agent system."""
    result = system(user_request=user_request)
    return result.response

@mcp_server.tool
def analyze_file(filepath: str, analysis_type: str = "summary") -> str:
    """Analyze a scientific data file."""
    pipeline = AnalysisPipeline()
    result = pipeline(filepath=filepath, analysis_type=analysis_type)
    return result.report

if __name__ == "__main__":
    mcp_server.run()
```

---

## Quick Reference

| Pattern | When to Use | Key Classes |
|---------|-------------|-------------|
| Hierarchical | Complex domains, tier-based routing | Module composition |
| Pipeline | Sequential processing stages | Module chaining |
| Router | Multiple specialists, dynamic dispatch | ChainOfThought + Literal |
| Fan-out | Independent parallel analyses | dspy.Parallel |
| Stateful | Multi-turn conversations | Custom state + History |
| Cache-first | Repeated queries, expensive agents | ARC integration |

**Optimization Quick Guide:**
- Router modules → MIPROv2 (instruction + demo optimization)
- Tool-using agents → SIMBA (agent-aware, long-horizon)
- End-to-end pipeline → BootstrapFinetune (teacher→student distillation)

**See also:**
- [03_MODULES_GUIDE.md](03_MODULES_GUIDE.md) — ReAct, CodeAct, Parallel, Tool APIs
- [04_OPTIMIZATION_GUIDE.md](04_OPTIMIZATION_GUIDE.md) — Full optimizer reference
- [06_ADVANCED_PATTERNS.md](06_ADVANCED_PATTERNS.md) — Tool integration, streaming, async, deployment
