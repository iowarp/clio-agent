# DSPy 3.x Optimization Guide
> Version: dspy-ai 3.1.3 | Updated: February 2026

Comprehensive guide to DSPy optimization techniques, covering all optimizers from simple few-shot learning to advanced genetic algorithms and reinforcement learning approaches.

---

## Overview: What is Optimization?

DSPy optimization (also called "compilation") is the process of automatically improving program performance by finding better prompts, demonstrations, and hyperparameters. Instead of manual prompt engineering, you:

1. **Define** what you want (signatures + modules)
2. **Collect** examples (training data)
3. **Specify** quality metrics
4. **Optimize** automatically

**Result**: The optimizer searches for the best prompts and demonstrations that maximize your metric on the training data.

---

## The Compilation Pattern

All DSPy optimizers follow this standard pattern:

```python
import dspy

# 1. Define your module
class MyModule(dspy.Module):
    def __init__(self):
        self.predictor = dspy.ChainOfThought('input -> output')

    def forward(self, input):
        return self.predictor(input=input)

# 2. Create training data
trainset = [
    dspy.Example(input="example 1", output="result 1").with_inputs("input"),
    dspy.Example(input="example 2", output="result 2").with_inputs("input"),
    # ... more examples
]

# 3. Define metric
def metric(example, pred, trace=None):
    """Evaluate prediction quality (0.0 to 1.0 or bool)."""
    return example.output == pred.output

# 4. Create optimizer
optimizer = dspy.BootstrapFewShot(metric=metric)

# 5. Compile (optimize)
optimized_module = optimizer.compile(
    student=MyModule(),
    trainset=trainset
)

# 6. Use optimized module
result = optimized_module(input="new query")
```

---

## Metric Functions

Metrics evaluate prediction quality. They determine what "good" means for your task.

### Metric Signature

```python
def metric(example, pred, trace=None):
    """
    Args:
        example: Training example with expected outputs
        pred: Model's prediction (module output)
        trace: List of tool calls/intermediate steps (optional, for ReAct/CodeAct)

    Returns:
        float: Score 0.0-1.0 (for evaluation)
        bool: True/False (for bootstrapping - strict pass/fail)
    """
    pass
```

### Metric Types

**Binary Metrics (for bootstrapping)**:
```python
def exact_match(example, pred, trace=None):
    """Strict match - useful for bootstrapping."""
    return example.answer == pred.answer
```

**Continuous Metrics (for evaluation)**:
```python
def fuzzy_match(example, pred, trace=None):
    """Partial credit - useful for evaluation."""
    if example.answer == pred.answer:
        return 1.0
    elif example.answer.lower() in pred.answer.lower():
        return 0.5
    return 0.0
```

**Multi-Dimensional Metrics**:
```python
def comprehensive_metric(example, pred, trace=None):
    """Evaluate multiple aspects."""

    # Correctness
    correctness = 1.0 if example.answer in pred.answer else 0.0

    # Completeness
    has_reasoning = len(pred.rationale) > 50 if hasattr(pred, 'rationale') else True
    completeness = 1.0 if has_reasoning else 0.5

    # Efficiency (if using tools)
    efficiency = 1.0
    if trace:
        num_tools = len([t for t in trace if hasattr(t, 'tool_name')])
        efficiency = 1.0 if num_tools <= 3 else 0.5

    # Return different values for different contexts
    if trace is None:  # Evaluation mode
        return (correctness + completeness + efficiency) / 3.0
    else:  # Bootstrapping mode (strict)
        return correctness >= 0.5 and completeness >= 0.5
```

---

## LabeledFewShot: Simplest Optimizer

Use labeled examples directly as demonstrations.

```python
import dspy

# Prepare examples
trainset = [
    dspy.Example(question="What is 2+2?", answer="4").with_inputs("question"),
    dspy.Example(question="What is 3*3?", answer="9").with_inputs("question"),
    dspy.Example(question="What is 10/2?", answer="5").with_inputs("question"),
]

# Create optimizer
optimizer = dspy.LabeledFewShot(k=3)  # Use 3 examples as demos

# Compile
module = dspy.ChainOfThought('question -> answer')
optimized = optimizer.compile(student=module, trainset=trainset)

# Result: Module now includes 3 examples in its prompts
```

