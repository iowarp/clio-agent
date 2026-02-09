# DSPy 3.x Advanced Patterns
> Version: dspy-ai 3.1.3 | Updated: February 2026

Comprehensive guide to advanced DSPy 3.x patterns including tools, streaming, async execution, code generation, and deployment strategies.

---

## Table of Contents

1. [Tool Integration](#1-tool-integration)
2. [Streaming Patterns](#2-streaming-patterns)
3. [Async Execution](#3-async-execution)
4. [Code Generation & Execution](#4-code-generation--execution)
5. [Citations & Attribution](#5-citations--attribution)
6. [Callbacks & Observability](#6-callbacks--observability)
7. [Error Handling](#7-error-handling)
8. [Deployment Patterns](#8-deployment-patterns)
9. [CLIO Integration Patterns](#9-clio-integration-patterns)

---

## 1. Tool Integration

### 1.1 Creating Tools from Functions

DSPy 3.x provides `dspy.Tool` for wrapping functions with automatic schema inference.

**Basic Tool Creation:**
```python
import dspy

def calculate_statistics(data: list[float]) -> dict:
    """Calculate basic statistics for a dataset.

    Args:
        data: List of numerical values

    Returns:
        Dictionary with mean, median, std, min, max
    """
    import statistics
    return {
        "mean": statistics.mean(data),
        "median": statistics.median(data),
        "stdev": statistics.stdev(data) if len(data) > 1 else 0.0,
        "min": min(data),
        "max": max(data),
    }

# Create tool from function - name/description/args inferred from docstring
stats_tool = dspy.Tool(calculate_statistics)

# Use in ReAct agent
agent = dspy.ReAct(
    "question -> answer",
    tools=[stats_tool]
)

result = agent(question="What's the mean of [1, 2, 3, 4, 5]?")
print(result.answer)
```

**Explicit Tool Configuration:**
```python
# Override inferred metadata
custom_tool = dspy.Tool(
    calculate_statistics,
    name="stats_calculator",
    description="Compute statistical measures for numeric datasets",
    args={
        "data": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Numeric values to analyze"
        }
    }
)
```

### 1.2 Async Tools

DSPy 3.x supports async tool execution for I/O-bound operations.

```python
import asyncio
import httpx

async def fetch_data(url: str, timeout: int = 10) -> dict:
    """Fetch JSON data from URL.

    Args:
        url: HTTP endpoint to query
        timeout: Request timeout in seconds

    Returns:
        JSON response as dictionary
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=timeout)
        return response.json()

# Async tool creation
fetch_tool = dspy.Tool(fetch_data)

# Use in async agent
class AsyncDataAgent(dspy.Module):
    def __init__(self):
        super().__init__()
        self.agent = dspy.ReAct("query -> answer", tools=[fetch_tool])

    async def aforward(self, query: str):
        return await self.agent.acall(query=query)

# Execute async agent
async def main():
    agent = AsyncDataAgent()
    result = await agent.aforward("Fetch data from https://api.example.com/data")
    print(result.answer)

asyncio.run(main())
```

### 1.3 MCP Tool Bridge (NEW in DSPy 3.x)

DSPy 3.x introduces `Tool.from_mcp_tool()` for Model Context Protocol integration.

```python
from fastmcp import Tool as MCPTool
import dspy

# Create MCP tool (example: filesystem tool)
mcp_read_file = MCPTool(
    name="read_file",
    description="Read contents of a file",
    parameters={
        "path": {"type": "string", "description": "File path to read"}
    },
    function=lambda path: open(path).read()
)

# Bridge to DSPy tool
dspy_read_file = dspy.Tool.from_mcp_tool(mcp_read_file)

# Use in agent
file_agent = dspy.ReAct(
    "question, file_path -> answer",
    tools=[dspy_read_file]
)

result = file_agent(
    question="What's in the config file?",
    file_path="/etc/config.yaml"
)
```

**MCP Server Integration:**
```python
from fastmcp import FastMCP
import dspy

# Connect to MCP server
mcp_server = FastMCP("Scientific Data Tools")

# Register MCP tools
@mcp_server.tool()
def read_hdf5(filepath: str, dataset: str) -> list:
    """Read dataset from HDF5 file."""
    import h5py
    with h5py.File(filepath, 'r') as f:
        return f[dataset][:].tolist()

@mcp_server.tool()
def write_parquet(data: list[dict], filepath: str) -> str:
    """Write data to Parquet file."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.Table.from_pylist(data)
    pq.write_table(table, filepath)
    return f"Written {len(data)} rows to {filepath}"

# Bridge all MCP tools to DSPy
dspy_tools = [
    dspy.Tool.from_mcp_tool(tool)
    for tool in mcp_server.list_tools()
]

# Scientific data agent with MCP tools
data_agent = dspy.ReAct(
    "question -> answer",
    tools=dspy_tools
)
```

### 1.4 LangChain Tool Bridge

DSPy can integrate with existing LangChain tools.

```python
from langchain.tools import Tool as LangChainTool
import dspy

# Existing LangChain tool
lc_tool = LangChainTool(
    name="calculator",
    description="Calculate mathematical expressions",
    func=lambda expr: eval(expr)  # Don't do this in production!
)

# Bridge to DSPy
dspy_calc = dspy.Tool.from_langchain(lc_tool)

# Use in DSPy agent
calc_agent = dspy.ReAct(
    "math_problem -> solution",
    tools=[dspy_calc]
)

result = calc_agent(math_problem="What is 15 * 23 + 47?")
```

### 1.5 ToolCalls Output Handling

DSPy 3.x provides `dspy.ToolCalls` for structured tool execution results.

```python
import dspy

class DataAnalysisAgent(dspy.Module):
    def __init__(self, tools: list[dspy.Tool]):
        super().__init__()
        self.agent = dspy.ReAct(
            "dataset_path, question -> answer",
            tools=tools
        )

    def forward(self, dataset_path: str, question: str):
        result = self.agent(dataset_path=dataset_path, question=question)

        # Access tool calls from result
        if hasattr(result, "tool_calls"):
            tool_calls: dspy.ToolCalls = result.tool_calls

            # Execute all tool calls
            for call in tool_calls:
                print(f"Tool: {call.tool_name}")
                print(f"Args: {call.args}")

                # Execute tool call
                output = tool_calls.execute(call)
                print(f"Output: {output}")

        return result.answer

# Usage
tools = [
    dspy.Tool(read_hdf5),
    dspy.Tool(calculate_statistics),
]
agent = DataAnalysisAgent(tools)
answer = agent(
    dataset_path="/data/experiment.h5",
    question="What's the mean temperature?"
)
```

### 1.6 Native Function Calling vs Prompt-Based

DSPy 3.x supports both native function calling (for models like GPT-4, Claude) and prompt-based tool use.

```python
import dspy

# Configure model with native function calling
lm_native = dspy.LM(
    model="openai/gpt-4-turbo",
    api_key="...",
    tool_calling="native"  # Use model's native function calling
)

# Configure model with prompt-based tools (for models without native support)
lm_prompt = dspy.LM(
    model="anthropic/claude-3-sonnet",
    api_key="...",
    tool_calling="prompt"  # Inject tool schemas into prompt
)

# Agent automatically adapts to LM capabilities
dspy.configure(lm=lm_native)  # or lm_prompt
agent = dspy.ReAct("question -> answer", tools=[stats_tool])
```

---

## 2. Streaming Patterns

### 2.1 Streaming with dspy.streamify()

DSPy 3.x provides `dspy.streamify()` to add streaming to any module.

```python
import dspy

class SummaryAgent(dspy.Module):
    def __init__(self):
        super().__init__()
        self.summarizer = dspy.ChainOfThought("document -> summary")

    def forward(self, document: str):
        return self.summarizer(document=document)

# Add streaming capability
agent = SummaryAgent()
streaming_agent = dspy.streamify(agent)

# Stream output
for chunk in streaming_agent(document="Long document text..."):
    print(chunk, end="", flush=True)
```

### 2.2 StreamListener for Custom Processing

Use `StreamListener` for custom streaming behavior.

```python
import dspy
from dspy.streaming import StreamListener

class ProgressListener(StreamListener):
    def __init__(self):
        self.tokens = 0
        self.start_time = time.time()

    def on_chunk(self, chunk: str):
        """Called for each streamed chunk."""
        self.tokens += len(chunk.split())
        print(f"\r[{self.tokens} tokens | {time.time() - self.start_time:.1f}s]", end="")

    def on_complete(self, full_text: str):
        """Called when streaming completes."""
        print(f"\nCompleted: {self.tokens} tokens")

    def on_error(self, error: Exception):
        """Called on streaming error."""
        print(f"\nError: {error}")

# Use listener
listener = ProgressListener()
streaming_agent = dspy.streamify(agent, listener=listener)

for chunk in streaming_agent(document="..."):
    pass  # Listener handles output
```

### 2.3 StatusMessageProvider

Provide status updates during long-running operations.

```python
import dspy
from dspy.streaming import StatusMessageProvider

class DataProcessingAgent(dspy.Module, StatusMessageProvider):
    def __init__(self):
        super().__init__()
        self.analyzer = dspy.ChainOfThought("data -> analysis")

    def forward(self, data: list):
        # Emit status messages
        self.emit_status("Loading data...")
        processed = self.preprocess(data)

        self.emit_status("Analyzing with LLM...")
        result = self.analyzer(data=str(processed))

        self.emit_status("Complete!")
        return result

    def preprocess(self, data: list):
        # Simulate preprocessing
        time.sleep(2)
        return [x * 2 for x in data]

# Stream with status updates
agent = dspy.streamify(DataProcessingAgent())

for update in agent(data=[1, 2, 3, 4, 5]):
    if isinstance(update, str):
        print(f"Status: {update}")
    else:
        print(f"Result: {update}")
```

### 2.4 OpenAI-Compatible Streaming

Use `streaming_response()` for API endpoints.

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import dspy

app = FastAPI()
agent = dspy.streamify(SummaryAgent())

@app.post("/summarize")
async def summarize(document: str):
    def generate():
        for chunk in agent(document=document):
            # OpenAI-compatible format
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## 3. Async Execution

### 3.1 Async Module Methods

DSPy 3.x supports async execution with `acall()` and `aforward()`.

```python
import asyncio
import dspy

class AsyncRAGAgent(dspy.Module):
    def __init__(self):
        super().__init__()
        self.query_gen = dspy.ChainOfThought("question -> search_query")
        self.answerer = dspy.ChainOfThought("question, context -> answer")

    async def aforward(self, question: str):
        # Generate query asynchronously
        query_result = await self.query_gen.acall(question=question)

        # Simulate async retrieval
        context = await self.retrieve(query_result.search_query)

        # Generate answer asynchronously
        answer_result = await self.answerer.acall(
            question=question,
            context=context
        )

        return answer_result.answer

    async def retrieve(self, query: str) -> str:
        # Async retrieval logic
        await asyncio.sleep(0.1)
        return f"Context for: {query}"

# Execute async agent
async def main():
    agent = AsyncRAGAgent()
    answer = await agent.aforward("What is quantum computing?")
    print(answer)

asyncio.run(main())
```

### 3.2 dspy.asyncify() for Sync Functions

Convert synchronous modules to async.

```python
import dspy

# Synchronous module
class SyncAgent(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predictor = dspy.ChainOfThought("question -> answer")

    def forward(self, question: str):
        return self.predictor(question=question).answer

# Convert to async
sync_agent = SyncAgent()
async_agent = dspy.asyncify(sync_agent)

# Use async
async def main():
    result = await async_agent.aforward(question="What is AI?")
    print(result)

asyncio.run(main())
```

### 3.3 Concurrent Execution with async_max_workers

Control concurrency for async operations.

```python
import dspy

# Configure async concurrency
dspy.configure(
    lm=dspy.LM("openai/gpt-4"),
    async_max_workers=10  # Max concurrent LM calls
)

class ParallelAnalyzer(dspy.Module):
    def __init__(self):
        super().__init__()
        self.analyzer = dspy.Predict("text -> sentiment: str, score: float")

    async def analyze_batch(self, texts: list[str]) -> list[dict]:
        # Run multiple analyses concurrently (respects async_max_workers)
        tasks = [
            self.analyzer.acall(text=text)
            for text in texts
        ]
        results = await asyncio.gather(*tasks)
        return [{"sentiment": r.sentiment, "score": r.score} for r in results]

# Process 100 texts with max 10 concurrent calls
async def main():
    analyzer = ParallelAnalyzer()
    texts = [f"Sample text {i}" for i in range(100)]
    results = await analyzer.analyze_batch(texts)
    print(f"Analyzed {len(results)} texts")

asyncio.run(main())
```

---

## 4. Code Generation & Execution

### 4.1 dspy.PythonInterpreter - Sandboxed Execution

DSPy 3.x includes sandboxed code execution using Deno.

```python
import dspy

# Create sandboxed Python interpreter
interpreter = dspy.PythonInterpreter(
    timeout=30,  # Execution timeout
    memory_limit="512MB",
    allow_network=False,
    allowed_imports=["math", "statistics", "json"]
)

# Execute code safely
code = """
import math

def calculate_area(radius):
    return math.pi * radius ** 2

result = calculate_area(5)
"""

output = interpreter.execute(code)
print(output["result"])  # 78.53981633974483
```

**Error Handling:**
```python
try:
    # This will fail - imports not allowed
    interpreter.execute("import os; os.system('ls')")
except dspy.PythonInterpreterError as e:
    print(f"Execution failed: {e}")
```

### 4.2 Tool Injection in Interpreter

Inject DSPy tools into the execution environment.

```python
import dspy

def read_data(filepath: str) -> list:
    """Read data from file."""
    with open(filepath) as f:
        return [line.strip() for line in f]

def calculate_stats(data: list[float]) -> dict:
    """Calculate statistics."""
    import statistics
    return {
        "mean": statistics.mean(data),
        "stdev": statistics.stdev(data)
    }

# Create interpreter with injected tools
interpreter = dspy.PythonInterpreter(
    tools=[
        dspy.Tool(read_data),
        dspy.Tool(calculate_stats)
    ]
)

# Tools are available in execution context
code = """
# Read data file
lines = read_data('data.txt')
numbers = [float(x) for x in lines]

# Calculate statistics
stats = calculate_stats(numbers)
result = f"Mean: {stats['mean']}, StdDev: {stats['stdev']}"
"""

output = interpreter.execute(code)
print(output["result"])
```

### 4.3 dspy.Code["python"] - Code Generation

Generate and execute code with LLM.

```python
import dspy

class CodeGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generator = dspy.Code["python"](
            "problem -> code: str",
            interpreter=dspy.PythonInterpreter()
        )

    def forward(self, problem: str):
        result = self.generator(problem=problem)
        return {
            "code": result.code,
            "output": result.execution_output
        }

# Generate code to solve problem
agent = CodeGenerator()
result = agent(problem="Calculate factorial of 10")

print("Generated code:")
print(result["code"])
print("\nOutput:", result["output"])
```

**Multi-Language Support:**
```python
# JavaScript code generation
js_generator = dspy.Code["javascript"](
    "task -> code: str",
    interpreter=dspy.JavaScriptInterpreter()
)

# SQL generation
sql_generator = dspy.Code["sql"](
    "schema, question -> query: str"
)
```

---

## 5. Citations & Attribution

### 5.1 dspy.experimental.Citations

DSPy 3.x supports Anthropic's Citations API for source attribution.

```python
import dspy
from dspy.experimental import Citations

class CitedRAG(dspy.Module):
    def __init__(self):
        super().__init__()
        self.answerer = Citations(
            "question, context: list[str] -> answer: str, citations: list[dict]"
        )

    def forward(self, question: str, documents: list[str]):
        result = self.answerer(
            question=question,
            context=documents
        )

        return {
            "answer": result.answer,
            "citations": result.citations  # [{"text": "...", "source": idx}]
        }

# Configure with Claude
dspy.configure(lm=dspy.LM("anthropic/claude-3-opus"))

agent = CitedRAG()
result = agent(
    question="What is photosynthesis?",
    documents=[
        "Photosynthesis is the process by which plants convert light to energy.",
        "Chloroplasts are the organelles where photosynthesis occurs.",
        "The light-dependent reactions occur in the thylakoid membranes."
    ]
)

print(f"Answer: {result['answer']}")
print("Citations:")
for cite in result['citations']:
    print(f"  - {cite['text']} (source {cite['source']})")
```

---

## 6. Callbacks & Observability

### 6.1 Custom Callback System

Register callbacks for LM calls, tool executions, and errors.

```python
import dspy
from dspy.callbacks import Callback

class MetricsCallback(Callback):
    def __init__(self):
        self.lm_calls = 0
        self.tool_calls = 0
        self.total_tokens = 0

    def on_lm_call_start(self, module: str, inputs: dict):
        """Called before LM call."""
        self.lm_calls += 1
        print(f"[{self.lm_calls}] Calling {module} with inputs: {inputs}")

    def on_lm_call_end(self, module: str, outputs: dict, tokens: int):
        """Called after LM call."""
        self.total_tokens += tokens
        print(f"  → Output: {outputs} ({tokens} tokens)")

    def on_tool_call(self, tool: str, args: dict, result: any):
        """Called after tool execution."""
        self.tool_calls += 1
        print(f"[Tool] {tool}({args}) = {result}")

    def on_error(self, error: Exception, context: dict):
        """Called on error."""
        print(f"[ERROR] {error} in {context}")

# Register callback
metrics = MetricsCallback()
dspy.configure(callbacks=[metrics])

# Run agent - callback tracks everything
agent = dspy.ReAct("question -> answer", tools=[stats_tool])
result = agent(question="What's the mean of [1, 2, 3]?")

print(f"\nMetrics: {metrics.lm_calls} LM calls, {metrics.tool_calls} tool calls")
```

### 6.2 Integration with Observability Platforms

```python
import dspy
from dspy.callbacks import LangfuseCallback, MLflowCallback

# Langfuse observability
langfuse_cb = LangfuseCallback(
    public_key="...",
    secret_key="...",
    host="https://cloud.langfuse.com"
)

# MLflow tracking
mlflow_cb = MLflowCallback(
    tracking_uri="http://localhost:5000",
    experiment_name="clio-agent"
)

dspy.configure(callbacks=[langfuse_cb, mlflow_cb])
```

---

## 7. Error Handling

### 7.1 Graceful Degradation

Handle errors with fallback strategies.

```python
import dspy

class RobustAgent(dspy.Module):
    def __init__(self):
        super().__init__()
        self.primary = dspy.ChainOfThought("question -> answer")
        self.fallback = dspy.Predict("question -> answer")

    def forward(self, question: str):
        try:
            # Try primary (more complex)
            result = self.primary(question=question)
            return result.answer
        except dspy.LMError as e:
            # Fall back to simpler approach
            print(f"Primary failed: {e}, using fallback")
            result = self.fallback(question=question)
            return result.answer
        except Exception as e:
            # Ultimate fallback
            return f"Error: Unable to process question ({e})"

agent = RobustAgent()
answer = agent(question="Complex query...")
```

### 7.2 Retry Logic with Exponential Backoff

```python
import dspy
import time

class RetryableAgent(dspy.Module):
    def __init__(self, max_retries: int = 3):
        super().__init__()
        self.predictor = dspy.Predict("question -> answer")
        self.max_retries = max_retries

    def forward(self, question: str):
        for attempt in range(self.max_retries):
            try:
                result = self.predictor(question=question)
                return result.answer
            except dspy.RateLimitError as e:
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    print(f"Rate limited, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    raise
```

---

## 8. Deployment Patterns

### 8.1 Save & Load Compiled Programs

```python
import dspy

# Train and compile agent
agent = SummaryAgent()
optimizer = dspy.MIPROv2(metric=quality_metric)
compiled_agent = optimizer.compile(agent, trainset=data)

# Save compiled program
compiled_agent.save("models/summary_agent_v1.json")

# Load in production
production_agent = SummaryAgent()
production_agent.load("models/summary_agent_v1.json")

# Use loaded agent
result = production_agent(document="...")
```

### 8.2 FastAPI Deployment

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import dspy

app = FastAPI()

# Load compiled agent at startup
agent = None

@app.on_event("startup")
def load_agent():
    global agent
    agent = SummaryAgent()
    agent.load("models/summary_agent_v1.json")
    dspy.configure(lm=dspy.LM("openai/gpt-4"))

class SummaryRequest(BaseModel):
    document: str
    max_length: int = 100

class SummaryResponse(BaseModel):
    summary: str
    tokens_used: int

@app.post("/summarize", response_model=SummaryResponse)
async def summarize(request: SummaryRequest):
    try:
        result = await agent.aforward(document=request.document)
        return SummaryResponse(
            summary=result,
            tokens_used=agent.lm.get_usage()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Run: uvicorn app:app --host 0.0.0.0 --port 8000
```

### 8.3 Streaming API Endpoint

```python
from fastapi.responses import StreamingResponse
import dspy

streaming_agent = dspy.streamify(SummaryAgent())

@app.post("/summarize/stream")
async def summarize_stream(request: SummaryRequest):
    async def generate():
        try:
            for chunk in streaming_agent(document=request.document):
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## 9. CLIO Integration Patterns

### 9.1 MCP Tools for Scientific Data

CLIO Agent integrates DSPy with MCP tools for scientific data operations.

```python
import dspy
from fastmcp import FastMCP

# Define MCP server for scientific tools
scientific_mcp = FastMCP("Scientific Data Tools")

@scientific_mcp.tool()
def read_hdf5(filepath: str, dataset: str) -> list:
    """Read dataset from HDF5 file."""
    import h5py
    with h5py.File(filepath, 'r') as f:
        return f[dataset][:].tolist()

@scientific_mcp.tool()
def analyze_timeseries(data: list[float], window: int = 10) -> dict:
    """Analyze time series data."""
    import numpy as np
    data_arr = np.array(data)
    return {
        "mean": float(np.mean(data_arr)),
        "trend": "increasing" if data_arr[-1] > data_arr[0] else "decreasing",
        "moving_avg": np.convolve(data_arr, np.ones(window)/window, mode='valid').tolist()
    }

@scientific_mcp.tool()
def write_parquet(data: list[dict], filepath: str) -> str:
    """Write data to Parquet format."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.Table.from_pylist(data)
    pq.write_table(table, filepath, compression='snappy')
    return f"Written {len(data)} rows to {filepath}"

# Bridge all MCP tools to DSPy
scientific_tools = [
    dspy.Tool.from_mcp_tool(tool)
    for tool in scientific_mcp.list_tools()
]

# Create CLIO DataExpert with MCP tools
class DataExpert(dspy.Module):
    """Expert agent for scientific data operations."""

    def __init__(self):
        super().__init__()
        self.agent = dspy.ReAct(
            "question -> answer",
            tools=scientific_tools,
            max_iters=10
        )

    def forward(self, question: str):
        result = self.agent(question=question)
        return result.answer

# Usage
expert = DataExpert()
answer = expert(question="Read /data/experiment.h5 dataset 'temperature' and analyze the trend")
```

### 9.2 ARC Memory Integration

Integrate tool results with ARC Memory for caching.

```python
import dspy
from clio_agent.arc.memory import ARCMemory

class CachedToolExpert(dspy.Module):
    def __init__(self, tools: list[dspy.Tool], arc: ARCMemory):
        super().__init__()
        self.tools = tools
        self.arc = arc
        self.agent = dspy.ReAct("question -> answer", tools=tools)

    def forward(self, question: str):
        # Check ARC cache for similar tool results
        cached = self.arc.get_cached_tool_result(question)
        if cached:
            return cached["result"]

        # Execute agent with tools
        result = self.agent(question=question)

        # Cache tool results in ARC
        if hasattr(result, "tool_calls"):
            for call in result.tool_calls:
                self.arc.cache_tool_result(
                    tool=call.tool_name,
                    params=call.args,
                    result=call.output
                )

        # Cache final answer
        self.arc.cache_tool_result("answer", {"question": question}, result.answer)

        return result.answer

# Usage with ARC
arc = ARCMemory(cache_dir="/data/arc")
expert = CachedToolExpert(tools=scientific_tools, arc=arc)

# First call - executes tools
answer1 = expert(question="Analyze /data/exp1.h5")

# Second call - hits cache
answer2 = expert(question="Analyze /data/exp1.h5")  # Much faster!
```

### 9.3 3-Tier Architecture Pattern

CLIO's hierarchical agent structure.

```python
import dspy

# Tier 3: Nanoagents (specific tools)
class HDF5Reader(dspy.Module):
    def __init__(self):
        super().__init__()
        self.reader = dspy.Tool(read_hdf5)

    def forward(self, filepath: str, dataset: str):
        return self.reader(filepath=filepath, dataset=dataset)

# Tier 2: Expert agents (domain specialists)
class DataExpert(dspy.Module):
    def __init__(self):
        super().__init__()
        self.agent = dspy.ReAct(
            "question -> answer",
            tools=scientific_tools
        )

    def forward(self, question: str):
        return self.agent(question=question).answer

class AnalysisExpert(dspy.Module):
    def __init__(self):
        super().__init__()
        self.analyzer = dspy.ChainOfThought(
            "data, question -> analysis: str, insights: list[str]"
        )

    def forward(self, data: list, question: str):
        result = self.analyzer(data=str(data), question=question)
        return {
            "analysis": result.analysis,
            "insights": result.insights
        }

# Tier 1: Main orchestration agent
class ClioAgent(dspy.Module):
    def __init__(self):
        super().__init__()

        # Wrap experts as tools
        self.data_expert = DataExpert()
        self.analysis_expert = AnalysisExpert()

        # Main agent with expert tools
        self.main_agent = dspy.ReAct(
            "question -> answer",
            tools=[
                dspy.Tool(
                    lambda q: self.data_expert(q),
                    name="data_expert",
                    description="Expert for data I/O operations (HDF5, Parquet, NetCDF)"
                ),
                dspy.Tool(
                    lambda data, q: self.analysis_expert(data, q),
                    name="analysis_expert",
                    description="Expert for data analysis and insights"
                )
            ]
        )

    def forward(self, question: str):
        result = self.main_agent(question=question)
        return result.answer

# Usage
clio = ClioAgent()
answer = clio(question="Load experiment data and analyze temperature trends")
```

---

## Summary

DSPy 3.x advanced patterns enable:

1. **Tool Integration**: MCP bridge, LangChain bridge, async tools
2. **Streaming**: Real-time output, status updates, API endpoints
3. **Async Execution**: High-throughput, concurrent operations
4. **Code Generation**: Sandboxed execution, tool injection
5. **Citations**: Source attribution for RAG systems
6. **Observability**: Callbacks, metrics, platform integration
7. **Error Handling**: Graceful degradation, retries
8. **Deployment**: Save/load, FastAPI, streaming APIs
9. **CLIO Integration**: MCP tools, ARC memory, 3-tier architecture

These patterns enable production-ready, observable, and maintainable AI systems for scientific data processing.
