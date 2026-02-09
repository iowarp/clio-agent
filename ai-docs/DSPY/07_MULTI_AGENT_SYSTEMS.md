# DSPy 3.x Multi-Agent & Multi-Step Systems
> Version: dspy-ai 3.1.3 | Updated: February 2026

Guide to building multi-agent architectures with DSPy 3.x. For individual module APIs (ReAct, CodeAct, Parallel, Tool), see [03_MODULES_GUIDE.md](03_MODULES_GUIDE.md). For tool integration, streaming, and async patterns, see [06_ADVANCED_PATTERNS.md](06_ADVANCED_PATTERNS.md).

---

## Table of Contents

1. [Multi-Agent Composition Patterns](#1-multi-agent-composition-patterns)
2. [Hierarchical Agent Systems](#2-hierarchical-agent-systems)
3. [Pipeline & Sequential Agents](#3-pipeline--sequential-agents)
4. [Router-Based Dispatch](#4-router-based-dispatch)
5. [Agent-as-Tool (Nested Agents)](#5-agent-as-tool-nested-agents)
6. [Trajectory Tracking & State](#6-trajectory-tracking--state)
7. [Manual Tool Handling with ToolCalls](#7-manual-tool-handling-with-toolcalls)
8. [Agent Optimization Strategies](#8-agent-optimization-strategies)
9. [Output Refinement (BestOfN & Refine)](#9-output-refinement-bestonf--refine)
10. [Error Handling in Multi-Agent Systems](#10-error-handling-in-multi-agent-systems)
11. [Production Multi-Agent Deployment](#11-production-multi-agent-deployment)
12. [CLIO Agent Architecture Patterns](#12-clio-agent-architecture-patterns)

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

        agent_names = list(agents.keys())
        self.router = dspy.ChainOfThought(
            f"query, agent_descriptions: str -> selected_agent: Literal{agent_names}, confidence: float, reasoning: str"
        )

    def forward(self, query: str) -> dspy.Prediction:
        desc_str = "\n".join(f"- {k}: {v}" for k, v in self.descriptions.items())
        routing = self.router(query=query, agent_descriptions=desc_str)

        if routing.confidence < 0.5:
            results = {}
            for name, agent in self.agents.items():
                try:
                    results[name] = agent(query=query)
                except Exception:
                    continue
            return dspy.Prediction(results=results, strategy="multi-agent")

        agent = self.agents[routing.selected_agent]
        return agent(query=query)
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
        exec_pairs = [(agent, {"data": data}) for agent in self.agents]
        results = self.parallel(exec_pairs)

        return self.synthesizer(
            statistical_summary=results[0].statistical_summary,
            anomaly_report=results[1].anomaly_report,
            trend_analysis=results[2].trend_analysis,
        )
```

---

## 5. Agent-as-Tool (Nested Agents)

Wrap an agent as a callable tool for another agent — enables hierarchical delegation:

```python
# Sub-agent with its own tools
sub_agent = dspy.ReAct("sub_task -> sub_result", tools=[tool_a, tool_b])

def delegate_to_specialist(sub_task: str) -> str:
    """Delegate a sub-task to a specialized data analysis agent."""
    result = sub_agent(sub_task=sub_task)
    return result.sub_result

# Main agent uses the sub-agent as one of its tools
main_agent = dspy.ReAct(
    "question -> answer",
    tools=[delegate_to_specialist, search_web, calculator],
    max_iters=10
)

result = main_agent(question="Analyze the correlation between columns A and B in data.csv")
```

---

## 6. Trajectory Tracking & State

### 6.1 ReAct Trajectory Structure

ReAct internally builds a trajectory dict during execution. Each iteration adds 4 keys:

```python
# Internal trajectory structure (from ReAct.forward())
trajectory = {
    "thought_0": "I need to look up the weather...",
    "tool_name_0": "get_weather",
    "tool_args_0": {"city": "Tokyo"},
    "observation_0": "The weather in Tokyo is sunny.",
    "thought_1": "I now have the answer...",
    "tool_name_1": "finish",       # Built-in finish tool signals completion
    "tool_args_1": {},
    "observation_1": "Completed."
}
```

**Key behaviors:**
- ReAct auto-adds a built-in `finish` tool — calling it ends the loop
- Tool execution errors are captured as observation strings, not exceptions — the agent can reason about and retry failures
- Override `truncate_trajectory(trajectory)` for custom context-window management (default: removes oldest 4 keys per truncation)

```python
class CustomReAct(dspy.ReAct):
    def truncate_trajectory(self, trajectory):
        """Keep only last 3 iterations instead of default behavior."""
        keys = list(trajectory.keys())
        while len(keys) > 12:  # 4 keys per iteration * 3 iterations
            del trajectory[keys.pop(0)]
```

### 6.2 dspy.History

`dspy.History` is a frozen Pydantic `BaseModel` for conversation history:

```python
import dspy

# Create history from previous interactions
history = dspy.History(
    messages=[
        {"question": "What is the capital of France?", "answer": "Paris"},
        {"question": "What is the capital of Germany?", "answer": "Berlin"},
    ]
)

# Pass history to maintain context across calls
predict = dspy.Predict("question, history: dspy.History -> answer: str")
result = predict(question="Which one has a larger population?", history=history)
```

**Key:** Message keys must match the associated signature fields. History is immutable (frozen).

### 6.3 Custom State Management

```python
class StatefulAgent(dspy.Module):
    """Agent with explicit state tracking across turns."""
    def __init__(self):
        self.agent = dspy.ReAct(
            "query, conversation_history: str -> response: str",
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

### 6.4 Usage Tracking

```python
dspy.configure(track_usage=True)

orchestrator = OrchestratorAgent()
result = orchestrator(user_request="Analyze my HDF5 dataset")

# Get total usage across all agents
usage = dspy.get_lm_usage()
for model, stats in usage.items():
    print(f"{model}: {stats['prompt_tokens']} prompt, {stats['completion_tokens']} completion")
    print(f"  Cost: ${stats.get('cost', 0):.4f}")
    print(f"  Calls: {stats['num_calls']}")

dspy.reset_lm_usage()
```

### 6.5 Cache Bypass for Agent Exploration

Use unique `rollout_id` + non-zero temperature to bypass cache while still caching new results:

```python
predict = dspy.Predict("question -> answer")

# Each call with different rollout_id bypasses cache
predict(question="1+1", config={"rollout_id": 1, "temperature": 1.0})
predict(question="1+1", config={"rollout_id": 2, "temperature": 1.0})

# Or disable cache globally
dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)
```

---

## 7. Manual Tool Handling with ToolCalls

For fine-grained control over tool execution (bypassing ReAct's loop):

```python
class ToolSignature(dspy.Signature):
    """Manual tool routing."""
    question: str = dspy.InputField()
    tools: list[dspy.Tool] = dspy.InputField()
    outputs: dspy.ToolCalls = dspy.OutputField()

tools = {
    "weather": dspy.Tool(weather_fn),
    "calculator": dspy.Tool(calc_fn),
}

predictor = dspy.Predict(ToolSignature)
response = predictor(question="What's 2+2?", tools=list(tools.values()))

# Execute tool calls from the output
for call in response.outputs.tool_calls:
    # Auto-discover functions by name
    result = call.execute()

    # Or pass explicit function mapping
    result = call.execute(functions={"weather": weather_fn, "calculator": calc_fn})

    # Or pass Tool objects
    result = call.execute(functions=[dspy.Tool(weather_fn)])
```

**ToolCalls type details:**
- `from_dict_list(list[dict])` — each dict has `name` and `args` keys
- `format()` — returns OpenAI-compatible tool_calls schema
- `validate_input()` — handles list of dicts, dict with "tool_calls" key, or individual pairs
- `is_streamable()` returns `False` — ToolCalls cannot be streamed

---

## 8. Agent Optimization Strategies

### 8.1 SIMBA for Agentic Workloads

SIMBA (Stochastic Introspective Mini-Batch Ascent) is purpose-built for optimizing agents and multi-step systems.

```python
optimizer = dspy.SIMBA(
    metric=agent_metric,                    # Required: (example, pred) -> float
    bsize=32,                               # Mini-batch size
    num_candidates=6,                       # New candidates per iteration
    max_steps=8,                            # Total optimization iterations
    max_demos=4,                            # Max demos per predictor before removal
    prompt_model=None,                      # LM for program evolution (default: configured LM)
    teacher_settings=None,                  # Override for teacher config
    demo_input_field_maxlen=100_000,        # Max chars for demo input fields
    num_threads=None,                       # Parallel threads
    temperature_for_sampling=0.2,           # Temperature for sampling programs
    temperature_for_candidates=0.2,         # Temperature for generating candidates
)

optimized = optimizer.compile(
    student=OrchestratorAgent(),
    trainset=trainset,
    seed=0,
)

# Access optimization results
optimized.candidate_programs  # list of {"score": float, "program": Module}
optimized.trial_logs          # batch_idx -> trial info
```

**Algorithm:** Iterates in mini-batches using softmax sampling to select programs. Stochastically drops demos (Poisson distribution) and applies dual strategies ("append_a_demo" or "append_a_rule") with LLM introspection for improvement rules.

### 8.2 Optimizing Individual Agents in a System

```python
class OptimizableSystem(dspy.Module):
    def __init__(self):
        self.router = dspy.ChainOfThought("query -> domain, reasoning")
        self.data_expert = DataExpert()
        self.io_expert = IOExpert()

    def forward(self, query: str) -> dspy.Prediction:
        routing = self.router(query=query)
        if routing.domain == "data":
            return self.data_expert(query=query)
        return self.io_expert(query=query)


# Strategy: Optimize each component independently
# 1. Router with MIPROv2 (instruction + demo optimization)
router_optimizer = dspy.MIPROv2(
    metric=routing_accuracy,
    auto="light",                    # "light", "medium", or "heavy"
    num_candidates=20,
    max_bootstrapped_demos=3,
    max_labeled_demos=4,
)

# 2. Experts with SIMBA (agent-aware)
expert_optimizer = dspy.SIMBA(metric=expert_metric, max_demos=4)

# 3. Compose optimized components
system = OptimizableSystem()
system.router = router_optimizer.compile(system.router, trainset=router_trainset)
system.data_expert = expert_optimizer.compile(system.data_expert, trainset=expert_trainset)

# Save/Load optimized programs
system.save("optimized_system.json")
loaded = OptimizableSystem()
loaded.load("optimized_system.json")
```

### 8.3 BootstrapFinetune for Multi-Agent Systems

```python
teacher_lm = dspy.LM("openai/gpt-4o")
student_lm = dspy.LM("openai/gpt-4o-mini")

dspy.configure(lm=teacher_lm)

optimizer = dspy.BootstrapFinetune(
    metric=agent_metric,
    multitask=True,                  # Share training data across predictors
    train_kwargs=None,               # Per-LM finetuning config
    adapter=None,                    # Data format adapter
    exclude_demos=False,             # Clear demos post-compilation
    num_threads=None,
)

# Collects teacher trajectories, finetunes student
optimized = optimizer.compile(
    student=OrchestratorAgent(),
    trainset=trainset,
    teacher=None,                    # Optional: explicit teacher or list[Module]
    target=student_lm.model,
)

# Deploy with cheaper student model
dspy.configure(lm=student_lm)
result = optimized(user_request="Analyze my data")
```

**Workflow:** Validates predictors → bootstraps trace data from teacher → prepares finetuning data grouped by LM → runs parallel finetuning via `lm.finetune()` → updates student with finetuned models.

---

## 9. Output Refinement (BestOfN & Refine)

### 9.1 dspy.BestOfN

Runs a module up to N times, returns the best result by `reward_fn` or first passing threshold:

```python
qa = dspy.ChainOfThought("question -> answer")

def one_word_reward(args, pred):
    return 1.0 if len(pred.answer.split()) == 1 else 0.0

best = dspy.BestOfN(
    module=qa,
    N=3,                    # Max attempts
    reward_fn=one_word_reward,
    threshold=1.0,          # Early-stop threshold
)
result = best(question="What is the capital of Belgium?")
# result.answer -> "Brussels"
```

Each attempt uses a unique `rollout_id` (bypasses cache). Returns best prediction by `reward_fn` score.

### 9.2 dspy.Refine

Same interface as BestOfN but with **iterative feedback** — after each attempt, generates detailed feedback and uses hints for subsequent runs:

```python
refine = dspy.Refine(
    module=qa,
    N=3,
    reward_fn=one_word_reward,
    threshold=1.0,
    fail_count=1,           # Optional: fail after N consecutive errors
)
result = refine(question="What is the capital of Belgium?")
```

**Use for agent retry patterns:**

```python
class RetryAgent(dspy.Module):
    def __init__(self, inner: dspy.Module, max_retries: int = 3):
        self.refiner = dspy.Refine(
            module=inner,
            N=max_retries,
            reward_fn=lambda args, pred: 1.0 if hasattr(pred, 'response') and pred.response else 0.0,
            threshold=0.5,
        )

    def forward(self, **kwargs) -> dspy.Prediction:
        return self.refiner(**kwargs)
```

---

## 10. Error Handling in Multi-Agent Systems

### 10.1 Agent-Level Error Isolation

```python
class ResilientOrchestrator(dspy.Module):
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
            dspy.logger.warning(f"Primary agent failed: {e}, using fallback")
            return self.fallback(query=query)
```

### 10.2 ReAct Built-in Error Recovery

ReAct captures tool execution errors as observation strings rather than raising exceptions. The agent can reason about the error and retry:

```python
# Inside ReAct.forward() — errors become observations:
try:
    trajectory[f"observation_{idx}"] = self.tools[pred.next_tool_name](
        **pred.next_tool_args
    )
except Exception as err:
    trajectory[f"observation_{idx}"] = f"Execution error in {pred.next_tool_name}: {err}"
    # Agent sees the error and can choose a different tool or approach
```

### 10.3 Circuit Breaker Pattern

```python
import time

class CircuitBreaker:
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
        return True

    def record_success(self):
        self.failure_count = 0
        self.state = "closed"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"


class ProtectedAgent(dspy.Module):
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

## 11. Production Multi-Agent Deployment

### 11.1 FastAPI Deployment

```python
from fastapi import FastAPI
import dspy
import uvicorn

app = FastAPI()

dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))
system = OrchestratorAgent()
system.load("optimized_system.json")

@app.post("/query")
async def handle_query(request: dict):
    result = system(user_request=request["query"])
    return {"response": result.response}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### 11.2 Async Multi-Agent Execution

```python
import asyncio
import dspy

class AsyncOrchestrator(dspy.Module):
    def __init__(self):
        self.agents = {
            "data": DataExpert(),
            "io": IOExpert(),
            "config": ConfigExpert(),
        }

    async def aforward(self, queries: dict[str, str]) -> dspy.Prediction:
        tasks = [self._run_agent(domain, query) for domain, query in queries.items()
                 if domain in self.agents]
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


async def main():
    orchestrator = AsyncOrchestrator()
    result = await orchestrator.acall(
        queries={"data": "Schema of experiment.h5", "io": "Current I/O throughput"}
    )
    print(result.results)

asyncio.run(main())
```

### 11.3 Per-Request LM Override with dspy.context()

```python
import dspy

dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))
system = OrchestratorAgent()

# Per-request override (thread-safe)
with dspy.context(lm=dspy.LM("openai/gpt-4o")):
    result = system(user_request="Complex analysis task")

# Back to gpt-4o-mini outside the context
result = system(user_request="Simple query")
```

### 11.4 Streaming Multi-Agent Output

```python
import dspy

system = OrchestratorAgent()
stream = dspy.streamify(system)

async for chunk in stream(user_request="Analyze this dataset"):
    if isinstance(chunk, dspy.Prediction):
        print(f"\nFinal: {chunk.response}")
    else:
        print(chunk, end="", flush=True)
```

### 11.5 Custom ReAct Agent (Production Pattern)

For maximum control, build a custom agent loop instead of using `dspy.ReAct`:

```python
class Agent(dspy.Module):
    """Production agent with custom tool dispatch and finish semantics."""
    def __init__(self, max_steps=5):
        self.max_steps = max_steps
        instructions = "For the final answer, produce short answers..."
        signature = dspy.Signature(
            'question, trajectory, functions -> next_selected_fn, args: dict[str, Any]',
            instructions
        )
        self.react = dspy.ChainOfThought(signature)

    def forward(self, question, functions):
        tools = {fn_name: fn_metadata(fn) for fn_name, fn in functions.items()}
        trajectory = []

        for _ in range(self.max_steps):
            pred = self.react(
                question=question, trajectory=trajectory, functions=tools
            )
            selected_fn = pred.next_selected_fn.strip('"').strip("'")
            fn_output = functions[selected_fn](**pred.args)
            trajectory.append(dict(
                reasoning=pred.reasoning,
                selected_fn=selected_fn,
                args=pred.args,
                output=fn_output,
            ))
            if selected_fn == "finish":
                break

        return dspy.Prediction(
            answer=fn_output.get("return_value", ""),
            trajectory=trajectory
        )
```

---

## 12. CLIO Agent Architecture Patterns

### 12.1 Three-Tier Mapping

| CLIO Tier | DSPy Pattern | Role |
|-----------|-------------|------|
| Tier 1: Main Agent | `OrchestratorAgent(dspy.Module)` | Routes user queries to experts |
| Tier 2: Experts | `DataExpert(dspy.Module)` using `dspy.ReAct` | Domain-specific reasoning + tools |
| Tier 3: Nanoagents | `NanoAgent(dspy.Module)` using `dspy.Predict` | Single-task execution |

### 12.2 Registry-Based Routing

```python
class CLIORouter(dspy.Module):
    def __init__(self, registry):
        self.registry = registry
        self.router = dspy.ChainOfThought(
            "query, capabilities: str -> selected_agent: str, reasoning: str"
        )

    def forward(self, query: str) -> dspy.Prediction:
        capabilities = self.registry.get_capability_descriptions()
        routing = self.router(query=query, capabilities=capabilities)
        agent = self.registry.get_agent(routing.selected_agent)
        return agent(query=query)
```

### 12.3 ARC-Integrated Agent Pattern

```python
class ARCAgent(dspy.Module):
    def __init__(self, arc, inner_agent: dspy.Module):
        self.arc = arc
        self.inner = inner_agent

    def forward(self, query: str) -> dspy.Prediction:
        cached = self.arc.get_cached_result(query)
        if cached:
            return dspy.Prediction(**cached)

        import time
        start = time.time()
        result = self.inner(query=query)
        duration_ms = (time.time() - start) * 1000

        self.arc.cache_result(query, result.toDict())
        self.arc.store_invocation({
            "query": query, "duration_ms": duration_ms,
            "success": True, "agent": type(self.inner).__name__,
        })
        return result
```

### 12.4 MCP Gateway with Multi-Agent Backend

```python
from fastmcp import FastMCP
import dspy

mcp_server = FastMCP("CLIO Agent")

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
| Agent-as-Tool | Nested delegation | Function wrapping agent call |
| Stateful | Multi-turn conversations | Custom state + History |
| Cache-first | Repeated queries, expensive agents | ARC integration |
| Manual tools | Fine-grained tool control | dspy.ToolCalls + Predict |

**Optimization Quick Guide:**
- Router modules → MIPROv2 (instruction + demo optimization)
- Tool-using agents → SIMBA (agent-aware, long-horizon)
- End-to-end pipeline → BootstrapFinetune (teacher→student distillation)
- Quality control → dspy.Refine / dspy.BestOfN (iterative improvement)

**Persistence:**
- `program.save("optimized.json")` — save optimized prompts/demos
- `program.load("optimized.json")` — restore from saved state

**See also:**
- [03_MODULES_GUIDE.md](03_MODULES_GUIDE.md) — ReAct, CodeAct, Parallel, Tool APIs
- [04_OPTIMIZATION_GUIDE.md](04_OPTIMIZATION_GUIDE.md) — Full optimizer reference (SIMBA, MIPROv2, BootstrapFinetune parameters)
- [06_ADVANCED_PATTERNS.md](06_ADVANCED_PATTERNS.md) — Tool integration, streaming, async, deployment