**Parameters**:
- `k`: Number of examples to use as demonstrations (default: 3)

**Use When**:
- You have high-quality labeled examples
- Quick iteration needed
- No computational budget for optimization

**Cost**: Zero (no LM calls during optimization)

---

## BootstrapFewShot: Self-Generated Demonstrations

Automatically generate demonstrations by running your module on training data.

```python
import dspy

# Define module
class QAModule(dspy.Module):
    def __init__(self):
        self.predictor = dspy.ChainOfThought('question -> answer')

    def forward(self, question):
        return self.predictor(question=question)

# Training data
trainset = [
    dspy.Example(question="What causes rain?",
                 answer="Water evaporates, forms clouds, then falls as precipitation").with_inputs("question"),
    # ... 20-50 examples recommended
]

# Metric
def qa_metric(example, pred, trace=None):
    # Check if key concepts are present
    answer_lower = pred.answer.lower()
    target_lower = example.answer.lower()

    # Simple keyword matching
    keywords = target_lower.split()[:5]  # First 5 words as keywords
    matches = sum(1 for kw in keywords if kw in answer_lower)

    if trace is None:  # Evaluation
        return matches / len(keywords)
    else:  # Bootstrapping
        return matches >= len(keywords) * 0.6  # 60% keywords present

# Optimize
optimizer = dspy.BootstrapFewShot(
    metric=qa_metric,
    max_bootstrapped_demos=4,    # Generate up to 4 demos
    max_labeled_demos=6,          # Use up to 6 from trainset
    max_rounds=1,                 # Optimization rounds
    num_threads=4                 # Parallel processing
)

compiled = optimizer.compile(student=QAModule(), trainset=trainset)
```

**Parameters**:
- `metric`: Quality metric (required)
- `max_bootstrapped_demos`: Max self-generated demos (default: 4)
- `max_labeled_demos`: Max labeled examples to include (default: 16)
- `max_rounds`: Optimization iterations (default: 1)
- `num_threads`: Parallel workers (default: multiprocessing.cpu_count())
- `teacher_settings`: Dict with teacher LM config (optional)

**Teacher-Student Pattern**:
```python
# Use strong model to generate demos for weak model
strong_lm = dspy.LM('openai/gpt-4o')
weak_lm = dspy.LM('openai/gpt-3.5-turbo')

dspy.configure(lm=weak_lm)  # Student uses weak model

optimizer = dspy.BootstrapFewShot(
    metric=metric,
    teacher_settings={'lm': strong_lm}  # Teacher uses strong model
)

compiled = optimizer.compile(student=module, trainset=trainset)
# Result: Weak model improved by strong model's demonstrations
```

**Use When**:
- 20-50 training examples available
- Quick improvement needed (15-30 minutes)
- Budget: $2-10
- Expected gain: +15-30%

---

## BootstrapFewShotWithRandomSearch: Multiple Candidates

Generate multiple candidate programs, select the best.

```python
import dspy

optimizer = dspy.BootstrapFewShotWithRandomSearch(
    metric=metric,
    max_bootstrapped_demos=4,
    num_candidate_programs=10,      # Generate 10 candidates
    num_threads=4
)

compiled = optimizer.compile(student=module, trainset=trainset, valset=valset)
# Returns the best-performing candidate on valset
```

**Parameters**:
- All BootstrapFewShot parameters, plus:
- `num_candidate_programs`: Number of candidates to generate (default: 10)
- `valset`: Validation set for candidate selection (recommended)

**Use When**:
- You have a validation set
- Variance in optimization results
- Willing to spend 2-5x more compute for better results

---

## MIPROv2: Production-Grade Optimization

Multi-prompt Instruction Proposal Optimizer - optimizes instructions, few-shot examples, and hyperparameters.

