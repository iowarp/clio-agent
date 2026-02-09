# DSPy 3.x API Reference
> Version: dspy-ai 3.1.3 | Updated: February 2026

Complete API reference for DSPy 3.x - the declarative framework for building modular AI software. DSPy enables building AI programs from natural-language modules and optimizes them into effective prompts and weights for language models.

---

## Table of Contents

- [Models](#models)
- [Signatures](#signatures)
- [Modules](#modules)
- [Adapters](#adapters)
- [Primitives](#primitives)
- [Optimizers](#optimizers)
- [Evaluation](#evaluation)
- [Tools](#tools)
- [Utilities](#utilities)
- [Experimental](#experimental)

---

## Models

### dspy.LM
**Language Model wrapper for provider-agnostic LLM access.**

```python
lm = dspy.LM(
    model: str,                    # Format: "provider/model-name"
    api_key: str | None = None,    # API key (or use env var)
    api_base: str | None = None,   # Custom API endpoint
    temperature: float = 0.0,      # Sampling temperature
    max_tokens: int = 1000,        # Max output tokens
    cache: bool = True,            # Enable response caching
    num_retries: int = 3,          # Retry failed requests
    model_type: str = "chat",      # "chat" or "responses"
    **kwargs                       # Provider-specific params
)
```

**Direct calling:**
```python
lm("Say this is a test!", temperature=0.7)
lm(messages=[{"role": "user", "content": "Hello"}])
```

**History inspection:**
```python
lm.history  # List of all calls with prompts, responses, usage, cost
```

**See:** [01_DSPY_FUNDAMENTALS.md](01_DSPY_FUNDAMENTALS.md#dspylm)

---

### dspy.Embedder
**Embedding model wrapper for semantic search and similarity.**

```python
from sentence_transformers import SentenceTransformer
embedder = dspy.Embedder(SentenceTransformer("all-MiniLM-L6-v2").encode)
```

**Used in:**
- KNNFewShot optimizer for example selection
- Semantic retrieval systems

---

## Signatures

### dspy.Signature
**Declarative specification of input/output behavior for DSPy modules.**

**Inline format:**
```python
dspy.Predict("question -> answer")
dspy.Predict("sentence -> sentiment: bool")
dspy.Predict("context: list[str], question: str -> answer: str")
```

**With instructions:**
```python
dspy.Signature(
    "comment -> toxic: bool",
    instructions="Mark as 'toxic' if comment includes insults or harassment."
)
```

**Class-based format:**
```python
class MySignature(dspy.Signature):
    """Task description here."""
    input_field: str = dspy.InputField(desc="Input description")
    output_field: str = dspy.OutputField(desc="Output description")
```

**See:** [01_DSPY_FUNDAMENTALS.md](01_DSPY_FUNDAMENTALS.md#signatures)

---

### dspy.InputField
**Defines an input field in a signature.**

```python
dspy.InputField(
    desc: str = "",           # Field description
    prefix: str = "",         # Prefix in prompt
    format: Callable = None,  # Custom formatter
    **kwargs                  # Passed to pydantic.Field
)
```

**Example:**
```python
class QA(dspy.Signature):
    context: str = dspy.InputField(desc="Facts assumed to be true")
    question: str = dspy.InputField()
```

---

### dspy.OutputField
**Defines an output field in a signature.**

```python
dspy.OutputField(
    desc: str = "",           # Field description
    prefix: str = "",         # Prefix in prompt
    format: Callable = None,  # Custom formatter
    **kwargs                  # Passed to pydantic.Field
)
```

**Example:**
```python
class Emotion(dspy.Signature):
    sentence: str = dspy.InputField()
    sentiment: Literal['joy', 'anger', 'fear'] = dspy.OutputField()
```

---

## Modules

### dspy.Module
**Base class for all DSPy programs. Subclass to create custom modules.**

```python
class MyProgram(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predictor = dspy.ChainOfThought("question -> answer")

    def forward(self, question):
        return self.predictor(question=question)
```

**Methods:**
- `forward(**kwargs)` - Main execution logic (must implement)
- `save(path)` - Save module state to JSON
- `load(path)` - Load module state from JSON
- `named_predictors()` - Iterator over all predictors
- `batch(inputs, batch_size=10)` - Batch process inputs

---

### dspy.Predict
**Basic predictor. Handles prompting and inference without modification.**

```python
predict = dspy.Predict(signature="question -> answer")
result = predict(question="What is 1+1?")
print(result.answer)
```

**Parameters:**
- `signature` - Task signature (string or class)
- `n` - Number of completions (default: 1)
- `temperature` - Override LM temperature
- `**config` - Additional LM config

---

### dspy.ChainOfThought
**Teaches LM to think step-by-step before responding.**

```python
cot = dspy.ChainOfThought("question -> answer", n=5)
result = cot(question="How many floors in the castle?")
print(result.reasoning)  # Step-by-step reasoning
print(result.answer)     # Final answer
```

**Adds field:** `reasoning: str` to signature

---

### dspy.ReAct
**Reasoning and Acting agent that uses tools to solve tasks.**

```python
def get_weather(city: str) -> str:
    """Get weather for a city."""
    return f"Weather in {city}: sunny"

react = dspy.ReAct(
    signature="question -> answer",
    tools=[get_weather],
    max_iters=10
)
result = react(question="What's the weather in Tokyo?")
print(result.answer)
print(result.trajectory)  # List of reasoning steps and tool calls
```

**Parameters:**
- `signature` - Task signature
- `tools` - List of callable functions or dspy.Tool objects
- `max_iters` - Maximum iterations (default: 20)

**See:** [01_DSPY_FUNDAMENTALS.md](01_DSPY_FUNDAMENTALS.md#tools-and-agents)

---

### dspy.CodeAct
**Agent that generates and executes Python code to solve tasks.**

```python
def factorial(n: int) -> int:
    """Calculate factorial of n."""
    if n == 1:
        return 1
    return n * factorial(n-1)

act = dspy.CodeAct("n -> factorial", tools=[factorial])
result = act(n=5)
```

**Available from:** dspy 3.0.4b2+

---

### dspy.ProgramOfThought
**Teaches LM to output executable code for reasoning.**

```python
pot = dspy.ProgramOfThought("question -> answer")
result = pot(question="Sarah has 5 apples. She buys 7 more. How many total?")
print(result.answer)
```

Generates Python code, executes it, and uses result as answer.

---

### dspy.RLM
**Recursive Language Model for exploring large contexts via Python REPL.**

```python
rlm = dspy.RLM(signature="document -> summary")
result = rlm(document=large_text)
```

---

### dspy.BestOfN
**Generate N predictions and select best based on reward function.**

```python
def one_word_metric(args, pred):
    return 1.0 if len(pred.answer.split()) == 1 else 0.0

qa = dspy.ChainOfThought("question -> answer")
best_of_3 = dspy.BestOfN(
    module=qa,
    N=3,
    reward_fn=one_word_metric,
    threshold=1.0
)
result = best_of_3(question="What is the capital of Belgium?")
```

---

### dspy.Refine
**Iteratively refine predictions using feedback from reward function.**

```python
refine = dspy.Refine(
    module=qa,
    N=3,
    reward_fn=one_word_metric,
    threshold=1.0,
    fail_count=1  # Max failures before giving up
)
result = refine(question="What is the capital of Belgium?")
```

---

### dspy.Parallel
**Execute multiple predictions in parallel threads.**

```python
parallel = dspy.Parallel(num_threads=2)
predict = dspy.Predict("question -> answer")
results = parallel([
    (predict, dspy.Example(question="1+1").with_inputs("question")),
    (predict, dspy.Example(question="2+2").with_inputs("question"))
])
```

---

### dspy.MultiChainComparison
**Compare multiple ChainOfThought outputs to produce final prediction.**

```python
mcc = dspy.MultiChainComparison("question -> answer")
result = mcc(question="Complex question requiring comparison")
```

---

## Adapters

Adapters bridge DSPy modules and LMs by formatting signatures and parsing responses.

### dspy.ChatAdapter
**Default adapter. Universal compatibility with all LMs.**

```python
dspy.configure(adapter=dspy.ChatAdapter())
```

**Format:** Uses `[[ ## field_name ## ]]` markers
**Advantages:** Works with all models, automatic fallback
**Disadvantages:** Higher latency (more tokens)

---

### dspy.JSONAdapter
**Uses native JSON generation via `response_format` parameter.**

```python
dspy.configure(adapter=dspy.JSONAdapter(use_native_function_calling=True))
```

**Format:** Inputs use markers, outputs are JSON objects
**Advantages:** Structured output, lower latency
**Disadvantages:** Requires model support (OpenAI, Anthropic, Gemini)

---

### dspy.XMLAdapter
**XML-based formatting for models that prefer XML structure.**

```python
dspy.configure(adapter=dspy.XMLAdapter())
```

---

### dspy.TwoStepAdapter
**Two-stage prompting for complex reasoning tasks.**

```python
dspy.configure(adapter=dspy.TwoStepAdapter())
```

---

## Primitives

### dspy.Example
**Training example with inputs and optional labels.**

```python
example = dspy.Example(
    question="What is 2+2?",
    answer="4"
)

# Mark inputs vs labels
example_with_inputs = example.with_inputs("question")
inputs_dict = example.inputs()  # Only input fields
labels_dict = example.labels()  # Only label fields
```

**See:** [01_DSPY_FUNDAMENTALS.md](01_DSPY_FUNDAMENTALS.md#dspyexample)

---

### dspy.Prediction
**Output from a DSPy module with metadata.**

```python
pred = module(question="test")
print(pred.answer)
print(pred.reasoning)
usage = pred.get_lm_usage()  # Token usage stats
```

**Methods:**
- `from_completions()` - Create from raw completions
- `score()` - Score against metric
- `get_lm_usage()` - Get token usage (requires `track_usage=True`)

---

### dspy.History
**Frozen Pydantic model representing conversation history.**

```python
# Access from LM
lm.history[-1]  # Last call
# Keys: prompt, messages, kwargs, response, outputs, usage, cost, timestamp, uuid, model
```

**Properties:** Immutable conversation threading for stateful agents

---

### dspy.Tool
**Wrapper for functions used by ReAct/CodeAct agents.**

```python
def search(query: str) -> str:
    """Search the web for information."""
    return f"Results for {query}"

tool = dspy.Tool(search)
print(tool.name)  # "search"
print(tool.desc)  # "Search the web for information."
print(tool.args)  # {"query": {"type": "string", ...}}
```

**Async support:**
```python
await tool.acall(query="test")
```

---

### dspy.ToolCalls
**Represents model output containing tool invocations.**

```python
# Available in dspy 3.0.4b2+
tool_calls = result.tool_calls
for call in tool_calls:
    print(call.name, call.args)

# Execute tools
output = tool_calls.execute(functions=[tool1, tool2])
```

---

### dspy.Code
**Type annotation for code generation tasks.**

```python
class CodeGen(dspy.Signature):
    """Generate Python code."""
    task: str = dspy.InputField()
    code: dspy.Code["python"] = dspy.OutputField()

gen = dspy.Predict(CodeGen)
result = gen(task="Write a factorial function")
```

---

### dspy.Image
**Multimodal image input type.**

```python
class DogBreed(dspy.Signature):
    """Classify dog breed from image."""
    image_1: dspy.Image = dspy.InputField(desc="Dog photo")
    breed: str = dspy.OutputField()

classify = dspy.Predict(DogBreed)
result = classify(image_1=dspy.Image(path="/path/to/dog.jpg"))
```

---

### dspy.Audio
**Multimodal audio input type.**

```python
class Transcribe(dspy.Signature):
    audio: dspy.Audio = dspy.InputField()
    text: str = dspy.OutputField()
```

---

## Optimizers

### MIPROv2
**State-of-the-art prompt and demonstration optimizer.**

```python
from dspy.teleprompt import MIPROv2

optimizer = MIPROv2(
    metric=your_metric,
    auto="light",  # "light", "medium", or "heavy"
)
optimized = optimizer.compile(
    program,
    trainset=trainset,
    max_bootstrapped_demos=3,
    max_labeled_demos=4
)
optimized.save("mipro_optimized")
```

**Zero-shot (instructions only):**
```python
optimized = optimizer.compile(
    program, trainset=trainset,
    max_bootstrapped_demos=0, max_labeled_demos=0
)
```

---

### BootstrapFewShot
**Generate few-shot examples via bootstrapping.**

```python
from dspy.teleprompt import BootstrapFewShot

optimizer = BootstrapFewShot(
    metric=your_metric,
    max_bootstrapped_demos=4,
    max_labeled_demos=16,
    max_rounds=1,
    max_errors=10,
    teacher_settings=dict(lm=gpt4)  # Optional stronger teacher
)
compiled = optimizer.compile(student=program, trainset=trainset)
```

---

### BootstrapFewShotWithRandomSearch
**Bootstrap with random search over hyperparameters.**

```python
from dspy.teleprompt import BootstrapFewShotWithRandomSearch

optimizer = BootstrapFewShotWithRandomSearch(
    metric=your_metric,
    max_bootstrapped_demos=2,
    num_candidate_programs=8,
    num_threads=4
)
compiled = optimizer.compile(
    student=program,
    trainset=trainset,
    valset=devset
)
```

---

### BootstrapFewShotWithOptuna
**Bootstrap with Optuna hyperparameter optimization.**

```python
from dspy.teleprompt import BootstrapFewShotWithOptuna

optimizer = BootstrapFewShotWithOptuna(
    metric=your_metric,
    max_bootstrapped_demos=2,
    num_candidate_programs=8,
    num_threads=4
)
compiled = optimizer.compile(student=program, trainset=trainset, valset=devset)
```

---

### BootstrapFinetune
**Bootstrap then finetune a model on generated examples.**

```python
from dspy.teleprompt import BootstrapFinetune

config = dict(
    target="huggingface/model-name",
    epochs=2,
    bf16=True,
    bsize=6,
    accumsteps=2,
    lr=5e-5
)
optimizer = BootstrapFinetune(metric=your_metric)
finetuned = optimizer.compile(program, trainset=trainset, **config)
```

---

### COPRO
**Automatic prompt optimization via meta-prompting.**

```python
from dspy.teleprompt import COPRO

optimizer = COPRO(
    prompt_model=lm,
    metric=your_metric,
    breadth=10,  # New prompts per iteration
    depth=3,     # Optimization rounds
    init_temperature=1.4
)
compiled = optimizer.compile(program, trainset=trainset)
```

---

### SIMBA
**Similarity-based optimization.**

```python
from dspy.teleprompt import SIMBA

optimizer = SIMBA(
    metric=your_metric,
    max_steps=12,
    max_demos=10
)
compiled = optimizer.compile(student=program, trainset=trainset)
```

---

### GEPA
**Guided Evolution for Prompt Adaptation.**

```python
from dspy.teleprompt import GEPA

optimizer = GEPA(metric=your_metric)
compiled = optimizer.compile(program, trainset=trainset)
```

---

### GRPO
**Group Relative Policy Optimization for RL-based tuning.**

```python
from dspy.teleprompt import GRPO

optimizer = GRPO(metric=your_metric)
compiled = optimizer.compile(program, trainset=trainset)
```

---

### KNNFewShot
**K-nearest neighbors example selection at inference time.**

```python
from sentence_transformers import SentenceTransformer
from dspy.teleprompt import KNNFewShot

optimizer = KNNFewShot(
    k=3,
    trainset=trainset,
    vectorizer=dspy.Embedder(SentenceTransformer("all-MiniLM-L6-v2").encode)
)
compiled = optimizer.compile(student=dspy.ChainOfThought("question -> answer"))
```

---

### LabeledFewShot
**Use labeled examples directly as demonstrations.**

```python
from dspy.teleprompt import LabeledFewShot

optimizer = LabeledFewShot(k=8)
compiled = optimizer.compile(student=program, trainset=trainset)
```

---

### Ensemble
**Combine multiple programs via voting or custom reduce function.**

```python
from dspy.teleprompt.ensemble import Ensemble

ensemble = Ensemble(reduce_fn=dspy.majority)
programs = [prog1, prog2, prog3]
ensemble_prog = ensemble.compile(programs)
```

---

### BetterTogether
**Co-optimize multiple models together.**

```python
from dspy.teleprompt import BetterTogether

optimizer = BetterTogether(metric=your_metric)
compiled = optimizer.compile([model1, model2], trainset=trainset)
```

---

## Evaluation

### dspy.Evaluate
**Evaluate a program against a dataset using a metric.**

```python
from dspy.evaluate import Evaluate

evaluator = Evaluate(
    devset=devset,
    metric=your_metric,
    num_threads=4,
    display_progress=True,
    display_table=10  # Show first N rows
)
score = evaluator(program)
```

**Metric function signature:**
```python
def metric(example: dspy.Example, pred: dspy.Prediction, trace=None) -> float:
    return 1.0 if pred.answer == example.answer else 0.0
```

---

### dspy.SemanticF1
**Semantic F1 score for text similarity evaluation.**

```python
from dspy.evaluate import SemanticF1

metric = SemanticF1()
score = metric(gold="Paris is the capital", pred="Capital of France: Paris")
```

---

### answer_exact_match
**Exact string matching metric.**

```python
from dspy.evaluate import answer_exact_match

score = answer_exact_match(example, prediction)
```

---

### answer_passage_match
**Check if answer appears in passage.**

```python
from dspy.evaluate import answer_passage_match

score = answer_passage_match(example, prediction)
```

---

## Tools

### dspy.ColBERTv2
**Dense retrieval system for document search.**

```python
colbert = dspy.ColBERTv2(url='http://20.102.90.50:2017/wiki17_abstracts')
dspy.configure(rm=colbert)

retriever = dspy.Retrieve(k=3)
results = retriever("What is DSPy?")
print(results.passages)
```

---

### dspy.Retrieve
**Generic retrieval module (requires configured retriever).**

```python
dspy.configure(rm=your_retriever)
retrieve = dspy.Retrieve(k=5)
results = retrieve(query="search query")
```

---

### dspy.PythonInterpreter
**Safe Python code execution environment.**

```python
from dspy.tools import PythonInterpreter

interpreter = PythonInterpreter()
result = interpreter.execute("print(2 + 2)")
```

---

## Utilities

### dspy.configure
**Global thread-safe configuration.**

```python
dspy.configure(
    lm=dspy.LM("openai/gpt-4o-mini"),
    adapter=dspy.ChatAdapter(),
    rm=colbert_retriever,
    track_usage=True
)
```

---

### dspy.context
**Scoped configuration override (context manager).**

```python
with dspy.context(lm=dspy.LM("openai/gpt-3.5-turbo")):
    result = module(question="test")
```

**See:** [01_DSPY_FUNDAMENTALS.md](01_DSPY_FUNDAMENTALS.md#configuration)

---

### dspy.streamify
**Convert module to streaming output.**

```python
import asyncio

stream_predict = dspy.streamify(
    predict,
    stream_listeners=[dspy.streaming.StreamListener(signature_field_name="answer")]
)

async def read_stream():
    output_stream = stream_predict(question="Why?")
    async for chunk in output_stream:
        print(chunk, end="")

asyncio.run(read_stream())
```

---

### dspy.asyncify
**Convert synchronous module to async.**

```python
async_module = dspy.asyncify(dspy.ChainOfThought("question -> answer"))
result = await async_module(question="What is DSPy?")
```

---

### dspy.syncify
**Convert async module to synchronous.**

```python
sync_module = dspy.syncify(async_module)
result = sync_module(question="test")
```

---

### track_usage
**Enable token usage tracking.**

```python
dspy.configure(track_usage=True)
result = predict(question="test")
print(result.get_lm_usage())  # {'prompt_tokens': X, 'completion_tokens': Y, ...}
```

---

### dspy.configure_cache
**Configure caching behavior.**

```python
dspy.configure_cache(
    enable_disk_cache=False,
    enable_memory_cache=False
)
```

---

### dspy.inspect_history
**Debug utility to view LM call history.**

```python
dspy.inspect_history(lm, n=5)  # Show last 5 calls
```

---

### dspy.load
**Load saved module from disk.**

```python
program = YourProgramClass()
program.load("path/to/saved_program.json")
```

---

### disable_logging / enable_logging
**Control DSPy logging output.**

```python
dspy.disable_logging()
# ... quiet operations ...
dspy.enable_logging()
```

---

### disable_litellm_logging / enable_litellm_logging
**Control LiteLLM logging.**

```python
dspy.disable_litellm_logging()
```

---

## Experimental

### dspy.Citations
**Track citations in generated text.**

```python
class CitedAnswer(dspy.Signature):
    question: str = dspy.InputField()
    citations: dspy.Citations = dspy.OutputField()
```

---

### dspy.Document
**Document representation for retrieval systems.**

```python
doc = dspy.Document(
    text="DSPy is a framework...",
    metadata={"source": "docs"}
)
```

---

## Quick Start

```python
import dspy

# 1. Configure language model
lm = dspy.LM('openai/gpt-4o-mini', api_key='YOUR_KEY')
dspy.configure(lm=lm)

# 2. Define signature
signature = "question -> answer"

# 3. Create module
cot = dspy.ChainOfThought(signature)

# 4. Execute
result = cot(question="What is 2+2?")
print(result.reasoning)
print(result.answer)

# 5. Optimize (optional)
from dspy.teleprompt import BootstrapFewShot

def metric(example, pred, trace=None):
    return example.answer == pred.answer

optimizer = BootstrapFewShot(metric=metric)
compiled = optimizer.compile(student=cot, trainset=trainset)
```

---

**For detailed usage and examples, see:**
- [01_DSPY_FUNDAMENTALS.md](01_DSPY_FUNDAMENTALS.md)
- Official docs: https://dspy.ai/
