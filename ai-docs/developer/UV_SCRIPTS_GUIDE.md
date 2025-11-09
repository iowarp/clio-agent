---
title: "UV Scripts Guide"
category: reference
priority: high
prerequisites: []
related:
  - poc/chat.py
  - poc/orchestrator.py
  - poc/config.py
implementation_phase: 1
estimated_reading_time: "30 minutes"
version: "1.0"
---

# UV Scripts Guide

UV allows self-contained scripts without virtual environments.

## Basic Structure

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
#   "rich>=13.0.0",
# ]
# ///

"""Module docstring."""

import dspy
# ... code ...

if __name__ == "__main__":
    main()
```

## Running Scripts

```bash
# Just run it - UV handles everything
uv run script.py

# With arguments
uv run script.py arg1 arg2

# No installation needed!
```

## Adding Dependencies

Edit the dependencies section:

```python
# dependencies = [
#   "dspy-ai>=2.6.0",           # Add here
#   "rich>=13.0.0",             # Add here
#   "requests>=2.31.0",         # New dependency
# ]
```

## Common Patterns

### With Click CLI
```python
# dependencies = ["click>=8.1.0"]

import click

@click.command()
@click.option('--name', prompt='Name')
def main(name):
    print(f"Hello {name}")

if __name__ == "__main__":
    main()
```

### With DSPy Configuration
```python
# dependencies = ["dspy-ai>=2.6.0"]

import dspy

lm = dspy.LM('ollama_chat/llama3.1:8b')
dspy.configure(lm=lm)

result = dspy.Predict("input -> output")(input="test")
```

## Advantages

✅ No virtual environment setup
✅ Dependencies inline and clear
✅ Reproducible execution
✅ Perfect for HPC clusters
✅ Easy to version and deploy

---
