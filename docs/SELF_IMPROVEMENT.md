# CLIO Agent Self-Improvement

How CLIO Agent learns and gets better with use.

---

## Concept

**CLIO Agent improves through two mechanisms**:
1. **ARC Memory**: Stores conversations, metrics, performance data
2. **Optimizer Layer**: Analyzes ARC metrics and tunes agents

**Result**: CLIO Agent gets better over time based on actual usage data.

---

## What Gets Optimized

### 1. Prompts (Tier 2 Experts)
- Few-shot examples
- Reasoning instructions
- Field descriptions

### 2. Routing Logic (Tier 1 Main Agent)
- Which expert to select for queries
- Capability matching rules
- Confidence thresholds

### 3. Tool Selection (Tier 2 Experts)
- When to call which MCP tool
- Tool parameters
- Execution order

---

## How It Works

### Offline Tuning (v0.4.0+)

User runs optimization session:

```bash
uv run src/clio_agent/ui/cli.py --tune
```

**Process**:
1. Select component (DataExpert prompts, routing, tools)
2. Generate training set from ARC history (real usage data)
3. Choose optimizer (BootstrapFewShot or MIPRO)
4. Run optimization (finds better prompts/logic)
5. Evaluate before/after metrics
6. Deploy if improvement > 5%

**Duration**: Minutes (BootstrapFewShot) to hours (MIPRO)

### Online Learning (v0.5.0+)

Automatic improvement while running:

```
While operating:
  → Capture metrics in ARC
  → A/B test new variants (10% traffic)
  → Compare performance
  → Roll out if better
```

**Result**: Continuous improvement without manual intervention.

---

## Implementation

**Internal**: Uses DSPy optimizers (BootstrapFewShot, MIPROv2)
**External**: Exposed via CLIO AgentOptimizer API

**Storage**: All metrics in ARC, optimized variants versioned in ARC

---

## Metrics

**Tracked in ARC**:
- Success rate (task completion)
- Latency (response time)
- User satisfaction (implicit/explicit)
- Tool efficiency (calls per query)

**Target Improvement**: > 5% on composite score

---

## Architecture Integration

```
Agent Invocation
  ↓
Store metrics in ARC
  ↓
Optimizer Layer reads ARC metrics
  ↓
Identifies improvement opportunities
  ↓
Tunes prompts/routing/tools
  ↓
Stores optimized variant in ARC
  ↓
Future invocations use optimized variant
  ↓
Better performance → Store in ARC
  ↓
Continuous cycle
```

---

**See**: PLAN.md (v0.4.0) for implementation tasks
**See**: CLIO_AGENT_ARCHITECTURE.md for full architecture
