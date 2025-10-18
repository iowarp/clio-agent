---
title: "DSPy Optimization: Self-Improving Systems"
category: foundation
priority: high
prerequisites:
  - foundation/01_DSPY_FUNDAMENTALS.md
  - foundation/02_SIGNATURES_GUIDE.md
  - foundation/03_MODULES_GUIDE.md
related:
  - architecture/OPTIMIZATION_STRATEGY.md
  - research/OPTIMIZATION_ALGORITHMS.md
implementation_phase: 1|3
estimated_reading_time: "75 minutes"
version: "1.0"
key_concepts:
  - Training sets
  - Quality metrics
  - BootstrapFewShot
  - MIPROv2
  - Optimization workflow
learning_objectives:
  - "Understand why modules need optimization"
  - "Learn to build training sets and metrics"
  - "Master BootstrapFewShot for quick improvements"
  - "Understand MIPROv2 for production optimization"
  - "Build evaluation frameworks"
---

# DSPy Optimization: Self-Improving Systems

The power of DSPy lies in its ability to **automatically improve module performance** through optimization. Instead of manually tuning prompts, you collect data and let optimizers improve your system.

## Why Optimize?

### The Problem with Manual Prompts

```python
# Manual approach - requires constant tweaking
prompt = """You are a data expert. Analyze this file:
1. Check compression
2. Validate chunking
3. Provide recommendations
... (50 more lines of manual instructions)
"""
```

### The DSPy Solution

```python
# Declarative approach - optimize automatically
class DataExpert(dspy.Module):
    def __init__(self):
        super().__init__()
        self.analyze = dspy.ChainOfThought(DataExpertSignature)

    def forward(self, task):
        return self.analyze(task=task)

# Collect usage examples
trainset = load_usage_logs("data/logs/")

# Optimize
optimizer = dspy.BootstrapFewShot(metric=quality_metric)
optimized = optimizer.compile(DataExpert(), trainset=trainset)

# Result: Automatically improved prompts!
```

---

## The Three-Stage Workflow

### Stage 1: Design Your Module

Define what you want using signatures. No worrying about prompts yet.

```python
class ExpertSignature(dspy.Signature):
    """Expert analysis."""
    task: str = dspy.InputField()
    analysis: str = dspy.OutputField()
    recommendations: list[str] = dspy.OutputField()

expert = dspy.Module()
expert.predict = dspy.ChainOfThought(ExpertSignature)
```

### Stage 2: Collect Examples

Gather real usage data or create examples.

```python
# From real usage logs
trainset = load_usage_logs("data/logs/", min_examples=10)

# Or create manually
trainset = [
    dspy.Example(
        task="Optimize this HDF5 file",
        analysis="File uses poor compression",
        recommendations=["Use GZIP-6", "Rechunk to 100x100"]
    ).with_inputs("task"),
    # ... more examples
]

# Or generate synthetically
def generate_example():
    # Use a strong LM to create examples
    pass

synthetic = [generate_example() for _ in range(100)]
trainset = real_examples + synthetic
```

### Stage 3: Optimize

Run an optimizer to improve the module.

```python
# Define quality metric
def metric(example, pred, trace=None):
    # Compare predictions to expected output
    return check_quality(example, pred)

# Choose optimizer
optimizer = dspy.BootstrapFewShot(metric=metric)

# Compile (optimize the module)
optimized = optimizer.compile(expert, trainset=trainset)

# Save
optimized.save("compiled_expert.json")
```

---

## Building Quality Metrics

A quality metric evaluates if your module is working well.

### Metric Structure

```python
def quality_metric(example, pred, trace=None):
    """Evaluate module quality.

    Args:
        example: The training example with expected output
        pred: Module's prediction
        trace: Tool call history (if using ReAct)

    Returns:
        Score 0.0-1.0 (higher is better)
    """

    # Check 1: Does output match expected?
    correctness = (example.expected in pred.answer)

    # Check 2: Is output substantive?
    has_content = len(pred.answer) > 50

    # Check 3: Efficiency (for ReAct agents)
    if trace:
        tool_calls = [t for t in trace if hasattr(t, 'tool_name')]
        efficient = len(tool_calls) <= 3
    else:
        efficient = True

    # Combine scores
    if trace is None:  # Evaluation mode
        return (correctness + has_content + efficient) / 3.0
    else:  # Bootstrapping mode
        return correctness and has_content and efficient
```

### For Different Task Types

**Question Answering**:
```python
def qa_metric(example, pred):
    # Check if answer contains key terms
    return all(keyword in pred.answer.lower()
               for keyword in example.key_terms)
```

