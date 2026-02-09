# DSPy 3.x Modules Guide
> Version: dspy-ai 3.1.3 | Updated: February 2026

## Table of Contents

1. [Overview](#overview)
2. [Module Base Class](#module-base-class)
3. [dspy.Predict](#dspypredict)
4. [dspy.ChainOfThought](#dspychainofthought)
5. [dspy.ReAct](#dspyreact)
6. [dspy.CodeAct](#dspycodeact)
7. [dspy.ProgramOfThought](#dspyprogramofthought)
8. [dspy.RLM](#dspyrlm)
9. [dspy.BestOfN](#dspybestofn)
10. [dspy.Refine](#dspyrefine)
11. [dspy.Parallel](#dspyparallel)
12. [dspy.MultiChainComparison](#dspymultichaincomparison)
13. [Custom Modules](#custom-modules)
14. [Module Methods](#module-methods)
15. [CLIO Agent Usage](#clio-agent-usage)

---

## Overview

Modules are the core building blocks of DSPy programs. They encapsulate LM interactions, reasoning patterns, and tool usage in reusable, composable components.

**Key Concepts:**
- All modules inherit from `dspy.Module`
- Modules implement a `forward()` method for processing
- Modules can be nested and composed
- Modules support batch processing, serialization, and optimization
- Modules track history and usage metrics

---

## Module Base Class

### `dspy.Module`

Base class for all DSPy modules.

```python
class dspy.Module:
    def __init__(self, callbacks=None):
        self.callbacks = callbacks or []
        self._compiled = False
        self.history = []
```

### Core Methods

| Method | Purpose |
|--------|---------|
| `__call__(*args, **kwargs)` | Wrapped forward pass with callbacks and usage tracking |
| `acall(*args, **kwargs)` | Async variant |
| `forward(**kwargs)` | Override this in subclasses |
| `aforward(**kwargs)` | Async forward override |
| `named_parameters()` | Returns (name, Parameter) tuples recursively |
| `parameters()` | Flat list of all parameters |
| `named_predictors()` | Only Predict instances with names |
| `predictors()` | Flat list of Predict instances |
| `get_lm()` | Get the LM (errors if multiple distinct LMs) |
| `set_lm(lm)` | Set LM on all predictors |
| `save(path, save_program=False)` | Save to .json/.pkl or full program via cloudpickle |
| `load(path, allow_pickle=False)` | Restore from saved state |
| `dump_state(json_mode=True)` | Serialize state as dict |
| `load_state(state)` | Restore from state dict |
| `batch(examples, num_threads=None, max_errors=None, ...)` | Parallel processing |
| `deepcopy()` | Deep copy with graceful fallback |
| `reset_copy()` | Deep copy + reset all parameters |
| `inspect_history(n=1)` | Display LM calling history |
| `named_sub_modules(type_=None, skip_compiled=False)` | Generator over sub-modules |
| `map_named_predictors(func)` | Apply function to each predictor |

### Custom Module Pattern

```python
import dspy

class MyModule(dspy.Module):
    def __init__(self):
        super().__init__()
        self.step1 = dspy.ChainOfThought("question -> reasoning")
        self.step2 = dspy.Predict("reasoning -> answer")

    def forward(self, question):
        reasoning_result = self.step1(question=question)
        return self.step2(reasoning=reasoning_result.reasoning)

# Usage
module = MyModule()
result = module(question="What is DSPy?")
print(result.answer)
```

---

## dspy.Predict

Basic prediction module that processes inputs through a signature.

### Constructor

```python
dspy.Predict(
    signature: str | type[Signature],
    callbacks: list[BaseCallback] | None = None,
    **config
)
```

### Methods

| Method | Purpose |
|--------|---------|
| `forward(**kwargs)` | Process through LM adapter |
| `aforward(**kwargs)` | Async forward |
| `batch(examples, num_threads=None, max_errors=None, ...)` | Parallel batch |
| `dump_state(json_mode=True)` | Serialize traces, demos, signature, LM state |
| `load_state(state)` | Restore state |
| `reset()` | Clear LM, traces, training, demos |
| `get_config()` | Return config dict |
| `update_config(**kwargs)` | Merge new settings |
| `save(path, save_program=False)` | Export JSON/pickle |
| `load(path, allow_pickle=False)` | Restore |

### Basic Usage

```python
# Inline signature
classify = dspy.Predict('sentence -> sentiment: bool')
response = classify(sentence="This is great")
print(response.sentiment)

# Class-based signature
class QASignature(dspy.Signature):
    """Answer questions concisely."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="Short factual answer")

qa = dspy.Predict(QASignature)
result = qa(question="What is the capital of France?")
print(result.answer)
```

### Multiple Completions

```python
# Generate N completions
classifier = dspy.Predict('question -> answer', n=5)
response = classifier(question="What is 1 + 52?")

# Access all completions
print(response.completions.answer)  # list of 5 answers
print(response.answer)  # First answer
```

### Per-Call Configuration

```python
predict = dspy.Predict("question -> answer")

# Override config per call
result = predict(
    question="What is DSPy?",
    config={"rollout_id": 5, "temperature": 1.0}
)
```

---

## dspy.ChainOfThought

Adds step-by-step reasoning before producing outputs.

### Constructor

```python
dspy.ChainOfThought(
    signature: str | type[Signature],
    rationale_field: FieldInfo | None = None,
    rationale_field_type: type = str,
    **config
)
```

### How It Works

ChainOfThought automatically prepends a `reasoning` field to the signature:
- **Prefix:** "Reasoning: Let's think step by step in order to"
- **Description:** "${reasoning}"
- **Position:** Before output fields

### Basic Usage

```python
# Simple inline signature
generate_answer = dspy.ChainOfThought("question -> answer")
pred = generate_answer(question='What is the color of the sky?')

print(pred.reasoning)  # Step-by-step reasoning
print(pred.answer)     # Final answer
```

### Class-Based Signature

```python
class ComplexReasoning(dspy.Signature):
    """Solve complex problems with detailed reasoning."""

    problem: str = dspy.InputField()
    context: str = dspy.InputField(default="")
    solution: str = dspy.OutputField()
    confidence: float = dspy.OutputField()

solver = dspy.ChainOfThought(ComplexReasoning)
result = solver(problem="Calculate the sum of prime numbers under 20")

print(result.reasoning)   # Shows reasoning steps
print(result.solution)    # Final solution
print(result.confidence)  # Confidence score
```

### Custom Rationale Field

```python
# Customize the reasoning field
cot = dspy.ChainOfThought(
    "question -> answer",
    rationale_field=dspy.OutputField(
        prefix="Detailed Analysis:",
        desc="Comprehensive step-by-step breakdown"
    )
)
```

---

## dspy.ReAct

Reasoning and Acting module that combines thinking with tool usage.

### Constructor

```python
dspy.ReAct(
    signature: type[Signature],
    tools: list[Callable],
    max_iters: int = 20
)
```

**Parameters:**
- `signature`: Input/output definition
- `tools`: List of functions, callables, or `dspy.Tool` instances
- `max_iters`: Maximum iteration count (default: 20)

### Methods

- `forward(**kwargs)`: Synchronous agent loop
- `aforward(**kwargs)`: Async agent loop (required for MCP tools)
- `truncate_trajectory()`: Remove oldest tool calls when context window exceeded

### Trajectory Structure

Each iteration appends:
- `thought_{idx}`: Reasoning step
- `tool_name_{idx}`: Selected tool
- `tool_args_{idx}`: Tool arguments (JSON)
- `observation_{idx}`: Tool result or error

Built-in "finish" tool automatically added.

### Basic Example

```python
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    # Mock implementation
    return f"The weather in {city} is sunny and 75F"

def calculate(expression: str) -> float:
    """Evaluate a mathematical expression."""
    return eval(expression)

react = dspy.ReAct(
    "question -> answer",
    tools=[get_weather, calculate],
    max_iters=5
)

result = react(question="What's the weather in Tokyo and what's 15 * 3?")
print(result.answer)
print(result.trajectory)  # Full reasoning and tool usage trace
```

### Async Tools with MCP

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server_params = StdioServerParameters(
    command="python",
    args=["path_to_mcp_server.py"],
)

async def run(user_request):
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

            # Convert MCP tools to DSPy tools
            dspy_tools = []
            for tool in tools.tools:
                dspy_tools.append(dspy.Tool.from_mcp_tool(session, tool))

            react = dspy.ReAct("user_request -> response", tools=dspy_tools)

            # Must use acall for async MCP tools
            result = await react.acall(user_request=user_request)
            print(result)
```

### Custom Tool Definition

```python
# Define tools with proper docstrings and type hints
def search_database(query: str, limit: int = 10) -> str:
    """Search the knowledge database.

    Args:
        query: Search query string
        limit: Maximum number of results to return

    Returns:
        Formatted search results
    """
    # Implementation
    return f"Found {limit} results for '{query}'"

# Wrap as DSPy Tool for more control
search_tool = dspy.Tool(
    func=search_database,
    name="database_search",
    desc="Search the knowledge database for information",
    arg_desc={
        "query": "The search query",
        "limit": "Maximum results (default: 10)"
    }
)

agent = dspy.ReAct("task -> result", tools=[search_tool], max_iters=10)
```

---

## dspy.CodeAct

Generates and executes Python code in a sandboxed environment.

### Constructor

```python
dspy.CodeAct(
    signature: str | type[Signature],
    tools: list[Callable],
    max_iters: int = 5,
    interpreter: PythonInterpreter | None = None
)
```

### Limitations

- Only accepts functions (not callable objects/classes)
- Cannot use third-party packages (numpy, etc.) within tool functions
- All dependent functions must be passed explicitly
- Tools must be pure, self-contained functions

### Basic Example

```python
def factorial(n: int) -> int:
    """Calculate factorial of n."""
    if n == 1:
        return 1
    return n * factorial(n-1)

def fibonacci(n: int) -> int:
    """Calculate nth Fibonacci number."""
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)

act = dspy.CodeAct(
    "n -> result",
    tools=[factorial, fibonacci]
)

result = act(n=5)  # Uses factorial(5) -> 120
print(result.result)
```

### Custom Interpreter

```python
from dspy import PythonInterpreter

# Create interpreter with custom settings
interpreter = PythonInterpreter(
    enable_read_paths=["/safe/data/path"],
    enable_write_paths=["/safe/output/path"],
    enable_env_vars=["ALLOWED_VAR"],
    sync_files=True
)

code_act = dspy.CodeAct(
    "problem -> solution",
    tools=[],
    interpreter=interpreter
)
```

---

## dspy.ProgramOfThought

Generates Python code for computational tasks.

### Constructor

```python
dspy.ProgramOfThought(
    signature: str | type[Signature],
    max_iters: int = 3,
    interpreter: PythonInterpreter | None = None
)
```

Uses three internal ChainOfThought predictors:
- `code_generate`: Initial code generation
- `code_regenerate`: Fix errors if execution fails
- `generate_output`: Extract final answer from execution

**Requires:** Deno installation

### Basic Example

```python
pot = dspy.ProgramOfThought("question -> answer")
result = pot(question="What is the sum of squares of numbers 1 to 10?")
print(result.answer)
```

### Complex Computation

```python
class ComputationSignature(dspy.Signature):
    """Solve computational problems with code."""

    problem: str = dspy.InputField()
    constraints: str = dspy.InputField(default="")
    code: str = dspy.OutputField(desc="Python code solution")
    answer: str = dspy.OutputField(desc="Final computed answer")

pot = dspy.ProgramOfThought(ComputationSignature)
result = pot(
    problem="Find all prime numbers between 100 and 200",
    constraints="Use efficient algorithm"
)
```

---

## dspy.RLM

Recursive Language Model for exploring large contexts.

### Constructor

```python
dspy.RLM(
    signature: str | type[Signature],
    max_iterations: int = 20,
    max_llm_calls: int = 50,
    max_output_chars: int = 100000,
    verbose: bool = False,
    tools: list | None = None,
    sub_lm: dspy.LM | None = None,
    interpreter: CodeInterpreter | None = None
)
```

### Built-in REPL Tools

- `llm_query(prompt)`: Query the LM
- `llm_query_batched(prompts)`: Batch LM queries
- `SUBMIT(...)`: Submit final answer
- Standard modules: `re`, `json`, `collections`, `math`

**Note:** Experimental API. Not thread-safe with custom interpreters.

### Basic Example

```python
rlm = dspy.RLM("context, query -> answer")
result = rlm(
    context="...large document with 50,000 words...",
    query="What are the main conclusions?"
)
print(result.answer)
```

### With Custom Tools

```python
def search_section(document: str, section: str) -> str:
    """Search for a specific section in the document."""
    # Implementation
    return f"Content of {section}"

rlm = dspy.RLM(
    "document, question -> answer",
    tools=[search_section],
    max_iterations=30
)
```

---

## dspy.BestOfN

Executes module N times and returns best result.

### Constructor

```python
dspy.BestOfN(
    module: Module,
    N: int,
    reward_fn: Callable,  # (args_dict, Prediction) -> float
    threshold: float,
    fail_count: int | None = None  # defaults to N
)
```

Executes module N times with different rollout IDs at temperature=1.0, returns first exceeding threshold or best.

### Basic Example

```python
qa = dspy.ChainOfThought("question -> answer")

def one_word_answer(args, pred):
    """Reward function: 1.0 if answer is one word, else 0.0."""
    return 1.0 if len(pred.answer.split()) == 1 else 0.0

best_of_3 = dspy.BestOfN(
    module=qa,
    N=3,
    reward_fn=one_word_answer,
    threshold=1.0
)

result = best_of_3(question="What is the capital of Belgium?")
print(result.answer)  # "Brussels"
```

### Complex Reward Function

```python
def quality_score(args, pred):
    """Multi-criteria quality scoring."""
    score = 0.0

    # Length check
    if 50 <= len(pred.answer) <= 200:
        score += 0.3

    # Has reasoning
    if hasattr(pred, 'reasoning') and len(pred.reasoning) > 0:
        score += 0.4

    # Confidence check
    if hasattr(pred, 'confidence') and pred.confidence > 0.8:
        score += 0.3

    return score

best_of_5 = dspy.BestOfN(
    module=dspy.ChainOfThought("question -> answer, confidence: float"),
    N=5,
    reward_fn=quality_score,
    threshold=0.8,
    fail_count=3
)
```

---

## dspy.Refine

Same interface as BestOfN but automatically generates feedback to improve future predictions.

### Constructor

```python
dspy.Refine(
    module: Module,
    N: int,
    reward_fn: Callable,
    threshold: float,
    fail_count: int | None = None
)
```

### Basic Example

```python
qa = dspy.ChainOfThought("question -> answer")

def concise_answer(args, pred):
    """Reward concise answers (under 10 words)."""
    word_count = len(pred.answer.split())
    return 1.0 if word_count <= 10 else 0.5 - (word_count - 10) * 0.05

refine = dspy.Refine(
    module=qa,
    N=3,
    reward_fn=concise_answer,
    threshold=1.0,
    fail_count=1
)

result = refine(question="What is the capital of Belgium?")
```

### Difference from BestOfN

- **BestOfN:** Simply tries N times and picks best
- **Refine:** Generates feedback after failures to guide next attempt

---

## dspy.Parallel

Execute module on multiple examples in parallel.

### Constructor

```python
dspy.Parallel(
    num_threads: int | None = None,
    max_errors: int | None = None,
    access_examples: bool = True,
    return_failed_examples: bool = False,
    provide_traceback: bool | None = None,
    disable_progress_bar: bool = False,
    timeout: int = 120,
    straggler_limit: int = 3
)
```

### Basic Example

```python
parallel = dspy.Parallel(num_threads=4)
predict = dspy.Predict("question -> answer")

examples = [
    dspy.Example(question="1+1").with_inputs("question"),
    dspy.Example(question="2+2").with_inputs("question"),
    dspy.Example(question="3+3").with_inputs("question"),
    dspy.Example(question="4+4").with_inputs("question")
]

results = parallel([(predict, ex) for ex in examples])

for result in results:
    print(result.answer)
```

### Batch Processing

```python
# Use module.batch() for simpler syntax
predict = dspy.Predict("text -> sentiment: bool")

texts = [
    "I love this!",
    "This is terrible.",
    "It's okay.",
    "Amazing experience!"
]

examples = [dspy.Example(text=t).with_inputs("text") for t in texts]
results = predict.batch(examples, num_threads=4)

for text, result in zip(texts, results):
    print(f"{text}: {result.sentiment}")
```

---

## dspy.MultiChainComparison

Compares M reasoning attempts and produces unified corrected reasoning.

### Constructor

```python
dspy.MultiChainComparison(
    signature,
    M=3,
    temperature=0.7,
    **config
)
```

### Basic Example

```python
mcc = dspy.MultiChainComparison(
    "question -> answer",
    M=3,
    temperature=0.9
)

result = mcc(question="Explain quantum entanglement")
print(result.answer)  # Unified best answer from 3 attempts
```

---

## Custom Modules

### Pattern 1: Simple Wrapper

```python
class DataAnalyzer(dspy.Module):
    """Analyze data and provide insights."""

    def __init__(self):
        super().__init__()
        self.analyze = dspy.ChainOfThought(
            "dataset -> insights, recommendations"
        )

    def forward(self, dataset):
        return self.analyze(dataset=dataset)

# Usage
analyzer = DataAnalyzer()
result = analyzer(dataset="sales_data.csv contents...")
```

### Pattern 2: Multi-Step Pipeline

```python
class ResearchPipeline(dspy.Module):
    """Multi-step research workflow."""

    def __init__(self):
        super().__init__()
        self.search = dspy.Predict("topic -> search_queries: list[str]")
        self.analyze = dspy.ChainOfThought("papers -> synthesis, conclusion")

    def forward(self, topic):
        # Step 1: Generate search queries
        queries = self.search(topic=topic)

        # Step 2: Synthesize results
        result = self.analyze(papers=str(queries.search_queries))

        return dspy.Prediction(
            search_queries=queries.search_queries,
            synthesis=result.synthesis,
            conclusion=result.conclusion
        )

# Usage
pipeline = ResearchPipeline()
result = pipeline(topic="Machine Learning in Healthcare")
```

### Pattern 3: Conditional Routing

```python
class SmartRouter(dspy.Module):
    """Route queries to specialized handlers."""

    def __init__(self):
        super().__init__()
        self.classifier = dspy.Predict(
            "input -> category: Literal['technical', 'creative', 'analytical']"
        )
        self.technical = dspy.ChainOfThought("input -> technical_answer")
        self.creative = dspy.Predict("input -> creative_answer")
        self.analytical = dspy.ChainOfThought("input -> analytical_answer")

    def forward(self, input_text):
        # Classify first
        classification = self.classifier(input=input_text)

        # Route based on classification
        if classification.category == 'technical':
            return self.technical(input=input_text)
        elif classification.category == 'creative':
            return self.creative(input=input_text)
        else:
            return self.analytical(input=input_text)

# Usage
router = SmartRouter()
result = router(input_text="Explain how neural networks work")
```

### Pattern 4: Expert System with Tools

```python
class DataExpert(dspy.Module):
    """Expert for data analysis tasks."""

    def __init__(self, tools):
        super().__init__()
        self.signature = dspy.Signature(
            "task -> analysis, recommendations, confidence: float"
        )
        self.agent = dspy.ReAct(
            self.signature,
            tools=tools,
            max_iters=10
        )

    def forward(self, task):
        return self.agent(task=task)

# Define tools
def analyze_hdf5(file_path: str) -> str:
    """Analyze HDF5 file structure."""
    return f"Analysis of {file_path}"

def optimize_compression(file_path: str, level: int = 5) -> str:
    """Optimize compression settings."""
    return f"Optimized {file_path} at level {level}"

# Usage
expert = DataExpert(tools=[analyze_hdf5, optimize_compression])
result = expert(task="Optimize compression for data.h5")
```

---

## Module Methods

### Saving and Loading

```python
# Save module state
module.save("path/to/module.json")

# Save full program (including code)
module.save("path/to/module.pkl", save_program=True)

# Load module state
loaded_module = MyModule()
loaded_module.load("path/to/module.json")

# Load with pickle
loaded_module.load("path/to/module.pkl", allow_pickle=True)
```

### State Management

```python
# Dump state to dict
state = module.dump_state(json_mode=True)

# Load state from dict
module.load_state(state)

# Deep copy
module_copy = module.deepcopy()

# Reset copy (clears demos, traces)
fresh_copy = module.reset_copy()
```

### Batch Processing

```python
module = dspy.ChainOfThought("question -> answer")

examples = [
    dspy.Example(question="What is 1+1?").with_inputs("question"),
    dspy.Example(question="What is 2+2?").with_inputs("question"),
]

results = module.batch(
    examples,
    num_threads=4,
    max_errors=2,
    return_failed_examples=True
)
```

### Inspect History

```python
# Show last N LM calls
module.inspect_history(n=3)

# Get full history
history = module.history
```

### Named Predictors

```python
# Get all predictors with their names
for name, predictor in module.named_predictors():
    print(f"{name}: {predictor}")

# Apply function to each predictor
module.map_named_predictors(lambda pred: pred.reset())
```

---

## CLIO Agent Usage

### Expert Module Pattern

```python
class ExpertAgent(dspy.Module):
    """Base class for expert agents in CLIO."""

    def __init__(self, signature, tools, max_iters=10):
        super().__init__()
        self.agent = dspy.ReAct(
            signature=signature,
            tools=tools,
            max_iters=max_iters
        )

    def forward(self, **kwargs):
        result = self.agent(**kwargs)

        # Store in ARC memory (simplified)
        # arc.store_invocation(...)

        return result

class DataExpert(ExpertAgent):
    """Expert for scientific data analysis."""

    def __init__(self):
        signature = dspy.Signature(
            "task, context -> analysis, recommendations: list[str]"
        )
        tools = [analyze_hdf5, convert_format, optimize_compression]
        super().__init__(signature, tools)
```

### Orchestrator Pattern

```python
class Orchestrator(dspy.Module):
    """Main orchestrator for CLIO Agent."""

    def __init__(self, registry):
        super().__init__()
        self.registry = registry
        self.router = dspy.ChainOfThought(
            "query, available_experts -> selected_expert, reasoning"
        )

    def forward(self, query):
        # Get available experts
        experts = self.registry.get_all_experts()

        # Route to expert
        routing = self.router(
            query=query,
            available_experts=list(experts.keys())
        )

        # Execute with selected expert
        expert = experts[routing.selected_expert]
        result = expert(task=query)

        return dspy.Prediction(
            answer=result.analysis,
            expert_used=routing.selected_expert,
            reasoning=routing.reasoning,
            recommendations=result.recommendations
        )
```

### Memory-Augmented Module

```python
class MemoryAugmentedExpert(dspy.Module):
    """Expert with ARC memory integration."""

    def __init__(self, arc_memory):
        super().__init__()
        self.arc = arc_memory
        self.expert = dspy.ChainOfThought(
            "query, memory_context -> answer, confidence: float"
        )

    def forward(self, query):
        # Retrieve from ARC
        memory_context = self.arc.retrieve(query, top_k=5)

        # Generate answer with context
        result = self.expert(
            query=query,
            memory_context=str(memory_context)
        )

        # Store result in ARC
        self.arc.store(query, result.answer, result.confidence)

        return result
```

---

## Summary

DSPy modules provide composable building blocks for LM programs:

1. **dspy.Predict** - Basic input/output transformation
2. **dspy.ChainOfThought** - Step-by-step reasoning
3. **dspy.ReAct** - Reasoning with tool usage
4. **dspy.CodeAct** - Code generation and execution
5. **dspy.ProgramOfThought** - Computational problem solving
6. **dspy.RLM** - Large context exploration
7. **dspy.BestOfN** - Multiple attempts with selection
8. **dspy.Refine** - Iterative improvement with feedback
9. **dspy.Parallel** - Concurrent execution
10. **dspy.MultiChainComparison** - Ensemble reasoning

Custom modules combine these primitives into domain-specific agents and pipelines, enabling CLIO Agent's 3-tier orchestration architecture.