```python
import dspy

# Requires substantial training data
trainset = load_data(min_size=200)  # 200+ examples recommended
valset = load_data(split='validation')  # Validation set

optimizer = dspy.MIPROv2(
    metric=metric,
    auto="medium",              # "light", "medium", or "heavy"
    num_trials=50,              # Optimization trials
    max_bootstrapped_demos=4,   # Few-shot examples
    max_labeled_demos=8,
    num_threads=8
)

compiled = optimizer.compile(
    student=module,
    trainset=trainset,
    valset=valset,
    num_batches=50,             # Evaluation batches
    max_bootstrapped_demos=4,
    max_labeled_demos=8
)
```

**Parameters**:
- `metric`: Quality metric (required)
- `auto`: Search intensity - "light" (fast), "medium" (balanced), "heavy" (thorough)
- `num_trials`: Optimization attempts (default: 50)
- `max_bootstrapped_demos`: Few-shot examples (default: 3)
- `max_labeled_demos`: Max labeled examples (default: 5)
- `init_temperature`: Starting temperature (default: 1.0)
- `num_threads`: Parallel workers

**Auto Modes**:
- `"light"`: Fast, good for quick iteration (~15-30 mins, 50 trials)
- `"medium"`: Balanced, production default (~30-60 mins, 100+ trials)
- `"heavy"`: Thorough, best results (~1-2 hours, 200+ trials)

**Use When**:
- 200+ training examples available
- Production deployment planned
- Budget: $10-50
- Time: 30-120 minutes
- Expected gain: +30-60%

---

## COPRO: Collaborative Prompt Optimization

Optimizes prompts through collaborative proposal and refinement.

```python
import dspy

optimizer = dspy.COPRO(
    metric=metric,
    breadth=10,                 # Number of prompt proposals per iteration
    depth=3,                    # Optimization depth (iterations)
    init_temperature=1.4        # Creativity in proposals
)

compiled = optimizer.compile(
    student=module,
    trainset=trainset,
    valset=valset
)
```

**Parameters**:
- `metric`: Quality metric (required)
- `breadth`: Prompt proposals per iteration (default: 10)
- `depth`: Optimization iterations (default: 3)
- `init_temperature`: Proposal creativity (default: 1.4)

**Use When**:
- Instruction tuning is important
- Prompt quality matters more than few-shot examples
- Complex reasoning tasks

---

## SIMBA: Agent-Optimized (NEW in 3.x)

Designed specifically for agentic and long-horizon tasks with tool use.

```python
import dspy

# For ReAct agents or multi-step tool use
optimizer = dspy.SIMBA(
    metric=agent_metric,        # Metric should consider tool efficiency
    num_iterations=10,          # Optimization iterations
    population_size=20,         # Candidate population
    max_demos=5                 # Few-shot demonstrations
)

# Metric should evaluate agent behavior
def agent_metric(example, pred, trace=None):
    correctness = evaluate_answer(example, pred)

    if trace:
        # Penalize inefficient tool use
        tool_calls = len(trace)
        efficiency = 1.0 if tool_calls <= 3 else 0.5
        return correctness and efficiency > 0.5

    return correctness

compiled = optimizer.compile(
    student=agent_module,
    trainset=agent_trainset
)
```

**Parameters**:
- `metric`: Quality metric (required, should consider trace)
- `num_iterations`: Optimization rounds (default: 10)
- `population_size`: Candidate pool size (default: 20)
- `max_demos`: Few-shot examples (default: 5)

**Use When**:
- ReAct or CodeAct agents
- Tool use optimization needed
- Long-horizon tasks with multiple steps

**CLIO Note**: SIMBA is ideal for optimizing DataExpert and other CLIO experts that use MCP tools.

---

## GEPA: Genetic-Pareto Optimization (NEW in 3.x)

Multi-objective optimization with prompt compression. Uses genetic algorithms and Pareto optimization.