**Routing/Classification**:
```python
def routing_metric(example, pred):
    # Check if correct expert was selected
    return pred.expert_choice == example.correct_expert
```

**Tool Calling**:
```python
def tool_metric(example, pred, trace):
    # Check if correct tools were called with correct args
    correct_calls = all(
        check_tool_call(t, example)
        for t in pred.trajectory
    )
    return correct_calls and len(pred.trajectory) <= 5
```

**Multi-Dimensional (ClaudIO)**:
```python
def claudio_metric(example, pred, trace=None):
    """Multi-dimensional quality for experts."""

    correctness = validate_answer(example, pred)
    comprehensiveness = len(pred.analysis) > 100
    actionable = validate_recommendations(pred.recommendations)

    if trace:
        tool_efficiency = len(trace) <= 3
    else:
        tool_efficiency = True

    if trace is None:  # Evaluation
        return (correctness + comprehensiveness +
                actionable + tool_efficiency) / 4.0
    else:  # Bootstrapping
        return correctness and actionable
```

---

## BootstrapFewShot: Quick Optimization

For rapid iteration with 10-50 examples.

```python
# 1. Prepare training examples (minimum 10)
trainset = load_examples(min_count=10, max_count=50)

# 2. Define metric
def my_metric(example, pred, trace=None):
    return evaluate_quality(example, pred)

# 3. Create optimizer
optimizer = dspy.BootstrapFewShot(
    metric=my_metric,
    max_bootstrapped_demos=4,  # Few-shot examples
    max_labeled_demos=6,        # Maximum training examples
    num_threads=4               # Parallel processing
)

# 4. Compile
optimized_module = optimizer.compile(
    student=my_module,
    trainset=trainset
)

# 5. Evaluate on test set
test_score = evaluate(optimized_module, testset)

# 6. Save
optimized_module.save("optimized_v1.json")
```

**Cost & Time**:
- Examples: 10-50
- Cost: $2-5 (with GPT-4o-mini)
- Time: 10-30 minutes
- Expected improvement: +15-25%

---

## MIPROv2: Production Optimization

For production-quality systems with 200+ examples.

```python
# 1. Collect substantial training data
trainset = load_examples(min_count=200, max_count=500)

# 2. Define comprehensive metric
def production_metric(example, pred, trace=None):
    # Multi-dimensional evaluation
    return evaluate_all_dimensions(example, pred, trace)

# 3. Create MIPROv2 optimizer
optimizer = dspy.MIPROv2(
    metric=production_metric,
    auto='medium',              # Search intensity
    num_trials=50,              # Number of optimization attempts
    max_new_examples=10,        # Examples to synthesize
)

# 4. Compile
optimized_module = optimizer.compile(
    student=my_module,
    trainset=trainset,
    dev_set=devset,             # For validation
)

# 5. Evaluate thoroughly
test_score = evaluate(optimized_module, testset)
print(f"Improvement: {test_score - baseline_score:.1%}")

# 6. Save with metadata
optimized_module.save("optimized_v2.json")
with open("optimization_report.txt", "w") as f:
    f.write(f"Score: {test_score}\nImprovement: +{score_gain:.1%}")
```

**Cost & Time**:
- Examples: 200+ required
- Cost: $10-30 (with GPT-4o)
- Time: 1-2 hours
- Expected improvement: +30-50%

---

## Optimization Workflow for ClaudIO

### Phase 1: Fast Prototyping (Week 1)

```
1. Define 3 experts
2. Create basic module
3. Manual testing (no optimization)
4. Baseline: ~60% performance
```

### Phase 2: Quick Improvement (Week 2)

```
1. Collect 20-30 usage examples
2. Define quality metric
3. Run BootstrapFewShot
4. Result: +15-25% improvement
```

### Phase 3: Production Optimization (Week 3-4)

```
1. Collect 200+ usage examples
2. Refine metric based on failures
3. Run MIPROv2
4. A/B test: old vs new
5. Result: +30-50% improvement
6. Deploy to production
```

### Phase 4: Continuous Learning (Ongoing)

```
1. Collect new usage examples weekly
2. Re-run MIPROv2 monthly
3. Track performance trends
4. Update deployed versions
```

---

## Managing Training Data

### Collecting Examples

```python
class UsageLogger:
    def log_example(self, task, output):
        """Log interaction for future optimization."""
        example = dspy.Example(
            task=task,
            output=output,
            timestamp=datetime.now()
        )
        self.save_to_disk(example)

# Usage
logger = UsageLogger()
result = expert(task="Optimize HDF5 file")
logger.log_example(task, result)
```

### Cleaning Data

