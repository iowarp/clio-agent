# DSPy Fundamentals: Comprehensive Documentation Report

**Report Generated:** 2025-10-17
**Total Pages Explored:** 30+
**Documentation Source:** https://dspy.ai/

---

## Executive Summary

### What is DSPy?

DSPy is a **declarative framework for building modular AI software** developed by Stanford NLP. The framework's name stands for "Declarative Self-improving Python." Rather than relying on manual prompt engineering, DSPy enables developers to build AI systems through structured Python code that describes AI behavior declaratively.

### Core Philosophy: Programming Over Prompting

DSPy fundamentally rejects the prompt-engineering paradigm. Instead of constantly tweaking text strings, developers write structured Python code. As the documentation states, think of DSPy as **"a higher-level language for AI programming,"** comparable to the evolution from assembly language to C or from pointer arithmetic to SQL.

The framework's tagline: **"Programming—rather than prompting—language models"**

### Key Innovation

DSPy decouples system design from specific language model choices or prompting strategies, making systems more maintainable and portable. As the FAQ states: "The same program expressed in 10 or 20 lines of DSPy can easily be compiled into multi-stage instructions for GPT-4, detailed prompts for Llama2-13b, or finetunes for T5-base."

---

## Installation

### Basic Installation
```bash
pip install dspy
```

### Development Version
```bash
pip install git+https://github.com/stanfordnlp/dspy.git
```

### With MCP Support
```bash
pip install -U "dspy[mcp]"
```

---

## Core Architecture: The Three Pillars

### 1. Modules as Code, Not Strings

DSPy provides structured modules that separate interface from implementation:

- **Signatures** define input/output behavior (e.g., `question -> answer: float`)
- **Modules** assign strategies for invoking language models
- **Adapters** map signatures to prompts before optimization

### 2. Optimizers Tune Prompts and Weights

DSPy provides multiple optimization strategies:

- **BootstrapRS**: Synthesizes few-shot examples for modules
- **GEPA & MIPROv2**: Propose and explore better natural-language instructions
- **BootstrapFinetune**: Builds datasets for finetuning model weights

### 3. Declarative Signatures

Rather than detailed prompts, developers specify *what* tasks need accomplishing through semantic field names and types.

---

## The Three-Stage Development Workflow

DSPy emphasizes a systematic progression:

### Stage 1: DSPy Programming
"Defining your task, its constraints, exploring a few examples, and using that to inform your initial pipeline design"

### Stage 2: DSPy Evaluation
Establishing metrics and development sets for systematic iteration

### Stage 3: DSPy Optimization
Tuning prompts and weights using DSPy optimizers

**Critical Note:** The documentation emphasizes: "it's unproductive to launch optimization runs using a poorly designed program or a bad metric," suggesting users follow the three-stage progression sequentially.

---

## Part 1: Language Models

### Initial Setup

```python
import dspy

# Configure a default language model
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)
```

### Supported Providers

DSPy supports numerous LM providers through LiteLLM integration:

- **OpenAI**: Set `OPENAI_API_KEY` environment variable
- **Google Gemini**: Use `GEMINI_API_KEY`
- **Anthropic Claude**: Configure `ANTHROPIC_API_KEY`
- **Databricks**: Automatic authentication on platform
- **Local models**: SGLang or Ollama servers
- **Other providers**: Azure, AWS SageMaker, Together AI, Anyscale, and more

### Provider Examples

**OpenAI:**
```python
lm = dspy.LM('openai/gpt-4o-mini', api_key='YOUR_OPENAI_API_KEY')
dspy.configure(lm=lm)
```

**Anthropic:**
```python
lm = dspy.LM('anthropic/claude-3-opus-20240229', api_key='YOUR_ANTHROPIC_API_KEY')
```

**Local Models (Ollama):**
```python
lm = dspy.LM('ollama_chat/llama3.2', api_base='http://localhost:11434', api_key='')
```

### Direct LM Calls

You can invoke the LM directly with a unified API:

```python
lm("Say this is a test!", temperature=0.7)
lm(messages=[{"role": "user", "content": "Say this is a test!"}])
```

### Generation Configuration

Customize LM behavior at initialization or per-call:

```python
lm = dspy.LM('openai/gpt-4o-mini',
             temperature=0.9,
             max_tokens=3000,
             cache=False)
```

**Key parameters:**
- `temperature`: Controls output randomness
- `max_tokens`: Limits response length
- `cache`: Enables/disables automatic caching (default: enabled)
- `stop`: Specifies stopping sequences

### Multiple LMs

Switch between models globally or within code blocks:

```python
# Global configuration
dspy.configure(lm=dspy.LM('openai/gpt-4o-mini'))

# Context-specific configuration (thread-safe)
with dspy.context(lm=dspy.LM('openai/gpt-3.5-turbo')):
    response = qa(question="Your question?")
```

### Caching & Rollout Control

DSPy caches LM calls by default. To force new requests while maintaining cache functionality, use `rollout_id` with non-zero temperature:

```python
lm("Say this is a test!", rollout_id=1, temperature=1.0)
```

This approach "hashes both the inputs and the `rollout_id` when looking up a cache entry," enabling diverse outputs while tracking experiments.

---

## Part 2: Signatures

### What Are DSPy Signatures?

A DSPy Signature is a **declarative specification defining input/output behavior** for language model modules. Rather than crafting detailed prompts, developers specify *what* tasks need accomplishing through semantic field names and types.

### Why Use Signatures?

As the documentation notes: **"Writing signatures is far more modular, adaptive, and reproducible than hacking at prompts or finetunes."**

Signatures enable:
- Modular, maintainable code
- Optimization by DSPy compilers into high-quality prompts
- Automatic fine-tune generation
- Easy modification without prompt re-engineering

### Inline Signature Syntax

Simple string-based definitions work for straightforward tasks:

```python
# Question Answering
"question -> answer"

# Classification
"sentence -> sentiment: bool"

# Multi-field tasks
"context: list[str], question: str -> answer: str"

# Multiple outputs
"question, choices: list[str] -> reasoning: str, selection: int"
```

**Notes:**
- Default type is `str` when unspecified
- Field names should be semantically meaningful
- Use `->` to separate inputs from outputs
- Use `:` to specify types

### Class-Based Signatures

For complex tasks requiring clarification, use class definitions:

```python
class Emotion(dspy.Signature):
    """Classify emotion."""
    sentence: str = dspy.InputField()
    sentiment: Literal['sadness', 'joy', 'love', 'anger', 'fear', 'surprise'] = dspy.OutputField()
```

**With Descriptions:**
```python
class CheckCitationFaithfulness(dspy.Signature):
    """Verify text is based on provided context."""

    context: str = dspy.InputField(desc="facts assumed true")
    text: str = dspy.InputField()
    faithfulness: bool = dspy.OutputField()
    evidence: dict[str, list[str]] = dspy.OutputField(desc="Supporting evidence")
```

**With Instructions (Runtime):**
```python
toxicity = dspy.Predict(
    dspy.Signature(
        "comment -> toxic: bool",
        instructions="Mark as 'toxic' if comment includes insults or harassment."
    )
)
```

### Type Support

Signatures support extensive type annotations:

- **Basic types**: `str`, `int`, `bool`, `float`
- **Collections**: `list[str]`, `dict[str, int]`
- **Typing module**: `Literal`, `Optional`, `Union`
- **Special types**: `dspy.Image`, `dspy.History`, `dspy.Tool`
- **Custom types**: Pydantic models with dot notation

### Example: Classification with Confidence

```python
class Classify(dspy.Signature):
    """Classify sentiment of a given sentence."""

    sentence: str = dspy.InputField()
    sentiment: Literal["positive", "negative", "neutral"] = dspy.OutputField()
    confidence: float = dspy.OutputField()
```

### Signature API Methods

**Modification Methods:**
- `with_updated_fields(name, type_=None, **kwargs)`: Updates field metadata
- `insert(index, name, field, type_=None)`: Inserts field at position
- `prepend(name, field, type_=None)`: Adds field at start
- `append(name, field, type_=None)`: Adds field at end
- `delete(name)`: Removes a field