```python
import dspy

# 5-argument metric for Pareto optimization
def pareto_metric(example, pred, trace=None, correctness=None, efficiency=None):
    """
    Returns dict with multiple objectives.
    """
    # Correctness
    correct = evaluate_answer(example, pred)

    # Efficiency (token usage)
    tokens = len(pred.answer.split())
    efficient = 1.0 if tokens < 100 else 0.5

    # Reasoning quality
    has_reasoning = hasattr(pred, 'rationale') and len(pred.rationale) > 20

    return {
        'correctness': 1.0 if correct else 0.0,
        'efficiency': efficient,
        'reasoning': 1.0 if has_reasoning else 0.0
    }

optimizer = dspy.GEPA(
    metric=pareto_metric,       # Multi-objective metric
    population_size=50,         # Genetic algorithm population
    num_generations=20,         # GA generations
    mutation_rate=0.1,          # Mutation probability
    reflection_lm=None,         # Optional reflection LM
    auto="medium"               # Budget control
)

compiled = optimizer.compile(
    student=module,
    trainset=trainset,
    valset=valset
)
```

**Parameters**:
- `metric`: 5-argument metric returning dict (required)
- `population_size`: GA population (default: 50)
- `num_generations`: Optimization generations (default: 20)
- `mutation_rate`: Genetic mutation rate (default: 0.1)
- `crossover_rate`: Genetic crossover rate (default: 0.7)
- `reflection_lm`: LM for self-reflection (optional)
- `auto`: Budget mode - "light", "medium", "heavy"

**Multi-Objective Metric**:
The metric should return a dict with multiple objectives. GEPA finds Pareto-optimal solutions.

**Use When**:
- Multiple competing objectives (accuracy vs. cost, completeness vs. brevity)
- Prompt compression needed
- Willing to invest in thorough optimization

---

## GRPO: RL-Based Weight Finetuning (NEW in 3.x)

Reinforcement learning-based optimization. Finetunes model weights, not just prompts.

```python
import dspy

# NOTE: Experimental feature
dspy.settings.experimental = True

optimizer = dspy.GRPO(
    metric=metric,
    num_iterations=100,         # RL training iterations
    batch_size=32,              # Training batch size
    learning_rate=1e-5,         # Optimizer learning rate
    reward_shaping=True         # Enable reward shaping
)

compiled = optimizer.compile(
    student=module,
    trainset=trainset,
    valset=valset
)

# Save finetuned model
compiled.save('grpo_finetuned.json')
```

**Parameters**:
- `metric`: Quality metric (used as reward function)
- `num_iterations`: RL training iterations (default: 100)
- `batch_size`: Training batch size (default: 32)
- `learning_rate`: Optimizer learning rate (default: 1e-5)
- `reward_shaping`: Enable reward shaping (default: True)

**Use When**:
- Maximum performance needed
- Have GPU resources for training
- Large training dataset (1000+ examples)
- Budget and time for finetuning

**Cost**: Highest (requires significant compute)

---

## BetterTogether: Ensemble Optimization

Combines multiple optimizers for better results.

```python
import dspy

# Combine multiple optimization strategies
optimizer = dspy.BetterTogether(
    optimizers=[
        dspy.BootstrapFewShot(metric=metric),
        dspy.MIPROv2(metric=metric, auto="medium"),
        dspy.COPRO(metric=metric, breadth=5, depth=2)
    ],
    metric=metric,
    ensemble_strategy='vote'    # or 'average', 'best'
)

compiled = optimizer.compile(
    student=module,
    trainset=trainset,
    valset=valset
)
```

**Parameters**:
- `optimizers`: List of optimizer instances
- `metric`: Quality metric
- `ensemble_strategy`: How to combine results
  - `'vote'`: Majority vote (classification)
  - `'average'`: Average scores (regression)
  - `'best'`: Use best-performing optimizer

**Use When**:
- Maximum performance needed
- Multiple optimization strategies applicable
- Budget allows running multiple optimizers

---

## BootstrapFinetune: Generate Finetuning Data

Create finetuning datasets from optimized demonstrations.

```python
import dspy

optimizer = dspy.BootstrapFinetune(
    metric=metric,
    num_epochs=3,               # Finetuning epochs
    batch_size=16,              # Training batch size
    learning_rate=2e-5          # Learning rate
)

compiled = optimizer.compile(
    student=module,
    trainset=trainset,
    valset=valset,
    output_path='finetuning_data.jsonl'  # Save training data
)

# Result: Both optimized module AND finetuning dataset
```

