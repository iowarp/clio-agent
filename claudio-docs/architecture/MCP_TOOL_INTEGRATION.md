---
title: "MCP Tool Integration"
category: architecture
priority: high
prerequisites:
  - foundation/03_MODULES_GUIDE.md
  - architecture/CLAUDIO_ARCHITECTURE.md
related:
  - research/ADVANCED_PATTERNS.md
implementation_phase: 2
estimated_reading_time: "40 minutes"
version: "1.0"
---

# MCP Tool Integration

## Tool Wrapper Pattern

```python
def tool_function(param1: str, param2: dict) -> dict:
    """Docstring used by ReAct to decide if tool is relevant."""
    from mcp_client import call_mcp
    
    result = call_mcp("tool_name", "operation", {
        "param1": param1,
        "param2": param2
    })
    return result
```

## Adding Tools to ReAct Agents

```python
class Expert(dspy.Module):
    def __init__(self):
        super().__init__()
        
        # Tools become available to agent
        self.agent = dspy.ReAct(
            signature=ExpertSignature,
            tools=[
                tool_analyze,
                tool_optimize,
                tool_validate,
            ],
            max_iters=5
        )
```

## Available Tools

### Data Tools
- **hdf5_analyze(filepath)** → compression, chunking, datasets
- **hdf5_optimize(filepath, strategy)** → optimized file path
- **adios_convert(input, output_format)** → conversion result
- **parquet_validate(filepath)** → validation report

### HPC Tools  
- **slurm_submit(script, resources)** → job ID
- **slurm_status(job_id)** → job status
- **darshan_analyze(log_file)** → performance analysis
- **mpi_check(code)** → MPI issues

### Analysis Tools
- **plot_create(data, type)** → plot code
- **stats_test(data, test_type)** → test result
- **ml_recommend(data)** → model suggestion

### Research Tools
- **search_papers(query)** → paper list
- **analyze_schema(topic)** → best practices
- **extract_context(topic)** → background info

## Error Handling

```python
def safe_tool_wrapper(func):
    """Wrap tool with error handling."""
    
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # Fallback analysis
            return {
                "error": str(e),
                "fallback_result": analyze_without_tool(*args, **kwargs)
            }
    
    return wrapper
```

## Testing Tools

```python
def test_tool_integration():
    # Create agent with tools
    agent = dspy.ReAct(sig, tools=[test_tool])
    
    # Test calling
    result = agent(input="test")
    
    # Verify tool was used
    assert len(result.trajectory) > 0
    assert "test_tool" in str(result.trajectory)
```

---

Next: [Optimization Strategy](OPTIMIZATION_STRATEGY.md)
