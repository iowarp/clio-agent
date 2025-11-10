# BLOCKER: DSPy 3.0.3 ReAct + LM Studio Incompatibility

**Status**: BLOCKING v0.3.0 completion
**Severity**: CRITICAL
**Root Cause**: Adapter incompatibility

---

## The Problem

DSPy 3.0.3 ReAct pattern requires JSONAdapter for structured tool calling.
JSONAdapter uses `response_format` parameter.
LM Studio rejects `response_format` parameter (not in OpenAI spec they support).

**Error**: `'response_format.type' must be 'json_schema' or 'text'`

## What Was Tried

1. ✗ `supports_response_format=False` - Doesn't exist in DSPy 3.x
2. ✗ `adapter=dspy.ChatAdapter()` - Still falls back to JSONAdapter internally
3. ✗ `lm_studio/` prefix - LiteLLM recognizes but DSPy adapter issue persists
4. ✗ `model_type='responses'` - DSPy parser incompatible with LM Studio format

## Research Findings

- gpt-oss uses Harmony format (analysis/commentary/final channels)
- LM Studio translates Harmony automatically
- DSPy 3.0.3 has minimal breaking changes from 2.6
- ChatAdapter→JSONAdapter fallback is hardcoded in DSPy
- LM Studio supports: /v1/chat/completions, /v1/responses, /v1/completions
- DSPy model_type='responses' exists but response parser incompatible

## Solutions

### Option A: Use ChainOfThought (WORKS)
Replace ReAct with ChainOfThought in claudio.py:
```python
self.agent = dspy.ChainOfThought(
    MainAgentSignature,
    n=3  # Multiple reasoning passes
)
```

**Pros**: Works immediately, no response_format
**Cons**: No automatic tool calling (lose expert routing)

### Option B: Custom Adapter (PROPER)
Create NoFallbackChatAdapter that doesn't retry with JSONAdapter.
**Pros**: Keeps ReAct pattern
**Cons**: Complex, might break in DSPy updates

### Option C: Refactor to Manual Tool Loop
Don't use DSPy ReAct, manually implement tool loop:
```python
for i in range(max_iters):
    decision = self.decide_next_action(question, trajectory)
    if decision.action == "call_expert":
        result = self.call_expert(decision.expert, question)
    elif decision.action == "answer":
        return decision.answer
```

**Pros**: Full control, no adapter issues
**Cons**: Reimplementing ReAct logic

---

## Recommendation

**For v0.3.0**: Use Option A (ChainOfThought)
- Get system working
- Complete bug fixes
- Validate architecture

**For v0.4.0**: Implement Option B or C
- Add custom adapter or manual tool loop
- Integrate with Optimizer Layer
- Proper multi-agent coordination

---

## Current State

Wave 1 complete (29 bugs fixed):
- ✓ Schema alignment
- ✓ Main Agent ReAct structure
- ✓ CLI response fields
- ✓ DataExpert signature
- ✗ ReAct inference blocked by adapter issue

**Decision needed to proceed.**

