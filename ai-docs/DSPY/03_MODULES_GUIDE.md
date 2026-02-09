---
title: "DSPy Modules: Composable Components"
category: foundation
priority: high
prerequisites:
  - foundation/01_DSPY_FUNDAMENTALS.md
  - foundation/02_SIGNATURES_GUIDE.md
related:
  - foundation/04_OPTIMIZATION_GUIDE.md
  - research/MULTI_AGENT_SYSTEMS.md
implementation_phase: 1
estimated_reading_time: "60 minutes"
version: "1.0"
key_concepts:
  - dspy.Module
  - Module composition
  - Built-in modules
  - Custom modules
  - ReAct agents
learning_objectives:
  - "Understand what DSPy modules are and why they matter"
  - "Learn to use built-in modules (Predict, ChainOfThought, ReAct)"
  - "Create custom modules for specific tasks"
  - "Compose modules into pipelines"
---

# DSPy Modules: Composable Components

DSPy Modules are the building blocks of any DSPy system. They encapsulate language model behavior in reusable, composable Python classes.

## What is a Module?

A **module** is a Python class that inherits from `dspy.Module` and implements a `forward()` method. It uses signatures to define its interface and orchestrates language model calls.

**Key Insight**: Modules abstract away the complexity of prompt engineering while providing clean, reusable components.

```python
import dspy

class SimpleQA(dspy.Module):
    """A simple question-answering module."""

    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict("question -> answer")

    def forward(self, question):
        return self.predict(question=question)

# Usage
qa = SimpleQA()
result = qa(question="What is machine learning?")
print(result.answer)
```

---

## Built-in Modules

### 1. Predict (Basic Prediction)

Simplest module - takes input through signature, returns output.

```python
# Simple string signature
predict = dspy.Predict("question -> answer")

# Class-based signature for more control
class QASignature(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

predict = dspy.Predict(QASignature)

result = predict(question="What is DSPy?")
```

**When to use**: Simple input → output transformations

---

### 2. ChainOfThought (Multi-Step Reasoning)

Prompts the model to reason through a problem step-by-step before producing output.

```python
class ChainOfThoughtQA(dspy.Module):
    def __init__(self):
        super().__init__()
        # ChainOfThought adds intermediate reasoning
        self.generate = dspy.ChainOfThought("question -> reasoning, answer")

    def forward(self, question):
        return self.generate(question=question)

# Produces both reasoning and answer
qa = ChainOfThoughtQA()
result = qa(question="Why is water important?")
print(result.reasoning)  # Model's step-by-step thinking
print(result.answer)     # Final answer
```

**When to use**: Complex reasoning tasks, multi-step problems, classification with explanation

---

### 3. ReAct (Reasoning + Acting)

Combines reasoning with tool calling - the model can call external tools and use results.

```python
def search_wikipedia(query: str) -> str:
    """Search Wikipedia for information."""
    # In real use, call actual Wikipedia API
    return f"Information about {query}"

def calculate(expression: str) -> float:
    """Evaluate mathematical expression."""
    return eval(expression)

class ResearchAgent(dspy.Module):
    def __init__(self):
        super().__init__()
        self.agent = dspy.ReAct(
            signature="query -> research_result",
            tools=[search_wikipedia, calculate],
            max_iters=5  # Max reasoning steps
        )

    def forward(self, query):
        return self.agent(query=query)

# Agent can now call tools autonomously
agent = ResearchAgent()
result = agent(query="What is the capital of France and what's 2+2?")
```

**When to use**: Tasks requiring external information, multi-step tool usage, autonomous agents

---

### 4. ProgramOfThought & CodeAct

For tasks requiring code generation and execution.

```python
class CodeGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate = dspy.ProgramOfThought(
            "problem -> code_solution"
        )

    def forward(self, problem):
        # Generates Python code as output
        return self.generate(problem=problem)

# Generate code to solve problems
gen = CodeGenerator()
result = gen(problem="Generate a function to find fibonacci numbers")
```

---

### 5. Refine (Iterative Improvement)

Tries a task multiple times, refining based on intermediate results.

```python
def quality_check(example, pred, trace=None):
    """Check if output quality is acceptable."""
    return len(pred.answer) > 50  # Example: answer should be substantial

refine = dspy.Refine(
    dspy.ChainOfThought("question -> answer"),
    metric=quality_check,
    num_attempts=3
)

result = refine(question="Explain machine learning")
```

---

