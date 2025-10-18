---
title: "DSPy API Reference"
category: reference
priority: medium
prerequisites: []
related:
  - foundation/01_DSPY_FUNDAMENTALS.md
  - foundation/03_MODULES_GUIDE.md
implementation_phase: 1
estimated_reading_time: "30 minutes"
version: "1.0"
---

# DSPy API Quick Reference

## Core Classes

### dspy.Module
Base class for all modules.

```python
class MyModule(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predictor = dspy.Predict("input -> output")
    
    def forward(self, input):
        return self.predictor(input=input)
```

### dspy.Signature
Declarative I/O specification.

```python
class MySignature(dspy.Signature):
    input: str = dspy.InputField(desc="Input description")
    output: str = dspy.OutputField(desc="Output description")
```

### dspy.Example
Training example for optimization.

```python
example = dspy.Example(
    input="test",
    output="expected_output"
).with_inputs("input")
```

## Modules

```python
dspy.Predict(signature)           # Simple prediction
dspy.ChainOfThought(signature)    # Multi-step reasoning
dspy.ReAct(signature, tools)      # Reasoning + tool calling
dspy.Refine(module, metric)       # Iterative improvement
```

## Optimizers

```python
dspy.BootstrapFewShot(metric)     # Quick optimization
dspy.MIPROv2(metric)              # Production optimization
dspy.GEPA(metric)                 # Reflective evolution
```

## Configuration

```python
dspy.configure(lm=language_model)
dspy.context(lm=language_model)   # Temporary override
```

## Language Models

```python
lm = dspy.LM('openai/gpt-4o')
lm = dspy.LM('ollama_chat/llama3.1:8b')
lm = dspy.LM('openai/gpt-4o-mini')
```

---

Full docs: https://dspy.ai