**Configuration:**
- `with_instructions(instructions)`: Sets task instructions

**State Management:**
- `dump_state()` / `load_state(state)`: Serialize/deserialize

---

## Part 3: Modules

### What Are DSPy Modules?

According to the documentation: **"A DSPy module is a building block for programs that use LMs."**

Each module:
- Abstracts a specific prompting technique
- Maintains generalizability across any signature
- Contains learnable parameters (prompts and LM weights)
- Can be composed into larger programs

### Built-In Modules

#### 1. dspy.Predict

The foundational module handling basic prediction.

**Usage:**
```python
# 1) Declare with a signature
classify = dspy.Predict('sentence -> sentiment: bool')

# 2) Call with input argument(s)
response = classify(sentence="it's charming and affecting")

# 3) Access the output
print(response.sentiment)
```

**Key Methods:**
- `__call__(**kwargs)`: Invokes the predictor
- `forward(**kwargs)`: Processes inputs through LM adapter
- `aforward(**kwargs)`: Async version
- `batch(examples, num_threads=None)`: Parallel processing
- `save(path)` / `load(path)`: Persistence
- `dump_state()` / `load_state()`: State management
- `reset()`: Clears LM, traces, and demos

#### 2. dspy.ChainOfThought

Encourages step-by-step reasoning before generating responses.

**Constructor:**
```python
dspy.ChainOfThought(
    signature: str | type[Signature],
    rationale_field: FieldInfo | None = None,
    rationale_field_type: type = str,
    **config
)
```

**How it Works:**
The module prepends a reasoning field with the prompt: "Reasoning: Let's think step by step in order to" followed by the output.

**Example:**
```python
generate_answer = dspy.ChainOfThought('question -> answer')
pred = generate_answer(question='What is the color of the sky?')
print(pred.rationale)  # Step-by-step reasoning
print(pred.answer)     # Final answer
```

#### 3. dspy.ProgramOfThought

Directs the LM to output executable code whose results inform the final answer.

**Constructor:**
```python
dspy.ProgramOfThought(
    signature: str | type[Signature],
    max_iters: int = 3,
    interpreter: PythonInterpreter | None = None
)
```