```python
def clean_trainset(trainset):
    """Remove duplicates and low-quality examples."""
    # Remove exact duplicates
    unique = list(set(trainset))

    # Remove failures
    valid = [ex for ex in unique if ex.is_valid]

    # Remove outliers
    cleaned = filter_outliers(valid)

    return cleaned

trainset = clean_trainset(raw_examples)
```

### Splitting Data

```python
# 80% training, 20% testing
split_point = int(0.8 * len(data))
trainset = data[:split_point]
testset = data[split_point:]

# Or use k-fold cross-validation
from sklearn.model_selection import KFold
kf = KFold(n_splits=5)
for train_idx, test_idx in kf.split(data):
    train_data = data[train_idx]
    test_data = data[test_idx]
```

---

## Handling Common Issues

### Not Enough Training Data

```python
# Option 1: Generate synthetic examples
def generate_synthetic(num_examples=100):
    strong_lm = dspy.LM('openai/gpt-4')
    with dspy.context(lm=strong_lm):
        examples = [create_example() for _ in range(num_examples)]
    return examples

synthetic = generate_synthetic()
mixed_trainset = real_examples + synthetic
```

### Expensive Optimization

```python
# Use cheaper model for optimization
cheap_lm = dspy.LM('openai/gpt-4o-mini')
with dspy.context(lm=cheap_lm):
    optimizer = dspy.BootstrapFewShot(metric=metric)
    optimized = optimizer.compile(module, trainset=trainset)

# Use production model for inference
prod_lm = dspy.LM('ollama_chat/llama3.1:8b')
dspy.configure(lm=prod_lm)
result = optimized(input=data)
```

### Poor Quality Metrics

```python
# Start simple
def simple_metric(example, pred):
    return pred.answer == example.expected_answer

# Gradually add dimensions
def better_metric(example, pred):
    exact_match = pred.answer == example.expected_answer
    contains_keywords = all(kw in pred.answer for kw in example.keywords)
    return (exact_match + contains_keywords) / 2.0
```

---

## Deployment Patterns

### Development Workflow

```python
# Develop with local LM
dev_lm = dspy.LM('ollama_chat/llama3.1:8b')
dspy.configure(lm=dev_lm)

expert = DataExpert()
result = expert(task="test")
```

### Optimization Workflow

```python
# Optimize with cloud LM
opt_lm = dspy.LM('openai/gpt-4o-mini')
with dspy.context(lm=opt_lm):
    optimizer = dspy.BootstrapFewShot(metric=metric)
    optimized = optimizer.compile(expert, trainset=trainset)

# Save
optimized.save("expert_v1.json")
```

### Production Workflow

```python
# Deploy with local LM (zero cost)
prod_lm = dspy.LM('ollama_chat/llama3.1:8b')
dspy.configure(lm=prod_lm)

# Load optimized expert
expert = DataExpert()
expert.load("expert_v1.json")

# Run inference
result = expert(task=user_input)
```

---

## Measuring Success

### Metrics to Track

```python
class OptimizerMetrics:
    def track(self, baseline, optimized, testset):
        baseline_score = evaluate(baseline, testset)
        optimized_score = evaluate(optimized, testset)

        improvement = optimized_score - baseline_score
        improvement_pct = improvement / baseline_score

        return {
            "baseline": baseline_score,
            "optimized": optimized_score,
            "absolute_gain": improvement,
            "percent_gain": improvement_pct,
            "success": improvement_pct > 0.15  # 15% improvement threshold
        }
```

### Success Criteria for ClaudIO

```python
SUCCESS_CRITERIA = {
    "Phase 2 (BootstrapFewShot)": {
        "min_examples": 20,
        "target_improvement": 0.15,  # 15%
        "max_cost": "$5"
    },
    "Phase 3 (MIPROv2)": {
        "min_examples": 200,
        "target_improvement": 0.30,  # 30%
        "max_cost": "$30"
    },
    "Production": {
        "accuracy": 0.85,
        "tool_efficiency": 0.50,  # 50% reduction in unnecessary calls
        "cost_per_query": "$0.001"
    }
}
```

---

## Summary

- **Optimization** automatically improves module performance
- **Metrics** define what "good performance" means
- **BootstrapFewShot** provides quick iteration (10-50 examples, 15-25% gain)
- **MIPROv2** enables production quality (200+ examples, 30-50% gain)
- **Workflow** progresses from manual → quick optimization → production optimization
- **Cost** is reasonable when using appropriate models

Next: Implement [ClaudIO Architecture](../architecture/CLAUDIO_ARCHITECTURE.md) using these foundation concepts.