**Parameters**:
- `metric`: Quality metric
- `num_epochs`: Finetuning epochs (default: 3)
- `batch_size`: Training batch size (default: 16)
- `learning_rate`: Learning rate (default: 2e-5)
- `output_path`: Where to save finetuning data

**Use When**:
- Want to finetune your own model
- Have infrastructure for finetuning
- Large dataset available (500+ examples)

---

## Ensemble: Combine Multiple Programs

Run multiple program variants and combine their outputs.

```python
import dspy

# Create multiple optimized variants
variants = []
for i in range(5):
    optimizer = dspy.BootstrapFewShot(metric=metric)
    compiled = optimizer.compile(student=module, trainset=trainset)
    variants.append(compiled)

# Create ensemble
ensemble = dspy.Ensemble(
    programs=variants,
    size=5,                     # Use all 5 variants
    aggregation='vote'          # or 'average', 'best'
)

# Use ensemble
result = ensemble(input="query")
# Returns combined result from all variants
```

**Parameters**:
- `programs`: List of optimized programs
- `size`: Number of programs to use (default: all)
- `aggregation`: How to combine outputs
  - `'vote'`: Majority vote
  - `'average'`: Average scores/probabilities
  - `'best'`: Use highest-confidence prediction

**Use When**:
- Maximum accuracy needed
- Can afford multiple LM calls per query
- Variance reduction important

---

## KNNFewShot: Nearest Neighbor Demo Selection

Select demonstrations based on similarity to query.

```python
import dspy

# Requires embedder
embedder = dspy.Embedder(model='openai/text-embedding-3-small')

optimizer = dspy.KNNFewShot(
    k=3,                        # Select 3 nearest examples
    embedder=embedder,          # Embedding model
    trainset=trainset           # Examples to search
)

compiled = optimizer.compile(student=module, trainset=trainset)

# At inference time, dynamically selects relevant demos
result = compiled(input="new query")
```

**Parameters**:
- `k`: Number of nearest examples to retrieve
- `embedder`: Embedding model for similarity
- `trainset`: Example pool

**Use When**:
- Large training set (100+ examples)
- Query diversity is high
- Want dynamic demo selection based on query

---

## Save and Load Compiled Programs

```python
# Save optimized program
compiled.save('optimized_module.json')

# Load later
module = MyModule()
module.load('optimized_module.json')

# Or use directly
result = module(input="query")
```

**What Gets Saved**:
- Optimized prompts
- Few-shot demonstrations
- Hyperparameters
- Module structure

**What Doesn't Get Saved**:
- LM configuration (set separately with `dspy.configure()`)
- Training data
- Optimizer settings

---

## Evaluation During Optimization

```python
import dspy
from dspy.evaluate import Evaluate

# Define evaluator
evaluator = Evaluate(
    devset=devset,
    metric=metric,
    num_threads=4,
    display_progress=True,
    display_table=True
)

# Evaluate before optimization
baseline_score = evaluator(module)
print(f"Baseline: {baseline_score:.1%}")

# Optimize
optimizer = dspy.BootstrapFewShot(metric=metric)
compiled = optimizer.compile(student=module, trainset=trainset)

# Evaluate after optimization
optimized_score = evaluator(compiled)
print(f"Optimized: {optimized_score:.1%}")
print(f"Improvement: +{(optimized_score - baseline_score):.1%}")
```

**Evaluate Parameters**:
- `devset`: Evaluation dataset
- `metric`: Quality metric
- `num_threads`: Parallel workers (default: 1)
- `display_progress`: Show progress bar (default: False)
- `display_table`: Show results table (default: False)
- `return_outputs`: Return predictions (default: False)

---

## CLIO Agent Optimization Strategy

### Phase 1: Baseline (v0.1.0)
No optimization. Manual prompts and signatures.

```python
# v0.1.0 - Just DSPy modules, no optimization
data_expert = DataExpert()
result = data_expert.forward(task="Analyze HDF5 file")
```

### Phase 2: Quick Optimization (v0.2.0)
Bootstrap with limited data.