## Creating Custom Modules

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
```

### Pattern 2: Multi-Step Pipeline

```python
class ResearchPipeline(dspy.Module):
    """Multi-step research workflow."""

    def __init__(self):
        super().__init__()
        self.search = dspy.Predict("topic -> search_queries")
        self.analyze = dspy.ChainOfThought("papers -> synthesis, conclusion")

    def forward(self, topic):
        # Step 1: Generate search queries
        queries = self.search(topic=topic)

        # Step 2: Synthesize results
        result = self.analyze(papers=queries.search_queries)

        return result
```

### Pattern 3: Expert System (For CLIO Agent)

```python
class DataExpert(dspy.Module):
    """Expert for data analysis tasks."""

    def __init__(self):
        super().__init__()
        self.agent = dspy.ReAct(
            signature="task -> analysis, recommendations, tools_used",
            tools=[
                analyze_hdf5,
                optimize_compression,
                convert_format,
            ],
            max_iters=5
        )

    def forward(self, task):
        return self.agent(task=task)

# Orchestrator routes to this expert
class Orchestrator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.data_expert = DataExpert()
        self.router = dspy.ChainOfThought("task -> expert_choice")

    def forward(self, task):
        routing = self.router(task=task)
        if "data" in routing.expert_choice.lower():
            return self.data_expert(task=task)
```

---

## Module Composition

### Chaining Modules

```python
class AnalysisPipeline(dspy.Module):
    def __init__(self):
        super().__init__()
        self.extract = dspy.Predict("text -> key_concepts")
        self.summarize = dspy.ChainOfThought("concepts -> summary")
        self.rate = dspy.Predict("summary -> quality_score")

    def forward(self, text):
        # Chain modules together
        concepts = self.extract(text=text)
        summary = self.summarize(concepts=concepts.key_concepts)
        score = self.rate(summary=summary.summary)

        return {
            "concepts": concepts.key_concepts,
            "summary": summary.summary,
            "quality": score.quality_score
        }
```

### Conditional Logic

```python
class SmartRouter(dspy.Module):
    def __init__(self):
        super().__init__()
        self.classifier = dspy.Predict("input -> category")
        self.technical = dspy.ChainOfThought("input -> technical_answer")
        self.creative = dspy.Predict("input -> creative_answer")

    def forward(self, input_text):
        # Classify first
        classification = self.classifier(input=input_text)

        # Route based on classification
        if "technical" in classification.category.lower():
            return self.technical(input=input_text)
        else:
            return self.creative(input=input_text)
```

---

## Module Lifecycle

### Initialization
```python
def __init__(self):
    super().__init__()
    # Initialize child modules
    self.predictor = dspy.Predict("input -> output")
```

### Forward Pass
```python
def forward(self, **inputs):
    # Implement module logic
    # Call child modules
    # Return results
    return dspy.Prediction(output=result)
```

### Configuration
```python
# Set language model for this module
with dspy.context(lm=specific_lm):
    result = module(input=data)

# Save module state
module.save("path/to/module.json")

# Load module state
module.load("path/to/module.json")
```

---

## Best Practices

### 1. Keep Modules Focused
Each module should have a clear, single responsibility.

### 2. Use Type Hints
```python
def forward(self, question: str) -> dspy.Prediction:
    return self.predict(question=question)
```

### 3. Document Behavior
```python
class MyModule(dspy.Module):
    """Clear description of what this module does.

    Usage:
        module = MyModule()
        result = module(input="data")
    """
```

### 4. Compose Strategically
Build complex systems from simple, testable modules.

### 5. Handle Failures
```python
def forward(self, input_text):
    try:
        result = self.predict(input=input_text)
        return result
    except Exception as e:
        # Fallback behavior
        return dspy.Prediction(output="Error occurred")
```

---

## Common Patterns in CLIO Agent

### Expert Module Pattern
```python
class Expert(dspy.Module):
    """Base expert with tools."""

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

### Orchestrator Pattern
```python
class Orchestrator(dspy.Module):
    """Route to appropriate expert."""

    def __init__(self, experts):
        super().__init__()
        self.router = dspy.ChainOfThought("task -> expert, reasoning")
        self.experts = experts

    def forward(self, task):
        routing = self.router(task=task)
        expert = self.experts.get(routing.expert)
        return expert(task=task)
```

---

## Summary

- **Modules** are reusable DSPy components inheriting from `dspy.Module`
- **Built-in modules** cover common patterns (Predict, ChainOfThought, ReAct, etc.)
- **Custom modules** compose built-ins and other modules
- **Composition** enables complex AI systems from simple pieces
- **Optimization** works on modules to improve performance

Next: Learn about [optimization](04_OPTIMIZATION_GUIDE.md) to improve module performance from data.
