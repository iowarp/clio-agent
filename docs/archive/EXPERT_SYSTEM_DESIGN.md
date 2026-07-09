---
title: "Multi-Expert Orchestration Design"
category: architecture
priority: high
prerequisites:
  - foundation/03_MODULES_GUIDE.md
  - architecture/CLIO_AGENT_ARCHITECTURE.md
related:
  - research/MULTI_AGENT_SYSTEMS.md
  - poc/experts.py
implementation_phase: 1|2
estimated_reading_time: "45 minutes"
version: "1.0"
---

# Multi-Expert Orchestration

> Historical design note: this document predates #629. Production CLIO no
> longer implements domain experts as privileged Python classes such as
> `DataExpert` or `AnalysisExpert`. Current expert behavior is loaded from
> registry Agent Blueprints and compiled through the DSPy Blueprint runtime
> described in `DSPY_BLUEPRINT_EXPERT_RUNTIME.md`.

## Expert Architecture Pattern

```python
class Expert(dspy.Module):
    """Base expert pattern."""

    def __init__(self, tools):
        super().__init__()
        self.agent = dspy.ReAct(
            signature=self.get_signature(),
            tools=tools,
            max_iters=5
        )

    def get_signature(self):
        raise NotImplementedError

    def forward(self, task):
        return self.agent(task=task)
```

## Five Core Experts

### 1. DataExpert
HDF5, ADIOS, Parquet analysis and optimization

**Tools**: hdf5_analyze, hdf5_optimize, adios_convert, parquet_validate

**Signature**: task → analysis, recommendations, mcp_commands

**Success Metric**: Optimization recommendations reduce file size by >10%

### 2. HPCExpert  
SLURM, MPI, performance analysis

**Tools**: slurm_submit, job_status, darshan_analyze, mpi_check

**Signature**: task → job_plan, resource_estimate, optimization_tips

**Success Metric**: Recommended resources accurate within 20%

### 3. AnalysisExpert
Visualization, statistics, machine learning

**Tools**: plot_data, statistical_test, ml_recommend

**Signature**: task → analysis, visualization_code, statistical_result

**Success Metric**: Visualizations clearly communicate patterns

### 4. ResearchExpert
Scientific paper analysis, citations, context

**Tools**: search_papers, analyze_schema, extract_context

**Signature**: task → relevant_papers, key_concepts, context

**Success Metric**: Retrieves highly relevant papers in top 3 results

### 5. WorkflowExpert
Automation, pipeline orchestration, process improvement

**Tools**: generate_pipeline, schedule_workflow, auto_optimize

**Signature**: task → workflow_plan, optimization_opportunities

**Success Metric**: Proposed workflows save >20% execution time

## Orchestrator Pattern

```python
class Orchestrator(dspy.Module):
    def __init__(self):
        super().__init__()
        
        # Learning-based routing
        self.router = dspy.ChainOfThought(
            "task, available_experts -> expert_choice, reasoning"
        )
        
        self.experts = {
            "data": DataExpert(data_tools),
            "hpc": HPCExpert(hpc_tools),
            # ... others
        }

    def forward(self, task):
        # Route decision
        routing = self.router(
            task=task,
            available_experts=list(self.experts.keys())
        )

        # Delegate
        expert = self.experts[routing.expert_choice]
        return expert(task=task)
```

## Tool Assignment Strategy

**Data-related tasks** → DataExpert:
- "Optimize my HDF5 file"
- "Convert ADIOS to Parquet"
- "Check compression ratios"

**HPC-related tasks** → HPCExpert:
- "Submit SLURM job"
- "Analyze MPI performance"
- "Estimate resources needed"

**Analysis tasks** → AnalysisExpert:
- "Visualize this data"
- "Run statistical test"
- "Recommend ML model"

**Research tasks** → ResearchExpert:
- "Find papers about..."
- "What are best practices for..."
- "Explain this technique"

**Workflow tasks** → WorkflowExpert:
- "Automate this process"
- "Optimize pipeline"
- "Schedule job sequence"

## Expert Collaboration

Multi-expert tasks route sequentially:

```
"Generate visualization of HPC performance"
    ↓
Orchestrator: Needs analysis + visualization
    ├→ HPCExpert: Analyze performance
    ├→ AnalysisExpert: Create visualization
    └→ Combined result
```

## Optimization Per Expert

Each expert optimized independently:

```
Orchestrator learns:
- When to route to data expert (90%+ accuracy)

DataExpert learns:
- Best compression recommendations

HPCExpert learns:
- Accurate resource estimation

All independently improve from their specific examples
```

---

See [CLIO_AGENT_ARCHITECTURE](../CLIO_AGENT_ARCHITECTURE.md) for system overview.