```python
# v0.2.0 - Collect 20-30 usage examples
usage_logs = load_arc_invocations(limit=30)
trainset = convert_to_examples(usage_logs)

# Quick optimization
optimizer = dspy.BootstrapFewShot(
    metric=clio_expert_metric,
    max_bootstrapped_demos=3
)

optimized_expert = optimizer.compile(
    student=DataExpert(),
    trainset=trainset
)

optimized_expert.save('data_expert_v2.json')
```

### Phase 3: Production Optimization (v0.4.0)
MIPROv2 with substantial data and SIMBA for agents.

```python
# v0.4.0 - Collect 200+ invocations from ARC
usage_logs = load_arc_invocations(limit=300)
trainset, valset = split_data(usage_logs, ratio=0.8)

# For reasoning-heavy experts
mipro_optimizer = dspy.MIPROv2(
    metric=clio_expert_metric,
    auto="medium",
    num_trials=50
)

optimized_expert = mipro_optimizer.compile(
    student=DataExpert(),
    trainset=trainset,
    valset=valset
)

# For tool-using agents (if we add ReAct later)
simba_optimizer = dspy.SIMBA(
    metric=tool_efficiency_metric,
    num_iterations=10
)

optimized_agent = simba_optimizer.compile(
    student=ToolAgent(),
    trainset=agent_trainset
)
```

### Phase 4: Multi-Objective (v0.5.0)
GEPA for balancing accuracy, cost, and efficiency.

```python
# v0.5.0 - Multi-objective optimization
def clio_pareto_metric(example, pred, trace=None, correctness=None, efficiency=None):
    """Balance accuracy, token usage, and tool efficiency."""
    return {
        'accuracy': evaluate_correctness(example, pred),
        'efficiency': evaluate_token_usage(pred),
        'tool_use': evaluate_tool_calls(trace) if trace else 1.0
    }

gepa_optimizer = dspy.GEPA(
    metric=clio_pareto_metric,
    population_size=30,
    num_generations=15,
    auto="medium"
)

optimized = gepa_optimizer.compile(
    student=expert,
    trainset=trainset,
    valset=valset
)
```

### CLIO Metric Example

```python
def clio_expert_metric(example, pred, trace=None):
    """
    Multi-dimensional metric for CLIO experts.

    Evaluates:
    - Correctness: Did it solve the problem?
    - Completeness: Is the analysis thorough?
    - Actionability: Are recommendations specific?
    - Tool efficiency: Minimal unnecessary tool calls
    """

    # 1. Correctness
    correctness = validate_solution(example, pred)

    # 2. Completeness (has substantive analysis)
    completeness = len(pred.analysis) > 100 if hasattr(pred, 'analysis') else True

    # 3. Actionability (specific recommendations)
    actionable = (
        hasattr(pred, 'recommendations') and
        len(pred.recommendations) >= 2 and
        all(len(r) > 20 for r in pred.recommendations)
    )

    # 4. Tool efficiency (if applicable)
    tool_efficient = True
    if trace:
        tool_calls = [t for t in trace if hasattr(t, 'tool_name')]
        tool_efficient = len(tool_calls) <= 5

    # Evaluation mode: return score
    if trace is None:
        score = (
            (1.0 if correctness else 0.0) +
            (1.0 if completeness else 0.5) +
            (1.0 if actionable else 0.5) +
            (1.0 if tool_efficient else 0.5)
        ) / 4.0
        return score

    # Bootstrapping mode: strict pass/fail
    return correctness and completeness and actionable
```

---

## Optimizer Selection Guide

| Optimizer | Data Size | Time | Cost | Quality Gain | Use Case |
|-----------|-----------|------|------|--------------|----------|
| LabeledFewShot | 5-20 | 0s | $0 | +5-15% | Quick demos, no budget |
| BootstrapFewShot | 20-100 | 10-30m | $2-10 | +15-30% | Fast iteration |
| BootstrapFewShotWithRandomSearch | 50-200 | 30-90m | $5-25 | +20-40% | Better results, have valset |
| MIPROv2 | 200-1000 | 30-120m | $10-50 | +30-60% | Production quality |
| COPRO | 50-200 | 20-60m | $5-20 | +20-40% | Instruction-heavy tasks |
| SIMBA | 50-200 | 30-60m | $10-30 | +25-50% | Agents, tool use |
| GEPA | 100-500 | 60-180m | $20-100 | +30-60% | Multi-objective, compression |
| GRPO | 1000+ | hours | $100+ | +40-80% | Maximum performance, have GPUs |
| BetterTogether | varies | 2-5x | 2-5x | +5-15% over best | Ensemble everything |
| BootstrapFinetune | 500+ | hours | $50+ | +40-80% | Finetune your model |
| KNNFewShot | 100+ | 0s | $0 | +10-25% | Dynamic demo selection |