**Requirements:** Deno must be installed (https://docs.deno.com/runtime/getting_started/installation/)

**Example:**
```python
import dspy

lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

pot = dspy.ProgramOfThought("question -> answer")
result = pot(question="Sarah has 5 apples. She gives 2 to Tom. How many does she have?")
```

#### 4. dspy.ReAct

An agent framework enabling tool integration for signature implementation.

**Constructor:**
```python
dspy.ReAct(
    signature: str | type[Signature],
    tools: list[Callable | Tool],
    max_iters: int = 10
)
```

**How it Works:**
The module operates through an iterative loop where the model:
1. Reasons about the current state
2. Selects an appropriate tool from available options
3. Executes the tool with specified arguments
4. Processes observations and continues or terminates

**Example:**
```python
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"The weather in {city} is sunny."

react = dspy.ReAct(
    signature="question -> answer",
    tools=[get_weather],
    max_iters=5
)

pred = react(question="What is the weather in Tokyo?")
print(pred.answer)
```

#### 5. dspy.MultiChainComparison

Compares multiple ChainOfThought outputs to produce refined predictions.

**Constructor:**
```python
dspy.MultiChainComparison(
    signature,
    M=3,           # Number of reasoning attempts
    temperature=0.7,
    **config
)
```

**How it Works:**
1. Generates M different reasoning attempts
2. Formats attempts as structured student responses
3. Compares and synthesizes a refined answer

#### 6. dspy.Refine

Runs a module multiple times and selects the best output based on a reward function.

**Constructor:**
```python
dspy.Refine(
    module: Module,
    N: int,
    reward_fn: Callable,
    threshold: float,
    fail_count: int | None = None
)
```

**Example:**
```python
qa = dspy.ChainOfThought("question -> answer")

def one_word_answer(args, pred):
    return 1.0 if len(pred.answer.split()) == 1 else 0.0

best_of_3 = dspy.Refine(
    module=qa,
    N=3,
    reward_fn=one_word_answer,
    threshold=1.0
)

result = best_of_3(question="What is the capital of Belgium?")
```

#### 7. dspy.BestOfN

Similar to Refine but simpler - runs module N times and picks best result.

```python
dspy.BestOfN(
    module: Module,
    N: int,
    reward_fn: Callable,
    threshold: float,
    fail_count: int | None = None
)
```

**Usage identical to Refine example above.**

#### 8. dspy.Parallel

Enables concurrent execution of DSPy modules across multiple threads.

**Constructor:**
```python
dspy.Parallel(
    num_threads: int | None = None,
    max_errors: int | None = None,
    access_examples: bool = True,
    return_failed_examples: bool = False
)
```

**Example:**
```python
parallel = dspy.Parallel(num_threads=4)
results = parallel([(predict, example1), (predict, example2), (predict, example3)])
```

### Module Composition

Modules combine into larger programs through standard Python control flow:

```python
class Hop(dspy.Module):
    def __init__(self, num_docs=10, num_hops=4):
        self.generate_query = dspy.ChainOfThought('claim, notes -> query')
        self.append_notes = dspy.ChainOfThought('claim, notes, context -> new_notes')

    def forward(self, claim: str, notes: str = ""):
        # Compose multiple module calls
        for hop in range(self.num_hops):
            query = self.generate_query(claim=claim, notes=notes).query
            context = search(query, k=self.num_docs)
            notes = self.append_notes(claim=claim, notes=notes, context=context).new_notes

        return dspy.Prediction(notes=notes)
```

**Key Pattern:**
1. Inherit from `dspy.Module`
2. Initialize sub-modules in `__init__`
3. Implement `forward()` method with composition logic
4. Return `dspy.Prediction` objects

---

## Part 4: Adapters

### What Are Adapters?

Adapters function as **intermediaries between `dspy.Predict` and language models**. They handle the conversion of DSPy signatures, user inputs, and demonstrations into multi-turn messages for LM processing.

### Core Responsibilities

- Converting signatures into system messages defining task structure
- Formatting input data according to signature specifications
- Parsing LM responses into structured `dspy.Prediction` outputs
- Managing conversation history and function calls
- Transforming DSPy types (Tools, Images, etc.) into prompt messages

### System Flow

1. User invokes a DSPy module with inputs
2. `dspy.Predict` calls `Adapter.format()` to convert signature, inputs, and demos into messages
3. Messages are sent to the LM via `dspy.LM`
4. LM generates a response
5. `Adapter.parse()` converts the response into structured outputs
6. Caller receives parsed results

### Adapter Configuration

Set adapters globally or contextually:

```python
# Global configuration
dspy.configure(adapter=dspy.ChatAdapter())

# Context-specific configuration
with dspy.context(adapter=dspy.JSONAdapter()):
    predict(question="What is 2+2?")
```

By default, `dspy.Predict` uses `ChatAdapter` if no adapter is specified.

### Built-In Adapters

#### 1. ChatAdapter (Default)

**Structure:** Uses `[[ ## field_name ## ]]` markers to delineate fields.

**Constructor:**
```python
dspy.ChatAdapter(
    callbacks: list[BaseCallback] | None = None,
    use_native_function_calling: bool = False,
    native_response_types: list[type] | None = [Citations]
)
```

**Advantages:**
- "Works with all language models"
- Includes fallback protection—automatically retries with JSONAdapter on failure
- Universal compatibility across model types

**Disadvantages:**
- Higher latency due to increased boilerplate output tokens
- Less efficient for latency-sensitive applications

**Example Output:**
```
[[ ## question ## ]]
What is 2+2?

[[ ## answer ## ]]
4

[[ ## completed ## ]]
```

#### 2. JSONAdapter

**Structure:** Prompts LMs to return JSON containing all output fields.

**Constructor:**
```python
dspy.JSONAdapter(
    callbacks: list[BaseCallback] | None = None,
    use_native_function_calling: bool = True
)
```

**Key Feature:** "JSONAdapter uses native function calling by default."

**Advantages:**
- Lower latency with minimal boilerplate
- Structured output validation
- Native function calling support

**Disadvantages:**
- Requires model support for JSON mode or function calling
- May fail with older/smaller models

**Example Output:**
```json
{
  "answer": "4",
  "confidence": 0.95
}
```

### Key Methods

**`format()`** - Converts DSPy inputs into multi-turn messages.

**`parse()`** - Extracts output fields from LM responses.

**`format_field_structure()`** - Generates instructions for field formatting.

**`format_finetune_data()`** - Converts call data into OpenAI-compatible fine-tuning format.

---

## Part 5: Tools

### Overview

DSPy provides robust support for tool-using agents that enable language models to interact with external functions, APIs, and services.

### Two Main Approaches

#### 1. dspy.ReAct (Fully Managed)

The ReAct module implements the Reasoning and Acting pattern where models iteratively reason and decide which tools to call.

**Key Features:**
- Automatic reasoning step-by-step
- Automatic tool selection based on context
- Iterative execution allowing multiple tool calls
- Built-in error recovery for failed calls
- Complete trajectory tracking

**Basic Implementation:**
```python
def get_weather(city: str) -> str:
    """Get weather for a city."""
    return f"Weather in {city}: sunny, 72°F"

def search_web(query: str) -> str:
    """Search the web."""
    return f"Search results for: {query}"

react_agent = dspy.ReAct(
    signature="question -> answer",
    tools=[get_weather, search_web],
    max_iters=5
)

result = react_agent(question="What's the weather in Tokyo?")
print(result.answer)
```

#### 2. Manual Tool Handling

Provides direct control using `dspy.Tool`, `dspy.ToolCalls`, and custom signatures.

**Key Components:**

**dspy.Tool** - Wraps Python functions for DSPy compatibility:

```python
def foo(x: int, y: str = "hello") -> str:
    """Example function."""
    return str(x) + y

tool = dspy.Tool(foo)
# Automatically infers: {'x': {'type': 'integer'}, 'y': {'type': 'string', 'default': 'hello'}}
```

**Constructor:**
```python
dspy.Tool(
    func: Callable,
    name: Optional[str] = None,
    desc: Optional[str] = None,
    args: Optional[dict] = None,
    arg_types: Optional[dict] = None,
    arg_desc: Optional[dict] = None
)
```

**Key Methods:**
- `__call__(**kwargs)`: Synchronous execution
- `acall(**kwargs)`: Async execution
- `format_as_litellm_function_call()`: Converts to LiteLLM format
- `from_langchain(tool)`: Converts LangChain tools
- `from_mcp_tool(session, tool)`: Converts MCP tools

### Native Tool Calling

DSPy adapters support native function calling:

- **ChatAdapter**: Defaults to text-based parsing (`use_native_function_calling=False`)
- **JSONAdapter**: Defaults to native calling (`use_native_function_calling=True`)

Models without native support automatically fall back to text parsing.

### Best Practices

**Tool Design:**
- Include clear, descriptive docstrings
- Provide explicit type hints
- Use basic types or Pydantic models for parameters

**Approach Selection:**
- Use ReAct for autonomous agents needing complex reasoning
- Use manual handling for precise control over tool orchestration

---

## Part 6: MCP Integration

### What is MCP?

The Model Context Protocol (MCP) is **"an open protocol that standardizes how applications provide context to language models."** DSPy supports MCP, enabling integration with any MCP-compatible server.

### Key Capabilities

- Connect to standardized tools from any MCP-compatible server
- Reuse the same tools across different frameworks
- Streamline tool integration with minimal code

### Installation

```bash
pip install -U "dspy[mcp]"
```

### Implementation Approaches

**HTTP Server (Remote):** Use streamable HTTP transport to connect to remote MCP servers over the network.

**Stdio Server (Local):** Use a local server process communicating via standard input/output, configured with `StdioServerParameters`.

### Tool Conversion

DSPy automatically converts MCP tools to DSPy tools:

```python
dspy.Tool.from_mcp_tool(session, tool)
```

The conversion preserves:
- Tool names
- Descriptions
- Parameter schemas
- Async execution support

### Typical Workflow

1. Establish connection to MCP server (HTTP or stdio)
2. Initialize the client session
3. List available tools from the server
4. Convert MCP tools to DSPy tools
5. Use tools with DSPy agents like ReAct

---

## Part 7: Primitives

### dspy.Example

The fundamental data structure for representing datapoints.

**Creation:**
```python
qa_pair = dspy.Example(question="This is a question?", answer="This is an answer.")
print(qa_pair.question)  # Access via dot notation
```

**Constructor:**
```python
dspy.Example(base=None, **kwargs)
```

**Key Methods:**

**`with_inputs(*keys)`** - Designates which fields are inputs:
```python
article_summary = dspy.Example(
    article="This is an article.",
    summary="This is a summary."
).with_inputs("article")
```

**`inputs()`** - Returns only input fields:
```python
input_only = article_summary.inputs()
```

**`labels()`** - Returns non-input fields:
```python
labels_only = article_summary.labels()
```

**Other Methods:**
- `copy(**kwargs)`: Creates shallow copy
- `without(*keys)`: Excludes specified keys
- `get(key, default=None)`: Retrieves value with default
- `keys()` / `values()` / `items()`: Dictionary-like access
- `toDict()`: Converts to dictionary

### dspy.Prediction

A specialized container for module outputs that inherits from Example.

**Key Differences:**
- Removes `_demos` and `_input_keys` attributes
- Adds score-based comparison operators (<, >, <=, >=)
- Supports arithmetic operations on score values
- Includes LM usage tracking

**Methods:**
- `get_lm_usage()` / `set_lm_usage()`: Monitor API consumption
- `from_completions()`: Creates from completion data
- All Example methods inherited

### dspy.Image

Enables image handling in DSPy programs.

**Constructor:**
```python
dspy.Image(url: Any = None, *, download: bool = False, **data)
```

**Supported Input Formats:**
- String paths: HTTP(S)/GS URLs or local file paths
- Raw bytes: Direct image byte data
- PIL images: `PIL.Image.Image` instances
- Legacy dict format: `{"url": value}`
- Data URIs: Already-encoded image data

**Example:**
```python
# From URL
image = dspy.Image("https://example.com/image.jpg")

# From file
image = dspy.Image("/path/to/image.png")

# From PIL
from PIL import Image
pil_img = Image.open("photo.jpg")
image = dspy.Image(pil_img)
```

**Key Methods:**
- `format()`: Returns image formatted for model consumption
- `parse_lm_response()`: Parses LM responses into Image objects
- `is_streamable()`: Returns False

### dspy.History

Represents conversation history as a list of messages.

**Constructor:**
```python
dspy.History(messages: list[dict[str, Any]])
```

**Usage:**
```python
history = dspy.History(messages=[
    {"question": "What is 2+2?", "answer": "4"},
    {"question": "What is 3+3?", "answer": "6"}
])

predict = dspy.Predict("question, history -> answer")
result = predict(question="What is 4+4?", history=history)
```

**Key Attribute:**
- `messages`: List of message dictionaries matching signature fields

**Configuration:**
- `frozen=True`: Immutable after creation
- `extra='forbid'`: Only defined fields allowed

### dspy.Tool

Wraps functions to enable tool calling in language models.

*See Part 5: Tools for complete documentation.*

---

## Part 8: Data Handling

### Core Data Type: Example Objects

DSPy uses `Example` objects as its fundamental data structure for training and test sets.

### Creating Examples

```python
qa_pair = dspy.Example(question="This is a question?", answer="This is an answer.")
print(qa_pair.question)

# Training set
trainset = [
    dspy.Example(report="LONG REPORT 1", summary="short summary 1"),
    dspy.Example(report="LONG REPORT 2", summary="short summary 2"),
    # ...
]
```

### Distinguishing Inputs from Labels

DSPy differentiates between inputs and labels using `with_inputs()`:

```python
# Mark single input
qa_pair.with_inputs("question")

# Mark multiple inputs
qa_pair.with_inputs("question", "context")
```

### Filtering Example Fields

```python
article_summary = dspy.Example(
    article="This is an article.",
    summary="This is a summary."
).with_inputs("article")

input_only = article_summary.inputs()      # Returns only input fields
labels_only = article_summary.labels()     # Returns non-input fields
```

### Key Characteristics

- Examples can contain any number of fields with flexible value types
- DSPy modules return `Prediction` objects, which are specialized Example subclasses
- The framework supports training without intermediate or final labels
- Field access uses dot notation (e.g., `example.field_name`)

---

## Part 9: Evaluation

### Philosophy

DSPy's evaluation approach emphasizes systematic refinement through data and metrics. The documentation notes: **"it's hard to consistently improve what you aren't able to define."**

### Key Setup Steps

#### 1. Collect Development Data

Gather inputs and outputs (rarely intermediate labels). Sources include:
- HuggingFace datasets
- Naturally occurring data (StackExchange, etc.)
- Synthetic data generation

**Recommendation:** "20 input examples of your task can be useful, though 200 goes a long way."

#### 2. Define Your Metric

Create a scoring function evaluating system outputs.

### Metrics

#### Definition

A metric in DSPy is **"a function that will take examples from your data and the output of your system and return a score that quantifies how good the output is."**

#### Metric Structure

All DSPy metrics follow this signature:

```python
def metric_name(example, pred, trace=None):
    return score  # float, int, or bool
```

**Parameters:**
- `example`: The ground truth Example
- `pred`: The Prediction from your program
- `trace`: Optional access to intermediate program steps

#### Simple Metrics

Basic metrics compare expected versus actual outputs:

```python
def validate_answer(example, pred, trace=None):
    return example.answer.lower() == pred.answer.lower()
```

**Built-in utilities:**
- `dspy.evaluate.metrics.answer_exact_match`
- `dspy.evaluate.metrics.answer_passage_match`

#### Complex Metrics

For long-form outputs, evaluate multiple dimensions:

```python
def validate_context_and_answer(example, pred, trace=None):
    answer_match = example.answer.lower() == pred.answer.lower()
    context_match = any((pred.answer.lower() in c) for c in pred.context)

    if trace is None:  # evaluation/optimization
        return (answer_match + context_match) / 2.0
    else:  # bootstrapping
        return answer_match and context_match
```

**Key Pattern:**
- During evaluation/optimization: Return continuous score (0.0-1.0)
- During bootstrapping: Return boolean (pass/fail)

#### AI-Powered Metrics

Leverage language models within metrics for sophisticated evaluation:

```python
class AssessQuality(dspy.Signature):
    """Assess the quality of an answer."""

    question: str = dspy.InputField()
    answer: str = dspy.InputField()
    quality_score: float = dspy.OutputField(desc="Score from 0.0 to 1.0")

def ai_metric(example, pred, trace=None):
    assessor = dspy.ChainOfThought(AssessQuality)
    result = assessor(question=example.question, answer=pred.answer)
    return result.quality_score
```

### The Evaluate Class

Assesses DSPy program performance using evaluation dataset and metric function.

**Constructor:**
```python
dspy.Evaluate(
    devset: list[Example],
    metric: Callable = None,
    num_threads: int = None,
    display_progress: bool = True,
    display_table: bool = True,
    max_errors: int = None,
    failure_score: float = 0.0,
    save_as_csv: str = None,
    save_as_json: str = None
)
```

**Usage:**
```python
# Simple loop approach
scores = []
for x in devset:
    pred = program(**x.inputs())
    score = metric(x, pred)
    scores.append(score)

# Or use Evaluate utility
evaluator = dspy.Evaluate(devset=test_data, metric=my_metric, num_threads=4)
results = evaluator(program=my_program)
print(f"Score: {results.score}%")
```

**Return Value:**

`EvaluationResult` object containing:
- `score`: A float percentage score (e.g., 67.30)
- `results`: List of (example, prediction, score) tuples

### Advanced: Trace Access

During optimization, access intermediate steps via the `trace` parameter to validate intermediate predictor outputs.

---

## Part 10: Optimization

### When to Optimize

Optimization begins after establishing:
- **Training data**: Minimum 30 examples, ideally 300+
- **Validation data**: Held-out test set
- **Evaluation metric**: Clear measurement of success

### How Optimization Works

DSPy optimizers tune prompts or model weights in your program. The framework recommends an unconventional data split: **"20% for training, 80% for validation"** to prevent overfitting to small training sets.

(Exception: GEPA optimizer follows standard ML conventions with larger training sets.)

### The Optimization Process

The iterative cycle:

1. **Initial optimization run** using available optimizers
2. **Assessment** of results against your metric
3. **Refinement** by revisiting:
   - Task definition clarity
   - Data collection adequacy
   - Metric appropriateness
   - Optimizer sophistication
   - Program structure complexity

### Best Practices

- **"Iterative development is key"** to successful optimization
- Consider adding DSPy Assertions for advanced constraints
- Increase program complexity with additional steps if needed
- Sequence multiple optimizers for cumulative improvements
- Revisit program structure, not just parameters

### Available Optimizers

#### Automatic Few-Shot Learning

##### 1. BootstrapFewShot

Automatically generates demonstration examples by combining labeled training examples with bootstrapped demonstrations.

**Constructor:**
```python
dspy.BootstrapFewShot(
    metric: Callable = None,
    metric_threshold: float = None,
    teacher_settings: dict = None,
    max_bootstrapped_demos: int = 4,
    max_labeled_demos: int = 16,
    max_rounds: int = 1,
    max_errors: Optional[int] = None
)
```

**How it Works:**

1. **Bootstrap Process**: "Each bootstrap round copies the LM with a new `rollout_id` at `temperature=1.0` to bypass caches and gather diverse traces."
2. **Demo Composition**: Combines labeled examples with bootstrapped demonstrations
3. **Quality Filtering**: Uses metric function to validate examples

**Usage:**
```python
from dspy.teleprompt import BootstrapFewShot

optimizer = BootstrapFewShot(metric=my_metric)
optimized_program = optimizer.compile(
    student=my_program,
    trainset=train_data
)
```

##### 2. BootstrapFewShotWithRandomSearch

Applies BootstrapFewShot multiple times with random search.

**Use when:** You have 50+ examples and want more thorough optimization.

##### 3. KNNFewShot

Uses k-nearest neighbors to find relevant training examples for each query.

**Use when:** You have diverse examples and want context-aware demonstrations.

#### Automatic Instruction Optimization

##### 1. MIPROv2

**Multiprompt Instruction PRoposal Optimizer Version 2** - jointly optimizes both instructions and few-shot examples.

**Constructor:**
```python
dspy.MIPROv2(
    metric: Callable,
    prompt_model=None,
    task_model=None,
    max_bootstrapped_demos: int = 4,
    max_labeled_demos: int = 4,
    auto: str = None,  # "light", "medium", "heavy"
    seed: int = 9,
    init_temperature: float = 1.0,
    verbose: bool = False,
    track_stats: bool = False,
    metric_threshold: float = None
)
```

**Three-Phase Process:**

1. **Bootstrap Few-Shot Examples**: Samples training examples, runs them through your program, retains successful outputs
2. **Propose Instructions**: Generates instruction candidates using dataset properties, program code, bootstrapped examples
3. **Bayesian Optimization**: Evaluates prompt combinations using minibatches and periodic full validation

**Usage:**
```python
from dspy.teleprompt import MIPROv2

optimizer = MIPROv2(metric=my_metric, auto="medium")
optimized_program = optimizer.compile(
    student=my_program,
    trainset=train_data,
    valset=val_data,
    num_trials=100
)
```

**Compile Parameters:**
- `trainset`: Training data (required)
- `valset`: Validation data (optional)
- `num_trials`: Optimization iterations
- `minibatch`: Use minibatch evaluation (default: True)
- `minibatch_size`: Batch size (default: 35)

**Cost:** A typical run costs approximately $2 USD and takes around 20 minutes.

##### 2. COPRO

Iteratively refines prompts through breadth-first search.

**Constructor:**
```python
dspy.COPRO(
    prompt_model=None,
    metric=None,
    breadth=10,
    depth=3,
    init_temperature=1.4,
    track_stats=False
)
```

**Parameters:**
- `breadth`: Number of candidate prompts per iteration (must be > 1)
- `depth`: Number of optimization iterations
- `init_temperature`: Temperature for prompt generation (default: 1.4)

**How it Works:**

1. **Initialization**: Generates `breadth-1` candidate prompts plus original
2. **Iteration Loop**: Evaluates all candidates, selects top performers
3. **Refinement**: Uses successful prompts to generate next batch
4. **Selection**: Returns best-performing program

**Usage:**
```python
optimizer = dspy.COPRO(metric=my_metric, breadth=10, depth=3)
optimized = optimizer.compile(student=program, trainset=train_data)
```

##### 3. GEPA

Reflects on program trajectories to identify gaps and propose improvements.

**Use when:** You need sophisticated reasoning about program behavior and want detailed feedback-driven optimization.

##### 4. SIMBA

(Details not fully available in documentation)

#### Automatic Finetuning

##### BootstrapFinetune

Distills prompt-based programs into weight updates for finetuned models.

**Use when:** You want to deploy a smaller, faster model with learned behaviors from larger models.

#### Program Transformations

##### Ensemble

Combines multiple DSPy programs into a single unified program.

**Use when:** You want to aggregate predictions from different approaches for improved robustness.

### Selection Guidance

Choose based on your constraints:

- **~10 examples**: Start with BootstrapFewShot
- **50+ examples**: Try BootstrapFewShotWithRandomSearch
- **0-shot preference**: Use MIPROv2 configured for instruction-only optimization
- **Extensive optimization budget**: Combine multiple optimizers sequentially

### Cost Considerations

Reference experiment using BootstrapFewShotWithRandomSearch on GPT-3.5-turbo:
- Time: ~6 minutes
- API calls: 3,200
- Cost: ~$3 USD

---

## Part 11: Complete Code Examples

### Example 1: Basic Question Answering

```python
import dspy

# Configure language model
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

# Create a simple QA module
qa = dspy.ChainOfThought('question -> answer')

# Use it
response = qa(question="What is the capital of France?")
print(response.answer)
print(response.rationale)
```

### Example 2: Classification with Confidence

```python
import dspy
from typing import Literal

# Define signature
class Classify(dspy.Signature):
    """Classify sentiment of a given sentence."""

    sentence: str = dspy.InputField()
    sentiment: Literal["positive", "negative", "neutral"] = dspy.OutputField()
    confidence: float = dspy.OutputField()

# Create classifier
classifier = dspy.Predict(Classify)

# Use it
result = classifier(sentence="I love this product!")
print(f"Sentiment: {result.sentiment}")
print(f"Confidence: {result.confidence}")
```

### Example 3: Multi-Step RAG Pipeline

```python
import dspy

class RAG(dspy.Module):
    def __init__(self, num_docs=3):
        super().__init__()
        self.retrieve = dspy.Retrieve(k=num_docs)
        self.generate_answer = dspy.ChainOfThought('context, question -> answer')

    def forward(self, question):
        # Retrieve relevant passages
        passages = self.retrieve(question).passages

        # Generate answer from context
        prediction = self.generate_answer(
            context=passages,
            question=question
        )

        return dspy.Prediction(
            context=passages,
            answer=prediction.answer
        )

# Configure
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

# Use RAG
rag = RAG(num_docs=5)
result = rag(question="What is DSPy?")
print(result.answer)
print("Sources:", result.context)
```

### Example 4: Agent with Tools

```python
import dspy

# Define tools
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    # In reality, call a weather API
    return f"The weather in {city} is sunny, 72°F"

def search_web(query: str) -> str:
    """Search the web for information."""
    # In reality, call a search API
    return f"Search results for: {query}"

def calculate(expression: str) -> str:
    """Evaluate a mathematical expression."""
    try:
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"Error: {e}"

# Create agent
agent = dspy.ReAct(
    signature="question -> answer",
    tools=[get_weather, search_web, calculate],
    max_iters=5
)

# Configure
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

# Use agent
questions = [
    "What's the weather in Tokyo?",
    "What is 15 * 23 + 7?",
    "What is the population of Paris?"
]

for q in questions:
    result = agent(question=q)
    print(f"Q: {q}")
    print(f"A: {result.answer}\n")
```

### Example 5: Evaluation and Optimization

```python
import dspy
from dspy.teleprompt import BootstrapFewShot

# Configure
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

# Define program
class QA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate_answer = dspy.ChainOfThought('question -> answer')

    def forward(self, question):
        return self.generate_answer(question=question)

# Create training data
trainset = [
    dspy.Example(question="What is 2+2?", answer="4").with_inputs("question"),
    dspy.Example(question="What is the capital of France?", answer="Paris").with_inputs("question"),
    dspy.Example(question="Who wrote Hamlet?", answer="William Shakespeare").with_inputs("question"),
    # ... more examples
]

# Create test data
testset = [
    dspy.Example(question="What is 3+3?", answer="6").with_inputs("question"),
    dspy.Example(question="What is the capital of Spain?", answer="Madrid").with_inputs("question"),
    # ... more examples
]

# Define metric
def exact_match(example, pred, trace=None):
    return example.answer.lower().strip() == pred.answer.lower().strip()

# Evaluate baseline
program = QA()
evaluator = dspy.Evaluate(devset=testset, metric=exact_match, num_threads=4)
baseline_score = evaluator(program)
print(f"Baseline Score: {baseline_score.score}%")

# Optimize
optimizer = BootstrapFewShot(metric=exact_match, max_bootstrapped_demos=4)
optimized_program = optimizer.compile(student=program, trainset=trainset)

# Evaluate optimized
optimized_score = evaluator(optimized_program)
print(f"Optimized Score: {optimized_score.score}%")

# Save optimized program
optimized_program.save('qa_optimized.json')

# Load later
loaded_program = QA()
loaded_program.load('qa_optimized.json')
```

### Example 6: Custom Module with Complex Logic

```python
import dspy

class MultiHopQA(dspy.Module):
    def __init__(self, num_hops=3):
        super().__init__()
        self.num_hops = num_hops
        self.generate_query = dspy.ChainOfThought('question, context -> search_query')
        self.generate_answer = dspy.ChainOfThought('question, context -> answer')
        self.retrieve = dspy.Retrieve(k=3)

    def forward(self, question):
        context = []

        # Perform multiple retrieval hops
        for hop in range(self.num_hops):
            # Generate search query
            query_result = self.generate_query(
                question=question,
                context=context
            )

            # Retrieve passages
            passages = self.retrieve(query_result.search_query).passages
            context.extend(passages)

        # Generate final answer
        answer = self.generate_answer(
            question=question,
            context=context
        )

        return dspy.Prediction(
            answer=answer.answer,
            context=context
        )

# Use it
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

multi_hop = MultiHopQA(num_hops=3)
result = multi_hop(question="What university did the inventor of the transistor attend?")
print(result.answer)
```

### Example 7: Conversation with History

```python
import dspy

# Configure
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

# Create chatbot
chatbot = dspy.ChainOfThought('question, history -> answer')

# Initialize conversation
history = dspy.History(messages=[])

# Conversation loop
while True:
    user_input = input("You: ")
    if user_input.lower() in ['exit', 'quit']:
        break

    # Get response
    result = chatbot(question=user_input, history=history)
    print(f"Bot: {result.answer}")

    # Update history
    history = dspy.History(messages=history.messages + [
        {"question": user_input, "answer": result.answer}
    ])
```

### Example 8: Multi-Model Usage

```python
import dspy

# Configure multiple models
gpt4 = dspy.LM('openai/gpt-4o')
gpt3 = dspy.LM('openai/gpt-3.5-turbo')
claude = dspy.LM('anthropic/claude-3-opus-20240229')

# Set default
dspy.configure(lm=gpt3)

# Use different models for different tasks
class SmartQA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.quick_classify = dspy.Predict('question -> complexity: Literal["simple", "complex"]')
        self.simple_qa = dspy.ChainOfThought('question -> answer')
        self.complex_qa = dspy.ChainOfThought('question -> answer')

    def forward(self, question):
        # Classify with fast model (default gpt3)
        complexity = self.quick_classify(question=question).complexity

        if complexity == "simple":
            # Use fast model for simple questions
            with dspy.context(lm=gpt3):
                return self.simple_qa(question=question)
        else:
            # Use powerful model for complex questions
            with dspy.context(lm=gpt4):
                return self.complex_qa(question=question)

# Use it
smart_qa = SmartQA()
result = smart_qa(question="Explain quantum entanglement")
print(result.answer)
```

---

## Part 12: Best Practices

### Programming Best Practices

1. **Start Simple**: Begin with single modules like `dspy.Predict` or `dspy.ChainOfThought`, then add complexity.

2. **Use Semantic Field Names**: Field names should clearly describe what they contain (e.g., `question`, `answer`, not `input`, `output`).

3. **Add Descriptions**: Use `desc` parameter in InputField/OutputField for clarity:
   ```python
   answer: str = dspy.OutputField(desc="often between 1 and 5 words")
   ```

4. **Compose Modules**: Build complex programs by composing simple modules:
   ```python
   class ComplexProgram(dspy.Module):
       def __init__(self):
           self.step1 = dspy.ChainOfThought(...)
           self.step2 = dspy.Predict(...)

       def forward(self, ...):
           result1 = self.step1(...)
           result2 = self.step2(result=result1, ...)
           return result2
   ```

5. **Use Type Annotations**: Leverage Python's type system for validation:
   ```python
   sentiment: Literal["positive", "negative", "neutral"] = dspy.OutputField()
   ```

### Evaluation Best Practices

1. **Start with Simple Metrics**: Begin with exact match or accuracy, then sophisticate.

2. **Use Continuous Scores**: During evaluation, return 0.0-1.0 scores for nuanced assessment.

3. **Collect Diverse Examples**: 20 examples minimum, 200+ ideal, covering edge cases.

4. **Hold Out Test Data**: Use separate validation set to avoid overfitting.

5. **Iterate on Metrics**: Treat your metric as a program itself—refine and optimize it.

### Optimization Best Practices

1. **Optimize After Evaluation**: Don't optimize prematurely—establish baseline first.

2. **Data Split**: Use 20% training / 80% validation for small datasets to prevent overfitting.

3. **Start Small**: Begin with BootstrapFewShot before trying more sophisticated optimizers.

4. **Sequence Optimizers**: Chain multiple optimizers for cumulative improvements:
   ```python
   # First optimize few-shot examples
   program = BootstrapFewShot(...).compile(program, trainset)

   # Then optimize instructions
   program = MIPROv2(...).compile(program, trainset, valset)
   ```

5. **Track Costs**: Monitor API usage during optimization runs.

### Production Best Practices

1. **Enable Caching**: Keep default caching enabled for cost efficiency.

2. **Use Async Methods**: Leverage `aforward()` and `acall()` for concurrent operations.

3. **Implement Error Handling**: Wrap calls in try-except blocks.

4. **Monitor with Callbacks**: Use adapter callbacks for logging and observability.

5. **Version Your Programs**: Save compiled programs with versioned names:
   ```python
   program.save('model_v1.2_2024-01-15.json')
   ```

6. **Thread Safety**: DSPy's context managers are thread-safe—use them for concurrent requests.

### Tool Design Best Practices

1. **Clear Docstrings**: Tools need descriptive docstrings for LM understanding:
   ```python
   def search_web(query: str) -> str:
       """Search the web for information about the query."""
       ...
   ```

2. **Type Hints**: Always provide type hints for automatic schema inference.

3. **Error Handling**: Return error messages as strings rather than raising exceptions:
   ```python
   def calculate(expr: str) -> str:
       try:
           return str(eval(expr))
       except Exception as e:
           return f"Error: {e}"
   ```

4. **Simple Return Types**: Return strings, numbers, or simple dictionaries.

### Debugging Best Practices

1. **Inspect History**: Use `inspect_history(n=1)` to see recent predictions:
   ```python
   pred = program(question="...")
   program.named_predictors()['module_name'].inspect_history(n=1)
   ```

2. **Enable Tracking**: Use `track_usage=True` to monitor token consumption:
   ```python
   dspy.settings.configure(track_usage=True)
   result = program(...)
   print(result.get_lm_usage())
   ```

3. **Access Traces**: During optimization, use `trace` parameter in metrics to validate intermediate steps.

4. **Disable Caching**: For debugging, disable cache:
   ```python
   lm = dspy.LM('openai/gpt-4o-mini', cache=False)
   ```

---

## Part 13: Common Patterns

### Pattern 1: Progressive Refinement

Use multiple modules to iteratively improve outputs:

```python
class ProgressiveQA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.draft = dspy.Predict('question -> draft_answer')
        self.refine = dspy.ChainOfThought('question, draft_answer -> final_answer')

    def forward(self, question):
        draft = self.draft(question=question)
        final = self.refine(question=question, draft_answer=draft.draft_answer)
        return final
```

### Pattern 2: Ensemble Decision Making

Combine multiple approaches for robust predictions:

```python
class EnsembleQA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.cot = dspy.ChainOfThought('question -> answer')
        self.pot = dspy.ProgramOfThought('question -> answer')
        self.voter = dspy.Predict('answers: list[str] -> best_answer: str')

    def forward(self, question):
        answer1 = self.cot(question=question).answer
        answer2 = self.pot(question=question).answer

        best = self.voter(answers=[answer1, answer2])
        return best
```

### Pattern 3: Conditional Routing

Route to different modules based on input characteristics:

```python
class ConditionalQA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.classifier = dspy.Predict('question -> needs_code: bool')
        self.text_qa = dspy.ChainOfThought('question -> answer')
        self.code_qa = dspy.ProgramOfThought('question -> answer')

    def forward(self, question):
        needs_code = self.classifier(question=question).needs_code

        if needs_code:
            return self.code_qa(question=question)
        else:
            return self.text_qa(question=question)
```

### Pattern 4: Hierarchical Processing

Break complex tasks into hierarchical subtasks:

```python
class HierarchicalQA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.decompose = dspy.ChainOfThought('question -> subquestions: list[str]')
        self.answer_sub = dspy.ChainOfThought('subquestion -> subanswer')
        self.synthesize = dspy.ChainOfThought('question, subanswers: list[str] -> final_answer')

    def forward(self, question):
        # Decompose into subquestions
        subquestions = self.decompose(question=question).subquestions

        # Answer each subquestion
        subanswers = [
            self.answer_sub(subquestion=sq).subanswer
            for sq in subquestions
        ]

        # Synthesize final answer
        final = self.synthesize(question=question, subanswers=subanswers)
        return final
```

### Pattern 5: Retrieval-Augmented Generation

Standard RAG pattern with DSPy:

```python
class RAG(dspy.Module):
    def __init__(self, k=5):
        super().__init__()
        self.retrieve = dspy.Retrieve(k=k)
        self.generate = dspy.ChainOfThought('context, question -> answer')

    def forward(self, question):
        context = self.retrieve(question).passages
        answer = self.generate(context=context, question=question)
        return dspy.Prediction(context=context, answer=answer.answer)
```

### Pattern 6: Self-Consistency

Generate multiple solutions and pick most common:

```python
class SelfConsistentQA(dspy.Module):
    def __init__(self, n=5):
        super().__init__()
        self.n = n
        self.generate = dspy.ChainOfThought('question -> answer')

    def forward(self, question):
        answers = []
        for i in range(self.n):
            with dspy.context(rollout_id=i, temperature=0.7):
                result = self.generate(question=question)
                answers.append(result.answer)

        # Use majority voting
        from collections import Counter
        most_common = Counter(answers).most_common(1)[0][0]
        return dspy.Prediction(answer=most_common, all_answers=answers)
```

### Pattern 7: Active Retrieval

Iteratively retrieve until sufficient information:

```python
class ActiveRAG(dspy.Module):
    def __init__(self, max_hops=3):
        super().__init__()
        self.max_hops = max_hops
        self.retrieve = dspy.Retrieve(k=3)
        self.assess = dspy.Predict('question, context -> sufficient: bool')
        self.generate = dspy.ChainOfThought('question, context -> answer')

    def forward(self, question):
        context = []

        for hop in range(self.max_hops):
            # Retrieve passages
            new_passages = self.retrieve(question).passages
            context.extend(new_passages)

            # Check if we have enough information
            assessment = self.assess(question=question, context=context)
            if assessment.sufficient:
                break

        # Generate answer
        answer = self.generate(question=question, context=context)
        return answer
```

---

## Part 14: DSPy vs Other Frameworks

### DSPy vs LangChain/LlamaIndex

**From the FAQ:**

> **Q: How does DSPy differ from LangChain/LlamaIndex?**
>
> Those libraries provide pre-built application modules with hand-crafted prompts. DSPy instead offers general-purpose modules that learn to optimize prompts or finetune your model on your specific data and pipeline.

**Key Differences:**

| Aspect | LangChain/LlamaIndex | DSPy |
|--------|---------------------|------|
| **Approach** | Pre-built prompts | Learned prompts |
| **Flexibility** | Fixed templates | Adaptive compilation |
| **Optimization** | Manual tuning | Automatic optimization |
| **Portability** | Prompt-specific | Model-agnostic |
| **Use Case** | Quick prototypes | Production systems |

### When to Use DSPy

From the FAQ:

> **Q: When should I use DSPy?**
>
> DSPy suits researchers and practitioners exploring new pipelines or tasks. It's ideal when you need higher quality outputs through iterative decomposition, improved prompting, data bootstrapping, or finetuning—not for simple string templating.

**Use DSPy when:**
- Building production systems requiring optimization
- Working with multiple language models
- Need reproducible, maintainable AI code
- Want to systematically improve performance
- Building complex multi-step pipelines

**Don't use DSPy when:**
- Simple one-off prompt is sufficient
- No optimization needed
- Extremely tight latency requirements (< 100ms)
- Just need basic string templating

---

## Part 15: Advanced Topics

### Streaming and Async Operations

DSPy supports asynchronous execution:

```python
import asyncio
import dspy

async def main():
    lm = dspy.LM('openai/gpt-4o-mini')
    dspy.configure(lm=lm)

    qa = dspy.ChainOfThought('question -> answer')

    # Async call
    result = await qa.acall(question="What is AI?")
    print(result.answer)

asyncio.run(main())
```

### Parallel Execution

Process multiple examples concurrently:

```python
# Using batch method
qa = dspy.ChainOfThought('question -> answer')
results = qa.batch(
    examples=[
        dspy.Example(question="What is 2+2?"),
        dspy.Example(question="What is 3+3?"),
        dspy.Example(question="What is 4+4?"),
    ],
    num_threads=3
)

# Using Parallel module
parallel = dspy.Parallel(num_threads=4)
results = parallel([
    (qa, dspy.Example(question="Q1")),
    (qa, dspy.Example(question="Q2")),
    (qa, dspy.Example(question="Q3")),
])
```

### State Management

Save and load optimized programs:

```python
# Save
optimized_program.save('model.json')

# Load
new_program = MyModule()
new_program.load('model.json')

# Dump/Load state
state = program.dump_state()
# ... later ...
program.load_state(state)
```

### Custom Adapters

Create custom adapters for specialized formatting:

```python
from dspy.adapters import Adapter

class CustomAdapter(Adapter):
    def format(self, signature, demos, inputs, labels=None):
        # Custom formatting logic
        messages = []
        # ... format messages ...
        return messages

    def parse(self, signature, completion, _parse_values=True):
        # Custom parsing logic
        outputs = {}
        # ... parse outputs ...
        return outputs

# Use it
dspy.configure(adapter=CustomAdapter())
```

### Integration with Existing Tools

#### LangChain Tools

```python
from langchain.tools import Tool as LCTool
import dspy

# Convert LangChain tool
lc_tool = LCTool(name="search", func=search_function, description="...")
dspy_tool = dspy.Tool.from_langchain(lc_tool)

# Use with ReAct
agent = dspy.ReAct(signature="question -> answer", tools=[dspy_tool])
```

#### MCP Tools

```python
import dspy

# Convert MCP tool
dspy_tool = dspy.Tool.from_mcp_tool(session, mcp_tool)

# Use with ReAct
agent = dspy.ReAct(signature="question -> answer", tools=[dspy_tool])
```

### Observability and Monitoring

#### MLflow Integration

DSPy integrates with MLflow for production monitoring:

```python
import mlflow

# Enable tracking
mlflow.dspy.autolog()

# Your DSPy code runs here
# Metrics automatically logged to MLflow
```

#### Custom Callbacks

Use adapter callbacks for custom logging:

```python
from dspy.adapters import BaseCallback

class LoggingCallback(BaseCallback):
    def on_format(self, **kwargs):
        print("Formatting inputs...")

    def on_parse(self, **kwargs):
        print("Parsing outputs...")

adapter = dspy.ChatAdapter(callbacks=[LoggingCallback()])
dspy.configure(adapter=adapter)
```

---

## Part 16: Cheatsheet Quick Reference

### Configuration

```python
# Basic setup
import dspy
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm)

# With settings
dspy.configure(lm=lm, adapter=dspy.JSONAdapter())

# Context manager
with dspy.context(lm=dspy.LM('openai/gpt-3.5-turbo')):
    result = module(...)
```

### Signatures

```python
# Inline
"question -> answer"
"context, question -> answer: str"

# Class-based
class MySignature(dspy.Signature):
    """Task description."""
    input_field: str = dspy.InputField(desc="...")
    output_field: str = dspy.OutputField(desc="...")
```

### Modules

```python
# Predict
predict = dspy.Predict('question -> answer')

# ChainOfThought
cot = dspy.ChainOfThought('question -> answer')

# ProgramOfThought
pot = dspy.ProgramOfThought('question -> answer')

# ReAct
react = dspy.ReAct('question -> answer', tools=[...], max_iters=5)
```

### Data

```python
# Create example
example = dspy.Example(question="Q?", answer="A").with_inputs("question")

# Access
example.question
example.inputs()
example.labels()
```

### Evaluation

```python
# Define metric
def metric(example, pred, trace=None):
    return example.answer == pred.answer

# Evaluate
evaluator = dspy.Evaluate(devset=test_data, metric=metric)
results = evaluator(program)
print(f"Score: {results.score}%")
```

### Optimization

```python
# BootstrapFewShot
from dspy.teleprompt import BootstrapFewShot
optimizer = BootstrapFewShot(metric=metric)
optimized = optimizer.compile(student=program, trainset=train_data)

# MIPROv2
from dspy.teleprompt import MIPROv2
optimizer = MIPROv2(metric=metric, auto="medium")
optimized = optimizer.compile(program, trainset, valset, num_trials=100)
```

### Caching

```python
# Force fresh output
result = predict(question="...", config={"rollout_id": 1, "temperature": 1.0})

# Disable caching
lm = dspy.LM('openai/gpt-4o-mini', cache=False)
```

### Batch Processing

```python
# Using batch
results = module.batch(examples, num_threads=4)

# Using Parallel
parallel = dspy.Parallel(num_threads=4)
results = parallel([(module, ex1), (module, ex2)])
```

### Save/Load

```python
# Save
program.save('model.json')

# Load
program.load('model.json')
```

---

## Part 17: Frequently Asked Questions

### Core Concepts

**Q: What is DSPy's main philosophy?**

DSPy abstracts away interactions between components in LM pipelines, letting developers focus on module-level design. "The same program expressed in 10 or 20 lines of DSPy can easily be compiled into multi-stage instructions for GPT-4, detailed prompts for Llama2-13b, or finetunes for T5-base."

**Q: When should I use DSPy?**

DSPy suits researchers and practitioners exploring new pipelines or tasks. It's ideal when you need higher quality outputs through iterative decomposition, improved prompting, data bootstrapping, or finetuning—not for simple string templating.

### Usage

**Q: What's the typical DSPy workflow?**

Define your task and metrics, prepare example inputs, build your pipeline using modules with signatures, then use an optimizer to compile your code into high-quality instructions or finetuned weights.

**Q: Can I specify multiple outputs?**

Yes. Use comma-separated values after "->" in short-form signatures, or include multiple `dspy.OutputField` instances in long-form signatures.

**Q: How expensive is compilation?**

One reference experiment using BootstrapFewShotWithRandomSearch on GPT-3.5-turbo took approximately 6 minutes with 3,200 API calls, costing around $3 USD.

### Deployment

**Q: How do I save compiled programs?**

Use `.save()` and `.load()` methods: `cot_compiled.save('file.json')` and `cot.load('file.json')`.

**Q: How do I disable caching?**

Set the `cache` parameter to `False` in `dspy.LM()`: `dspy.LM('openai/gpt-4o-mini', cache=False)`.

---

## Part 18: Resources and Community

### Official Resources

- **Website**: https://dspy.ai/
- **Documentation**: https://dspy.ai/learn/
- **GitHub**: https://github.com/stanfordnlp/dspy (29.3k+ stars)
- **Discord**: https://discord.gg/XCGy2WDCQB
- **Twitter**: @DSPyOSS

### Research Papers

1. **Primary Paper (ICLR 2024)**: "DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines"
2. Additional papers on prompt optimization, fine-tuning integration, and multi-stage programs

### Tutorials and Learning Materials

**Official Tutorials:**
- RAG systems
- AI Agents
- Classification
- Entity Extraction
- Math Reasoning
- Multi-Hop Search
- Custom Modules
- Tool Use
- Finetuning

**Community Content:**

**Blogs:**
- "Why I bet on DSPy"
- "Not Your Average Prompt Engineering"
- "Achieving GPT-4 Performance at Lower Cost"

**Videos:**
- "DSPy Explained!" (60K views)
- Structured outputs tutorials
- RAG implementation guides
- Code generation examples

**Podcasts:**
- Weaviate podcasts on MIPRO optimization
- Foundational concepts discussions

### Third-Party Resources

**Weaviate DSPy Directory:**
- 10 notebooks
- 6 podcasts
- Practical applications

### Contributing

DSPy is open-source with 349+ contributors. The project welcomes:
- Bug fixes
- New optimizers
- Documentation improvements
- Use case examples
- Integration with other tools

### Production Use Cases

The documentation notes: "DSPy is deployed in production by many enterprises and startups," with documented real-world use cases including:
- Financial analysis
- Email extraction
- Code generation
- Creative applications
- Agentic systems

---

## Part 19: Links to All Pages Explored

### Core Documentation

1. **Main Page**: https://dspy.ai/
2. **Learn DSPy**: https://dspy.ai/learn/
3. **API Reference**: https://dspy.ai/api/
4. **FAQ**: https://dspy.ai/faqs/
5. **Cheatsheet**: https://dspy.ai/cheatsheet/
6. **Production**: https://dspy.ai/production/
7. **Community**: https://dspy.ai/community/community-resources/

### Programming

8. **Programming Overview**: https://dspy.ai/learn/programming/overview/
9. **Language Models**: https://dspy.ai/learn/programming/language_models/
10. **Signatures**: https://dspy.ai/learn/programming/signatures/
11. **Modules**: https://dspy.ai/learn/programming/modules/
12. **Adapters**: https://dspy.ai/learn/programming/adapters/
13. **Tools**: https://dspy.ai/learn/programming/tools/
14. **MCP**: https://dspy.ai/learn/programming/mcp/

### Evaluation

15. **Evaluation Overview**: https://dspy.ai/learn/evaluation/overview/
16. **Data Handling**: https://dspy.ai/learn/evaluation/data/
17. **Metrics**: https://dspy.ai/learn/evaluation/metrics/

### Optimization

18. **Optimization Overview**: https://dspy.ai/learn/optimization/overview/
19. **Optimizers**: https://dspy.ai/learn/optimization/optimizers/

### Tutorials

20. **Tutorials Overview**: https://dspy.ai/tutorials/
21. **RAG Tutorial**: https://dspy.ai/tutorials/rag/
22. **Agents Tutorial**: https://dspy.ai/tutorials/agents/
23. **Classification**: https://dspy.ai/tutorials/classification/

### API Reference - Modules

24. **Predict**: https://dspy.ai/api/modules/Predict/
25. **ChainOfThought**: https://dspy.ai/api/modules/ChainOfThought/
26. **ReAct**: https://dspy.ai/api/modules/ReAct/
27. **ProgramOfThought**: https://dspy.ai/api/modules/ProgramOfThought/
28. **MultiChainComparison**: https://dspy.ai/api/modules/MultiChainComparison/
29. **Refine**: https://dspy.ai/api/modules/Refine/
30. **BestOfN**: https://dspy.ai/api/modules/BestOfN/
31. **Parallel**: https://dspy.ai/api/modules/Parallel/

### API Reference - Adapters

32. **ChatAdapter**: https://dspy.ai/api/adapters/ChatAdapter/
33. **JSONAdapter**: https://dspy.ai/api/adapters/JSONAdapter/

### API Reference - Optimizers

34. **BootstrapFewShot**: https://dspy.ai/api/optimizers/BootstrapFewShot/
35. **MIPROv2**: https://dspy.ai/api/optimizers/MIPROv2/
36. **COPRO**: https://dspy.ai/api/optimizers/COPRO/

### API Reference - Primitives

37. **Example**: https://dspy.ai/api/primitives/Example/
38. **Prediction**: https://dspy.ai/api/primitives/Prediction/
39. **Image**: https://dspy.ai/api/primitives/Image/
40. **History**: https://dspy.ai/api/primitives/History/
41. **Tool**: https://dspy.ai/api/primitives/Tool/

### API Reference - Signatures

42. **Signature**: https://dspy.ai/api/signatures/Signature/
43. **InputField**: https://dspy.ai/api/signatures/InputField/

### API Reference - Evaluation

44. **Evaluate**: https://dspy.ai/api/evaluation/Evaluate/

### External

45. **GitHub Repository**: https://github.com/stanfordnlp/dspy

---

## Conclusion

DSPy represents a paradigm shift in AI development, moving from brittle prompt engineering to robust, declarative programming. Its three-pillar architecture—Signatures, Modules, and Optimizers—enables developers to build maintainable, portable, and optimizable AI systems.

### Key Takeaways

1. **Programming > Prompting**: Write structured code instead of manually crafting prompts
2. **Declarative Specifications**: Define what you want, not how to get it
3. **Automatic Optimization**: Let DSPy tune prompts and weights for your specific use case
4. **Model Agnostic**: Write once, run on any LM
5. **Composable Modules**: Build complex systems from simple, reusable components
6. **Systematic Improvement**: Follow the three-stage workflow (Programming → Evaluation → Optimization)

### Getting Started

1. Install DSPy: `pip install dspy`
2. Configure an LM: `dspy.configure(lm=dspy.LM('openai/gpt-4o-mini'))`
3. Create a simple module: `qa = dspy.ChainOfThought('question -> answer')`
4. Build evaluation data and metrics
5. Optimize with BootstrapFewShot or MIPROv2
6. Deploy your optimized program

### Future Directions

DSPy continues to evolve with:
- Enhanced optimizers (GEPA, SIMBA)
- Better integration with production tools (MLflow, observability)
- Expanded adapter support
- Growing community contributions

**The DSPy philosophy**: Treat AI development like traditional software engineering—structured, testable, maintainable, and continuously improvable.

---

**Report End**

*For the latest updates, visit https://dspy.ai/ or join the Discord community.*
