# DSPy 3.x Fundamentals
> Version: dspy-ai 3.1.3 | Updated: February 2026

Core concepts and getting started with DSPy - the declarative framework for programming language models.

---

## Table of Contents

- [Installation](#installation)
- [Core Concepts](#core-concepts)
- [Language Models (dspy.LM)](#language-models-dspylm)
- [Configuration (dspy.configure)](#configuration-dspyconfigure)
- [Context Management (dspy.context)](#context-management-dspycontext)
- [Signatures](#signatures)
- [Examples and Predictions](#examples-and-predictions)
- [Modules](#modules)
- [Evaluation](#evaluation)
- [Usage Tracking](#usage-tracking)
- [Advanced Types](#advanced-types)
- [CLIO Integration](#clio-integration)

---

## Installation

### Using pip
```bash
pip install dspy-ai
```

### Using uv (recommended for CLIO)
```bash
uv add dspy-ai
```

### Development version
```bash
pip install git+https://github.com/stanfordnlp/dspy.git
```

### With optional dependencies
```bash
pip install "dspy-ai[mcp]"  # MCP protocol support
```

---

## Core Concepts

### What is DSPy?

DSPy is a **declarative framework for building modular AI software**. Instead of manual prompt engineering, you write structured Python code that describes AI behavior declaratively.

**Core philosophy:** Programming—rather than prompting—language models.

### The Three Pillars

1. **Signatures** - Declarative I/O specifications (what the module does)
2. **Modules** - Composable building blocks (how it's done)
3. **Optimizers** - Automatic tuning of prompts and weights (making it better)

### Key Benefits

- **Portable:** Same code works across GPT-4, Llama, Claude, etc.
- **Optimizable:** Automatic prompt engineering via optimizers
- **Composable:** Build complex programs from simple modules
- **Type-safe:** Full Python type hints and Pydantic validation

---

## Language Models (dspy.LM)

### Basic Setup

```python
import dspy

# Configure OpenAI
lm = dspy.LM('openai/gpt-4o-mini', api_key='YOUR_OPENAI_API_KEY')
dspy.configure(lm=lm)
```

### Constructor Parameters

```python
lm = dspy.LM(
    model: str,                    # Format: "provider/model-name"
    api_key: str | None = None,    # API key (or use environment variable)
    api_base: str | None = None,   # Custom API endpoint URL
    temperature: float = 0.0,      # Sampling temperature (0.0-2.0)
    max_tokens: int = 1000,        # Maximum output tokens
    cache: bool = True,            # Enable response caching
    num_retries: int = 3,          # Number of retry attempts
    model_type: str = "chat",      # "chat" or "responses"
    **kwargs                       # Provider-specific parameters
)
```

### Provider Format

DSPy uses the format `"provider/model-name"`:

```python
# OpenAI
dspy.LM('openai/gpt-4o-mini')
dspy.LM('openai/gpt-4o')
dspy.LM('openai/gpt-3.5-turbo')

# Anthropic
dspy.LM('anthropic/claude-sonnet-4-5-20250929')
dspy.LM('anthropic/claude-opus-4')

# Google Gemini
dspy.LM('gemini/gemini-2.5-pro-preview-03-25')
dspy.LM('gemini/gemini-1.5-flash')

# Ollama (local)
dspy.LM('ollama_chat/llama3.2', api_base='http://localhost:11434', api_key='')
dspy.LM('ollama_chat/mistral')

# SGLang (local)
dspy.LM('openai/meta-llama/Meta-Llama-3-8B-Instruct',
        api_base='http://localhost:7501/v1',
        api_key='',
        model_type='chat')

# Databricks
dspy.LM('databricks/databricks-meta-llama-3-1-70b-instruct')

# Custom OpenAI-compatible
dspy.LM('openai/your-model-name',
        api_key='PROVIDER_API_KEY',
        api_base='https://your-provider-url.com')
```

### Environment Variables

DSPy respects standard environment variables:

```bash
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export GEMINI_API_KEY="..."
```

Then use without `api_key` parameter:

```python
lm = dspy.LM('openai/gpt-4o-mini')  # Uses OPENAI_API_KEY
```

### Direct LM Calls

You can call LM directly (bypasses DSPy modules):

```python
lm = dspy.LM('openai/gpt-4o-mini')

# String input
response = lm("Say this is a test!", temperature=0.7)

# Message format
response = lm(messages=[
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
])
```

### History Inspection

Every LM call is logged in `.history`:

```python
lm = dspy.LM('openai/gpt-4o-mini')
lm("Test query")

# View history
print(len(lm.history))  # Number of calls
last_call = lm.history[-1]

# Available keys:
# - prompt: Original prompt string
# - messages: Formatted messages
# - kwargs: Call parameters
# - response: Raw LM response
# - outputs: Parsed outputs
# - usage: Token counts
# - cost: Estimated cost
# - timestamp: Unix timestamp
# - uuid: Unique call ID
# - model: Model identifier
```

### Per-Call Configuration

Override LM settings per call:

```python
# At initialization
lm = dspy.LM('openai/gpt-4o-mini', temperature=0.9, max_tokens=3000, cache=False)

# Per call
lm("Test", rollout_id=1, temperature=1.0)

# In DSPy modules
predict = dspy.Predict("question -> answer")
predict(question="What is 1+1?", config={"rollout_id": 5, "temperature": 1.0})
```

**Note:** `rollout_id` forces a fresh request while maintaining caching semantics.

---

## Configuration (dspy.configure)

### Global Configuration

`dspy.configure()` sets global defaults (thread-safe):

```python
import dspy

lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(
    lm=lm,                          # Default language model
    adapter=dspy.ChatAdapter(),     # Adapter for prompt formatting
    rm=colbert_retriever,           # Retrieval model (optional)
    track_usage=True                # Enable usage tracking
)
```

### Parameters

- `lm` - Language model instance
- `adapter` - Adapter for formatting (ChatAdapter, JSONAdapter, XMLAdapter)
- `rm` - Retrieval model for dspy.Retrieve
- `track_usage` - Enable token usage tracking (default: False)

### Thread Safety

`dspy.configure()` is **thread-safe**. Each thread can have its own configuration.

---

## Context Management (dspy.context)

### Scoped Configuration Override

Use `dspy.context()` to temporarily override configuration:

```python
# Global config
dspy.configure(lm=dspy.LM('openai/gpt-4o-mini'))

qa = dspy.ChainOfThought('question -> answer')

# Use different model for specific calls
with dspy.context(lm=dspy.LM('openai/gpt-3.5-turbo')):
    response = qa(question="Fast query")

# Back to default model
response = qa(question="Regular query")
```

### Context Parameters

All `dspy.configure()` parameters work in contexts:

```python
with dspy.context(
    lm=dspy.LM('anthropic/claude-opus-4'),
    adapter=dspy.JSONAdapter(),
    track_usage=True,
    allow_tool_async_sync_conversion=True
):
    result = complex_module(input_data)
```

### Nested Contexts

Contexts can be nested:

```python
with dspy.context(lm=fast_model):
    # Use fast model here
    with dspy.context(lm=smart_model):
        # Use smart model here
        pass
    # Back to fast model
```

---

## Signatures

### What is a Signature?

A signature is a **declarative specification of input/output behavior** for a DSPy module.

Think of it as a function signature that describes what a module does, not how it does it.

### Inline Signatures (String Format)

Simplest form: `"input_fields -> output_fields"`

```python
# Basic
"question -> answer"

# With type hints
"sentence -> sentiment: bool"

# Multiple inputs
"context, question -> answer"

# With type annotations
"context: list[str], question: str -> answer: str"

# Multiple outputs
"question, choices: list[str] -> reasoning: str, selection: int"
```

### Inline with Instructions

```python
import dspy

signature = dspy.Signature(
    "comment -> toxic: bool",
    instructions="Mark as 'toxic' if comment includes insults, harassment, or sarcastic derogatory remarks."
)

classify = dspy.Predict(signature)
result = classify(comment="You're amazing!")
print(result.toxic)  # False
```

### Class-Based Signatures

For complex signatures, use class-based format:

```python
class BasicQA(dspy.Signature):
    """Answer questions with short factoid answers."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="often between 1 and 5 words")

qa = dspy.ChainOfThought(BasicQA)
result = qa(question="What is the capital of France?")
```

### InputField and OutputField

**dspy.InputField:**
```python
dspy.InputField(
    desc: str = "",           # Description for the LM
    prefix: str = "",         # Prefix in prompt
    format: Callable = None,  # Custom formatting function
    **kwargs                  # Passed to pydantic.Field
)
```

**dspy.OutputField:**
```python
dspy.OutputField(
    desc: str = "",           # Description for the LM
    prefix: str = "",         # Prefix in prompt (e.g., "Reasoning: Let's think step by step")
    format: Callable = None,  # Custom formatting function
    **kwargs                  # Passed to pydantic.Field
)
```

**Example:**
```python
class CheckCitationFaithfulness(dspy.Signature):
    """Verify that the text is based on the provided context."""
    context: str = dspy.InputField(desc="facts here are assumed to be true")
    text: str = dspy.InputField()
    faithfulness: bool = dspy.OutputField()
    evidence: dict[str, list[str]] = dspy.OutputField(desc="Supporting evidence for claims")
```

### Type Annotations

DSPy supports rich type annotations:

```python
from typing import Literal, Any
import pydantic

# Literal types (enums)
class Emotion(dspy.Signature):
    """Classify emotion."""
    sentence: str = dspy.InputField()
    sentiment: Literal['sadness', 'joy', 'love', 'anger', 'fear', 'surprise'] = dspy.OutputField()

# Lists
class MultiClassify(dspy.Signature):
    message: str = dspy.InputField()
    categories: list[str] = dspy.OutputField()

# Dicts
class StructuredOutput(dspy.Signature):
    query: str = dspy.InputField()
    result: dict[str, Any] = dspy.OutputField()

# Pydantic models
class QueryResult(pydantic.BaseModel):
    text: str
    score: float

class Search(dspy.Signature):
    query: str = dspy.InputField()
    result: QueryResult = dspy.OutputField()
```

### Multimodal Signatures

DSPy supports images and audio:

```python
class DogBreedClassifier(dspy.Signature):
    """Output the dog breed of the dog in the image."""
    image_1: dspy.Image = dspy.InputField(desc="An image of a dog")
    answer: str = dspy.OutputField(desc="The dog breed")

classify = dspy.Predict(DogBreedClassifier)
result = classify(image_1=dspy.Image(path="/path/to/dog.jpg"))
```

---

## Examples and Predictions

### dspy.Example

**Creating examples:**
```python
import dspy

example = dspy.Example(
    question="What is 2+2?",
    answer="4"
)

# With multiple fields
example = dspy.Example(
    context="Paris is the capital of France.",
    question="What is the capital of France?",
    answer="Paris"
)
```

### Marking Inputs vs Labels

Use `.with_inputs()` to distinguish inputs from labels:

```python
example = dspy.Example(
    question="What is 2+2?",
    answer="4"
).with_inputs("question")

# Access separately
inputs = example.inputs()    # {"question": "What is 2+2?"}
labels = example.labels()    # {"answer": "4"}
```

**Multiple inputs:**
```python
example = dspy.Example(
    context="...",
    question="...",
    answer="..."
).with_inputs("context", "question")
```

### dspy.Prediction

Output from a DSPy module:

```python
cot = dspy.ChainOfThought("question -> answer")
pred = cot(question="What is 1+1?")

# Access fields
print(pred.answer)      # "2"
print(pred.reasoning)   # "Let's think step by step..."

# Get completions (if n > 1)
cot = dspy.ChainOfThought("question -> answer", n=5)
pred = cot(question="What is 1+1?")
print(pred.completions.answer)  # List of 5 answers
```

### Usage Tracking

```python
import dspy

dspy.configure(track_usage=True)

cot = dspy.ChainOfThought("question -> answer")
pred = cot(question="What is 2+2?")

usage = pred.get_lm_usage()
print(usage)
# {'prompt_tokens': 123, 'completion_tokens': 45, 'total_tokens': 168}
```

---

## Modules

### Base Class: dspy.Module

All DSPy programs inherit from `dspy.Module`:

```python
class MyProgram(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predictor = dspy.ChainOfThought("question -> answer")

    def forward(self, question):
        return self.predictor(question=question)

program = MyProgram()
result = program(question="What is DSPy?")
```

**Key methods:**
- `forward(**kwargs)` - Main logic (must implement)
- `save(path)` - Save to JSON
- `load(path)` - Load from JSON
- `named_predictors()` - Iterate over predictors
- `batch(inputs, batch_size=10)` - Batch processing

### dspy.Predict

Basic predictor (no modification to signature):

```python
predict = dspy.Predict("question -> answer")
result = predict(question="What is 1+1?")
print(result.answer)
```

**With n completions:**
```python
predict = dspy.Predict("question -> answer", n=5)
result = predict(question="What is 1+1?")
print(result.completions.answer)  # List of 5 answers
```

### dspy.ChainOfThought

Adds reasoning step before answer:

```python
cot = dspy.ChainOfThought("question -> answer")
result = cot(question="How many floors are in the castle David Gregory inherited?")

print(result.reasoning)  # "Let's think step by step. David Gregory inherited..."
print(result.answer)     # "3"
```

**What it does:**
- Prepends `reasoning: str` field to signature
- Default prefix: "Reasoning: Let's think step by step in order to"
- LM generates reasoning, then final answer

**Custom rationale field:**
```python
cot = dspy.ChainOfThought(
    "question -> answer",
    rationale_field=dspy.OutputField(prefix="Analysis: "),
    rationale_field_type=str
)
```

### dspy.ProgramOfThought

Generates and executes Python code:

```python
pot = dspy.ProgramOfThought("question -> answer")
result = pot(question="Sarah has 5 apples. She buys 7 more apples. How many apples does Sarah have now?")
print(result.answer)  # 12
```

**What it does:**
- LM generates Python code
- Code is executed in safe interpreter
- Result is used as answer

### dspy.ReAct

Reasoning + Acting agent with tools:

```python
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny."

def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Search results for: {query}"

react = dspy.ReAct(
    signature="question -> answer",
    tools=[get_weather, search_web],
    max_iters=10
)

result = react(question="What is the weather in Tokyo?")
print(result.answer)
print(result.trajectory)  # List of steps
```

**Tool requirements:**
- Type hints on parameters and return value
- Docstring (becomes tool description)
- Supported types: str, int, bool, float, dict, list, Pydantic models

**Trajectory:**
```python
for step in result.trajectory:
    print(step['next_thought'])
    print(step['next_tool_name'])
    print(step['next_tool_args'])
    print(step['observation'])
```

### dspy.CodeAct

Similar to ProgramOfThought but with explicit tools:

```python
def factorial(n: int) -> int:
    """Calculate factorial of n."""
    if n == 1:
        return 1
    return n * factorial(n-1)

act = dspy.CodeAct("n -> factorial", tools=[factorial])
result = act(n=5)
print(result.factorial)  # 120
```

**Available from:** dspy 3.0.4b2+

### dspy.BestOfN

Generate N outputs, select best by reward:

```python
def one_word_answer(args, pred):
    return 1.0 if len(pred.answer.split()) == 1 else 0.0

qa = dspy.ChainOfThought("question -> answer")
best = dspy.BestOfN(
    module=qa,
    N=3,
    reward_fn=one_word_answer,
    threshold=1.0
)

result = best(question="What is the capital of Belgium?")
print(result.answer)  # "Brussels"
```

### dspy.Refine

Iterative refinement based on reward:

```python
refine = dspy.Refine(
    module=qa,
    N=3,
    reward_fn=one_word_answer,
    threshold=1.0,
    fail_count=1  # Max failures before stopping
)

result = refine(question="What is the capital of Belgium?")
```

### dspy.Parallel

Execute multiple predictions in parallel:

```python
parallel = dspy.Parallel(num_threads=2)
predict = dspy.Predict("question -> answer")

results = parallel([
    (predict, dspy.Example(question="1+1").with_inputs("question")),
    (predict, dspy.Example(question="2+2").with_inputs("question"))
])
```

---

## Evaluation

### Basic Metric

```python
def exact_match(example: dspy.Example, pred: dspy.Prediction, trace=None) -> float:
    return 1.0 if example.answer == pred.answer else 0.0
```

### Using dspy.Evaluate

```python
from dspy.evaluate import Evaluate

# Prepare dataset
devset = [
    dspy.Example(question="What is 1+1?", answer="2").with_inputs("question"),
    dspy.Example(question="What is 2+2?", answer="4").with_inputs("question"),
]

# Create evaluator
evaluator = Evaluate(
    devset=devset,
    metric=exact_match,
    num_threads=4,
    display_progress=True,
    display_table=5
)

# Evaluate program
program = dspy.ChainOfThought("question -> answer")
score = evaluator(program)
print(f"Accuracy: {score}")
```

### LLM-as-Judge

```python
class FactJudge(dspy.Signature):
    """Judge if the answer is factually correct based on the context."""
    context = dspy.InputField(desc="Context for the prediction")
    question = dspy.InputField(desc="Question to be answered")
    answer = dspy.InputField(desc="Answer for the question")
    factually_correct: bool = dspy.OutputField(desc="Is the answer factually correct?")

judge = dspy.ChainOfThought(FactJudge)

def factuality_metric(example, pred, trace=None):
    result = judge(
        context=example.context,
        question=example.question,
        answer=pred.answer
    )
    return 1.0 if result.factually_correct else 0.0
```

---

## Usage Tracking

### Enable Tracking

```python
import dspy

dspy.configure(track_usage=True)
```

### Get Usage from Prediction

```python
cot = dspy.ChainOfThought("question -> answer")
pred = cot(question="What is 2+2?")

usage = pred.get_lm_usage()
print(usage)
# {
#   'prompt_tokens': 123,
#   'completion_tokens': 45,
#   'total_tokens': 168
# }
```

### Get Usage from LM History

```python
lm = dspy.LM('openai/gpt-4o-mini')
dspy.configure(lm=lm, track_usage=True)

# Make some calls
cot(question="Q1")
cot(question="Q2")

# Check history
for call in lm.history:
    print(call['usage'])
    print(call['cost'])  # Estimated cost
```

---

## Advanced Types

### dspy.History

Frozen Pydantic model for conversation history:

```python
lm = dspy.LM('openai/gpt-4o-mini')
lm("Hello")
lm("How are you?")

# Access history
history = lm.history
print(len(history))  # 2

# Last call
last = history[-1]
print(last['messages'])
print(last['response'])
print(last['timestamp'])
```

### dspy.Tool

Wrapper for functions in ReAct/CodeAct:

```python
def search(query: str) -> str:
    """Search the web for information."""
    return f"Results for: {query}"

tool = dspy.Tool(search)
print(tool.name)  # "search"
print(tool.desc)  # "Search the web for information."
print(tool.args)  # {"query": {"type": "string", ...}}

# Call tool
result = tool(query="DSPy framework")

# Async call
result = await tool.acall(query="DSPy framework")
```

### dspy.ToolCalls

Represents model output with tool invocations:

```python
# Available in dspy 3.0.4b2+
react = dspy.ReAct("question -> answer", tools=[...])
result = react(question="...")

# Access tool calls
for call in result.tool_calls:
    print(call.name)
    print(call.args)

# Execute tools
output = result.tool_calls.execute(functions=[tool1, tool2])
```

### dspy.Code

Type annotation for code:

```python
class CodeGen(dspy.Signature):
    """Generate Python code."""
    task: str = dspy.InputField()
    code: dspy.Code["python"] = dspy.OutputField()

gen = dspy.Predict(CodeGen)
result = gen(task="Write a factorial function")
print(result.code)
```

### dspy.Image

Multimodal image input:

```python
class ImageClassifier(dspy.Signature):
    image: dspy.Image = dspy.InputField()
    label: str = dspy.OutputField()

classifier = dspy.Predict(ImageClassifier)
result = classifier(image=dspy.Image(path="/path/to/image.jpg"))
```

### dspy.Audio

Multimodal audio input:

```python
class Transcribe(dspy.Signature):
    audio: dspy.Audio = dspy.InputField()
    text: str = dspy.OutputField()

transcriber = dspy.Predict(Transcribe)
result = transcriber(audio=dspy.Audio(path="/path/to/audio.mp3"))
```

---

## CLIO Integration

### How CLIO Configures DSPy

CLIO Agent uses DSPy internally for its expert agents and optimizers. Here's how:

**1. Language Model Configuration:**
```python
# CLIO configures DSPy with LiteLLM proxy
lm = dspy.LM(
    model="openai/gpt-4o-mini",  # Or other configured model
    api_base="http://localhost:4000",  # LiteLLM proxy
    cache=True,
    track_usage=True
)
dspy.configure(lm=lm)
```

**2. Expert Agents as DSPy Modules:**
```python
class DataExpert(dspy.Module):
    def __init__(self):
        super().__init__()
        self.signature = dspy.Signature(
            "query: str, context: dict -> analysis: str, recommendations: list[str]",
            instructions="Analyze scientific data and provide insights."
        )
        self.predictor = dspy.ChainOfThought(self.signature)

    def forward(self, query, context):
        return self.predictor(query=query, context=context)
```

**3. Optimizer Integration:**
CLIO uses DSPy optimizers (MIPROv2, BootstrapFewShot) in its Optimizer Layer to improve agent performance over time.

**4. ARC Memory Integration:**
CLIO stores DSPy predictions and metrics in ARC (Adaptive Retrieval Cache) for performance tracking and optimization.

### CLIO-Specific Notes

- **Internal Use:** DSPy is an internal implementation detail in CLIO, not exposed in public APIs
- **Adapter Choice:** CLIO uses `ChatAdapter` for compatibility with all LLM providers
- **Usage Tracking:** Always enabled for performance monitoring
- **Caching:** Enabled for tool result caching in ARC

---

## Quick Reference

### Common Patterns

**Simple prediction:**
```python
predict = dspy.Predict("question -> answer")
result = predict(question="What is 1+1?")
```

**Chain of thought:**
```python
cot = dspy.ChainOfThought("question -> answer")
result = cot(question="Complex question")
print(result.reasoning, result.answer)
```

**With tools:**
```python
react = dspy.ReAct("question -> answer", tools=[tool1, tool2], max_iters=10)
result = react(question="...")
```

**Scoped LM:**
```python
with dspy.context(lm=fast_lm):
    result = module(input)
```

**Batch processing:**
```python
results = module.batch([ex1, ex2, ex3], batch_size=10)
```

### Best Practices

1. **Use type hints:** Always annotate signature fields
2. **Track usage:** Enable `track_usage=True` for monitoring
3. **Cache by default:** Keep `cache=True` unless testing
4. **Use contexts:** Scope configuration changes with `dspy.context()`
5. **Modular design:** Break complex programs into multiple modules
6. **Test first:** Validate on small datasets before optimization
7. **Metric quality:** Invest time in good evaluation metrics

---

## Next Steps

- **[00_DSPY_API_REFERENCE.md](00_DSPY_API_REFERENCE.md)** - Complete API reference
- **[02_SIGNATURES_GUIDE.md](02_SIGNATURES_GUIDE.md)** - Deep dive on signatures
- **[03_MODULES_GUIDE.md](03_MODULES_GUIDE.md)** - Advanced module patterns
- **[04_OPTIMIZATION_GUIDE.md](04_OPTIMIZATION_GUIDE.md)** - Optimizer usage
- **Official Docs:** https://dspy.ai/

---

**Remember:** DSPy is about **programming**, not prompting. Write structured code, let DSPy handle the prompts.