---

## Best Practices

### 1. Start Simple
Begin with LabeledFewShot or BootstrapFewShot before moving to advanced optimizers.

### 2. Collect Quality Data
- **Minimum**: 20 examples for BootstrapFewShot
- **Recommended**: 200+ examples for MIPROv2
- **Diversity**: Cover different query types
- **Quality**: Clean, correct, representative

### 3. Design Good Metrics
- **Binary for bootstrapping**: Return True/False for strict filtering
- **Continuous for evaluation**: Return 0.0-1.0 for nuanced comparison
- **Multi-dimensional**: Consider multiple aspects (accuracy, efficiency, cost)

### 4. Use Validation Sets
Always split data into train/val/test for honest evaluation:
```python
from sklearn.model_selection import train_test_split

train, test = train_test_split(data, test_size=0.2, random_state=42)
train, val = train_test_split(train, test_size=0.2, random_state=42)
```

### 5. Track Costs
```python
import dspy

# Enable usage tracking
dspy.configure_usage_tracking(enable=True)

# After compilation
print(f"Optimization cost: ${compiled.get_lm_usage()['cost']:.2f}")
```

### 6. Version Control
```python
# Save with version metadata
compiled.save(f'expert_v{version}_optimized.json')

# Log optimization results
with open(f'optimization_log_v{version}.txt', 'w') as f:
    f.write(f"Baseline: {baseline_score:.2%}\n")
    f.write(f"Optimized: {optimized_score:.2%}\n")
    f.write(f"Gain: +{(optimized_score - baseline_score):.2%}\n")
```

### 7. Iterate
Optimization is iterative:
1. Collect 20 examples → BootstrapFewShot → deploy → collect more data
2. Collect 200 examples → MIPROv2 → deploy → monitor performance
3. Refine metric based on failures → re-optimize
4. Continuous improvement

---

## Troubleshooting

### "Not enough training data"
**Solution**: Start with LabeledFewShot (needs 5-10 examples) or generate synthetic data.

### "Optimization too expensive"
**Solution**:
- Use cheaper LM for optimization (GPT-4o-mini instead of GPT-4o)
- Reduce `num_trials` or `num_candidate_programs`
- Use `auto="light"` for MIPROv2

### "Optimization not improving results"
**Solution**:
- Check metric function (debug with print statements)
- Ensure training data quality
- Verify data diversity
- Try different optimizer (e.g., MIPROv2 if BootstrapFewShot plateaus)

### "Compiled program performs worse"
**Solution**:
- Overfitting to training data - need more diverse examples
- Metric doesn't capture true quality - refine metric
- Bad demonstrations selected - try different optimizer
- Evaluate on proper test set (not training data)

---

## Summary

DSPy optimization transforms manual prompt engineering into automated, data-driven improvement:

1. **Collect data**: 20-1000+ examples depending on optimizer
2. **Define metric**: What "good" means for your task
3. **Choose optimizer**: Based on budget, time, and data size
4. **Compile**: Let DSPy find optimal prompts and demonstrations
5. **Evaluate**: Measure improvement on test set
6. **Deploy**: Use optimized program in production

**For CLIO Agent**:
- v0.2.0: BootstrapFewShot with 30 examples (+15-25% gain)
- v0.4.0: MIPROv2 with 200+ examples (+30-50% gain), SIMBA for tool agents
- v0.5.0: GEPA for multi-objective optimization (accuracy + efficiency + cost)

Next: [LM Integration Guide](./05_LM_INTEGRATION.md) for language model configuration and local AI.
